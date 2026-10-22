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
    make.add_argument('--nonce', required=True)
    verify.add_argument('--challenge-file', required=True)
    actions = verify.add_mutually_exclusive_group(required=True)
    actions.add_argument('--new-challenge', action='store_true')
    actions.add_argument('--mac')
    args = parser.parse_args()
    if args.action == 'verify' and args.new_challenge:
        try:
            fd = os.open(args.challenge_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_hex(32) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            return 0
        except OSError:
            return 1
    try:
        if args.action == 'make':
            raw_nonce = args.nonce
        else:
            raw_nonce = read_token(args.challenge_file).decode('ascii')
            # Consume the verifier's saved challenge before checking a response.
            os.unlink(args.challenge_file)
        token = read_token(args.token_file)
        nonce = bytes.fromhex(raw_nonce)
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
    print('一致' if match else '不一致')
    return 0 if match else 1


if __name__ == '__main__':
    sys.exit(main())
