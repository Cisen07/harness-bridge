#!/usr/bin/env python3
"""Restart the macOS bridge LaunchAgent from INSIDE a bridge job without being reaped.

A job that restarts its own bridge is killed with it (it ends as "interrupted" - expected). A plain
`nohup sh -c '...' &` does not survive either: the agent CLI reaps the command's process group when the job ends.
This helper double-forks + setsid, sleeps, then runs `launchctl kickstart -k gui/<uid>/<label>`.

Usage (run on the Mac, e.g. as the last step of a mac-codex job):
    python3 -B restart_bridge_detached.py <launchd-label> [--delay 5] [--log ~/Library/Logs/harness-bridge/restart.log]
Before running: confirm no other job is queued/running (GET /jobs is per id; check your own records),
back up the bridge files with `cp -p`, and `python3 -m py_compile` the new code.
Afterwards, verify from the dispatcher: GET /health, GET /backends, then a tiny smoke job.
Never use `launchctl submit` (KeepAlive would restart it in a loop).
"""
import argparse
import os
import pathlib
import re
import subprocess
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('label')
    ap.add_argument('--delay', type=int, default=5)
    ap.add_argument('--log', default=str(pathlib.Path.home() / 'Library/Logs/harness-bridge/restart.log'))
    a = ap.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,120}', a.label):
        sys.exit('invalid launchd label')
    log = pathlib.Path(a.log).expanduser()
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.fork():
        print('restart scheduled in %ds for %s; log: %s' % (a.delay, a.label, a.log))
        return
    os.setsid()
    if os.fork():
        os._exit(0)
    time.sleep(a.delay)
    r = subprocess.run(['launchctl', 'kickstart', '-k', 'gui/%d/%s' % (os.getuid(), a.label)],
                       capture_output=True, text=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('rc=%s out=%s err=%s\n' % (r.returncode, r.stdout.strip(), r.stderr.strip()))
    os._exit(0)


if __name__ == '__main__':
    main()
