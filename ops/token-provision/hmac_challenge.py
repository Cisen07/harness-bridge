#!/usr/bin/env python3
"""Offline proof that two machines hold the same credential; never emit its hash."""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys

# Reuse the exact bridge file checks without loading deployment configuration.
# bridge_config is deliberately not imported: this tool requires only a token path.
import stat


def read_token(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError('unsafe token file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(4097)
    finally:
        os.close(fd)
    token = raw.strip()
    if len(raw) > 4096 or not token or any(c < 33 or c > 126 for c in token):
        raise ValueError('invalid token')
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    make = sub.add_parser('make')
    make.add_argument('--token-file', required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--token-file', required=True)
    verify.add_argument('--nonce', required=True)
    verify.add_argument('--mac', required=True)
    args = parser.parse_args()
    try:
        token = read_token(args.token_file)
        nonce = secrets.token_bytes(32) if args.action == 'make' else bytes.fromhex(args.nonce)
        if len(nonce) != 32:
            raise ValueError('invalid nonce')
        mac = hmac.new(token, nonce, hashlib.sha256).digest()
        if args.action == 'make':
            print(json.dumps({'nonce': nonce.hex(), 'mac': mac.hex()}))
            return 0
        candidate = bytes.fromhex(args.mac)
        match = hmac.compare_digest(mac, candidate)
    except (OSError, ValueError):
        if args.action == 'make':
            return 1
        match = False
    print('match' if match else 'mismatch')
    return 0 if match else 1


if __name__ == '__main__':
    sys.exit(main())
