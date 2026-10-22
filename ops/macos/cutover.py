#!/usr/bin/env python3
"""Offline LaunchAgent cutover. Dry-run by default; --execute detaches before delay."""
import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shlex
import stat
import hmac
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


def owned_file(path, modes, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in modes:
            raise ValueError('unsafe control file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('oversized control file')
            return data
    finally:
        os.close(fd)


def require_absent_confirmation(path):
    if path is not None and os.path.lexists(path):
        raise ValueError('confirmation file already exists')


def confirmation(path, nonce):
    """Return False while a producer is writing; reject unsafe or complete wrong files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('unsafe confirmation file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(4097)
        if stat.S_IMODE(info.st_mode) != 0o600:
            return False
        expected = (nonce + '\n').encode('ascii')
        if not data or not data.endswith(b'\n') or len(data) < len(expected):
            return False
        if not hmac.compare_digest(data, expected):
            raise ValueError('confirmation mismatch')
        return True
    finally:
        os.close(fd)


def lsof_output(port):
    # Tolerate two transient command failures; unsafe listener output still fails at once.
    for attempt in range(3):
        try:
            return run(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:' + str(port), '-sTCP:LISTEN', '-Fpn'])
        except (OSError, subprocess.SubprocessError):
            if attempt == 2:
                raise


def listener(host, port, label=None, release=None):
    output = lsof_output(port)
    pid, found = None, set()
    for line in output.splitlines():
        if line.startswith('p'):
            pid = line[1:]
        if line.startswith('n'):
            if line != 'n%s:%s' % (host, port) or not pid or not pid.isdigit():
                raise ValueError('unexpected listener')
            found.add(pid)
    if len(found) != 1:
        raise ValueError('missing or ambiguous listener')
    pid = found.pop()
    if label:
        output = run(['/bin/launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)])
        match = re.search(r'^\s*pid = ([0-9]+)\s*$', output, re.M)
        if not match or match.group(1) != pid:
            raise ValueError('launchd listener mismatch')
    if release:
        command = run(['/bin/ps', '-o', 'command=', '-p', pid])
        root = str(Path(release).resolve())
        paths = [word.partition('=')[2] if '=' in word else word for word in shlex.split(command)]
        if not any(os.path.isabs(word) and (os.path.normpath(word) == root or os.path.normpath(word).startswith(root + '/')) for word in paths):
            raise ValueError('unexpected release')
    return pid


def check(host, port, url=None, clock=time.monotonic, sleep=time.sleep, label=None, release=None):
    start = clock()
    stable_since = None
    last = None
    result = {'listener': False, 'pid_stable': False, 'self_check': url is None}
    while clock() - start < 60:
        try:
            current = listener(host, port, label, release)
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
                    result['pid'] = current
                    return result
        except ValueError:
            return {'listener': False, 'pid_stable': False, 'self_check': False}
        except (OSError, subprocess.SubprocessError):
            result.update(listener=False, pid_stable=False, self_check=url is None)
            stable_since = None
            last = None
        sleep(min(2, max(0, 60 - (clock() - start))))
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def success(checks):
    return all(checks.values())


def execute(a, target, clock=None, sleep=None):
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    require_absent_confirmation(getattr(a, 'confirm_file', None))
    snapshot = owned_file(a.plist_snapshot, {0o600, 0o644}, 1024 * 1024)
    state = {'phase': 'armed', 'rolled_back': False, 'checks': {}}
    domain = 'gui/%d' % os.getuid()
    def save(phase):
        state['phase'] = phase
        state['timestamp'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        fd, temporary = tempfile.mkstemp(dir=Path(a.state_file).parent, prefix='.state-')
        try:
            with os.fdopen(fd, 'w') as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, a.state_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    def switch(source, restore=False):
        try:
            run(['/bin/launchctl', 'bootout', domain + '/' + a.label])
        except subprocess.CalledProcessError:
            pass
        if restore:
            # Use the bytes validated before arming, even if the path changes later.
            fd, name = tempfile.mkstemp(dir=target.parent, prefix='.snapshot-')
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(snapshot)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, target)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        else:
            install(source, target)
        run(['/bin/launchctl', 'bootstrap', domain, str(target)])
    def unchanged(pid):
        if listener(a.host, a.port, a.label, a.expect_release) != pid:
            raise ValueError('listener changed')
    save('armed')
    sleep(a.delay)
    try:
        switch(a.plist_new)
        save('switched')
        save('self_check')
        state['checks']['new'] = check(a.host, a.port, a.self_check_url, clock=clock, sleep=sleep,
                                     label=a.label, release=a.expect_release)
        if not success(state['checks']['new']):
            raise RuntimeError('health check failed')
        pid = state['checks']['new']['pid']
        save('awaiting_confirm')
        deadline = clock() + a.confirm_timeout
        while True:
            unchanged(pid)
            if confirmation(a.confirm_file, a.confirm_nonce):
                break
            if clock() >= deadline:
                raise TimeoutError('confirmation timeout')
            sleep(min(2, deadline - clock()))
        save('confirmed')
        save('watching')
        deadline = clock() + a.post_confirm_watch
        while clock() < deadline:
            sleep(min(30, deadline - clock()))
            unchanged(pid)
        save('complete')
    except Exception:
        state['rolled_back'] = True
        try:
            switch(a.plist_snapshot, restore=True)
            state['checks']['rollback'] = check(a.host, a.port, a.self_check_url, clock=clock, sleep=sleep, label=a.label)
            save('rolled_back' if success(state['checks']['rollback']) else 'rollback_failed')
        except Exception:
            save('rollback_failed')
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('label', 'plist-new', 'plist-snapshot', 'bind', 'state-file'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--self-check-url')
    p.add_argument('--expect-release')
    p.add_argument('--confirm-file')
    p.add_argument('--confirm-nonce')
    p.add_argument('--confirm-timeout', type=float, default=600)
    p.add_argument('--post-confirm-watch', type=float, default=300)
    p.add_argument('--delay', type=float, default=5)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--execute', action='store_true')
    a = p.parse_args()
    if a.execute and not all((a.self_check_url, a.expect_release, a.confirm_file, a.confirm_nonce)):
        p.error('execute requires self-check-url, expect-release, confirm-file and confirm-nonce')
    if not all(math.isfinite(n) and n >= 0 for n in (a.delay, a.confirm_timeout, a.post_confirm_watch)):
        p.error('invalid timing')
    if a.confirm_nonce and (not a.confirm_nonce.isascii() or not 1 <= len(a.confirm_nonce) <= 4095 or any(c.isspace() for c in a.confirm_nonce)):
        p.error('invalid confirmation nonce')
    if a.expect_release and (not Path(a.expect_release).is_absolute() or str(Path(a.expect_release).resolve()) == '/'):
        p.error('expect-release must be an absolute release directory')
    try:
        require_absent_confirmation(a.confirm_file)
    except ValueError as error:
        p.error(str(error))
    owned_file(a.plist_snapshot, {0o600, 0o644}, 1024 * 1024)
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
        print('Dry run: detach; delay %.1fs; launchctl bootout gui/%d/%s; atomic copy %s -> %s; launchctl bootstrap; lsof exact listener + stable PID >=30s; HTTP 403; launchd PID + release argv; external nonce confirmation; post-confirm watch; on failure restore %s and recheck; state %s' %
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
