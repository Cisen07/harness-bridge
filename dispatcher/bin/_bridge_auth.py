#!/usr/bin/env python3
"""Emit a curl header on a private pipe; never put credentials in argv or errors."""
import os
import pathlib
import stat
import sys


def auth_header():
    required = os.environ.get('HARNESS_BRIDGE_REQUIRE_TOKEN', '1')
    if required == '0':
        return ''
    if required != '1':
        raise ValueError('Invalid token requirement')
    path = os.environ.get('HARNESS_BRIDGE_TOKEN_FILE') or str(
        pathlib.Path(os.environ.get('HARNESS_HOME', os.path.expanduser('~/harness'))) / 'bridge.token')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError('Unsafe token file')
        token = stream.read(4097).strip()
    if not token or len(token) > 4096 or any(c < 33 or c > 126 for c in token):
        raise ValueError('Invalid token')
    return 'Authorization: Bearer ' + token.decode('ascii')


if __name__ == '__main__':
    try:
        print(auth_header())
    except (OSError, ValueError):
        sys.exit('Bridge token file unavailable or unsafe (requires owned regular file, mode 0600)')
