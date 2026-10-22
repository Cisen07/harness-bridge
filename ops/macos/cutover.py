#!/usr/bin/env python3
"""Offline LaunchAgent cutover. Dry-run by default; --execute detaches before delay."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def run(command, timeout=8):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True).stdout


def install(source, target):
    fd, name = tempfile.mkstemp(dir=target.parent, prefix='.cutover-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(Path(source).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check(host, port, url=None, clock=time.monotonic, sleep=time.sleep):
    start = clock()
    stable_since = None
    last = None
    result = {'listener': False, 'pid_stable': False, 'self_check': url is None}
    while clock() - start < 60:
        try:
            output = run(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:' + str(port), '-sTCP:LISTEN', '-Fpn'],
                         timeout=min(8, max(0.01, 60 - (clock() - start))))
            pid = None
            found = set()
            for line in output.splitlines():
                if line.startswith('p'):
                    pid = line[1:]
                if line == 'n%s:%s' % (host, port) and pid:
                    found.add(pid)
            current = next(iter(found)) if len(found) == 1 else None
            result['listener'] = current is not None
            if current is None or current != last:
                stable_since = clock() if current else None
            last = current
            result['pid_stable'] = stable_since is not None and clock() - stable_since >= 30
            if clock() - start >= 60:
                break
            if result['listener'] and result['pid_stable']:
                if url:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    try:
                        with opener.open(url, timeout=min(3, max(0.01, 60 - (clock() - start)))) as response:
                            result['self_check'] = response.status == 403
                    except urllib.error.HTTPError as error:
                        result['self_check'] = error.code == 403
                if result['self_check']:
                    return result
        except (OSError, subprocess.SubprocessError):
            result['listener'] = False
            stable_since = None
            last = None
        sleep(min(2, max(0, 60 - (clock() - start))))
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def success(checks):
    return all(checks.values())


def execute(a, target):
    state = {'phase': 'delay', 'rolled_back': False, 'checks': {}}
    domain = 'gui/%d' % os.getuid()
    def save():
        state['timestamp'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        fd, temporary = tempfile.mkstemp(dir=Path(a.state_file).parent, prefix='.state-')
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(state, f)
        os.replace(temporary, a.state_file)
    def switch(source):
        # bootout may report already absent; bootstrap remains checked.
        try:
            run(['/bin/launchctl', 'bootout', domain + '/' + a.label])
        except subprocess.CalledProcessError:
            pass
        install(source, target)
        run(['/bin/launchctl', 'bootstrap', domain, str(target)])
    save()
    time.sleep(a.delay)
    try:
        state['phase'] = 'cutover'
        save()
        switch(a.plist_new)
        state['checks']['new'] = check(a.host, a.port, a.self_check_url)
        if not success(state['checks']['new']):
            raise RuntimeError('health check failed')
        state['phase'] = 'complete'
    except Exception:
        state['rolled_back'] = True
        state['phase'] = 'rollback'
        save()
        try:
            switch(a.plist_snapshot)
            state['checks']['rollback'] = check(a.host, a.port, a.self_check_url)
            state['phase'] = 'rolled_back' if success(state['checks']['rollback']) else 'rollback_failed'
        except Exception:
            state['phase'] = 'rollback_failed'
    save()
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('label', 'plist-new', 'plist-snapshot', 'bind', 'state-file'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--self-check-url')
    p.add_argument('--delay', type=float, default=5)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--execute', action='store_true')
    a = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,120}', a.label) or a.delay < 0:
        p.error('invalid label/delay')
    a.host, raw_port = a.bind.rsplit(':', 1)
    host = ipaddress.ip_address(a.host)
    a.port = int(raw_port)
    if not (host == ipaddress.ip_address('127.0.0.1') or host in ipaddress.ip_network('100.64.0.0/10')) or not 0 < a.port < 65536:
        p.error('invalid bind')
    if a.self_check_url:
        u = urllib.parse.urlsplit(a.self_check_url)
        if u.scheme != 'http' or u.hostname != a.host or u.port != a.port or u.username or u.password or u.fragment:
            p.error('self check must use the exact bind endpoint without credentials')
    for source in (a.plist_new, a.plist_snapshot):
        with open(source, 'rb') as f:
            if plistlib.load(f).get('Label') != a.label:
                p.error('plist label mismatch')
    target = Path.home() / 'Library/LaunchAgents' / (a.label + '.plist')
    if not a.execute:
        print('Dry run: detach; delay %.1fs; launchctl bootout gui/%d/%s; atomic copy %s -> %s; launchctl bootstrap; lsof exact listener + stable PID >=30s; optional HTTP 403; on failure restore %s and recheck; state %s' %
              (a.delay, os.getuid(), a.label, a.plist_new, target, a.plist_snapshot, a.state_file))
        return
    if os.fork():
        return
    os.setsid()
    if os.fork():
        os._exit(0)
    os.chdir('/')
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)
    execute(a, target)
    os._exit(0)


if __name__ == '__main__':
    main()
