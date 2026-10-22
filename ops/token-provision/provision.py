#!/usr/bin/env python3
"""One-shot offline provisioning. Never log crypto input or subprocess diagnostics."""
import argparse
import base64
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys

OPENSSL = '/usr/bin/openssl'


def crypto(args, data=None):
    return subprocess.run([OPENSSL] + args, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=True).stdout


def exclusive(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)


def options(mode):
    base = ['-pkeyopt', 'rsa_padding_mode:oaep']
    # Legacy LibreSSL has no rsa_oaep_md option; SHA-1 is OAEP's default.
    return base if mode == 'sha1' else base + ['-pkeyopt', 'rsa_oaep_md:' + mode]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['gen', 'encrypt', 'decrypt'])
    parser.add_argument('key')
    parser.add_argument('output', nargs='?')
    parser.add_argument('--wipe-key', nargs='?', const='unlink', choices=['unlink', 'truncate-only'])
    parser.add_argument('--require-mode', choices=['sha256'])
    a = parser.parse_args()
    if a.action == 'gen':
        directory = Path(a.key)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise ValueError('private directory required')
        private = crypto(['genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072'])
        exclusive(directory / 'private.pem', private)
        sys.stdout.buffer.write(crypto(['pkey', '-pubout'], private))
    elif a.action == 'encrypt':
        if not a.output:
            raise ValueError('output required')
        directory = Path(a.output).absolute().parent
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise ValueError('private output directory required')
        token = secrets.token_urlsafe(32).encode() + b'\n'
        exclusive(a.output, token)
        for mode in (('sha256',) if a.require_mode else ('sha256', 'sha1')):
            try:
                encrypted = crypto(['pkeyutl', '-encrypt', '-pubin', '-inkey', a.key] + options(mode), token)
                break
            except subprocess.CalledProcessError:
                if mode == 'sha1' or a.require_mode:
                    raise
        print('OAEP-' + mode.upper())
        print(base64.b64encode(encrypted).decode())
    else:
        if not a.output:
            raise ValueError('output required')
        envelope = sys.stdin.buffer.read(16385)
        if len(envelope) > 16384:
            raise ValueError('oversized envelope')
        mode, encoded = envelope.splitlines()
        modes = {b'OAEP-SHA256': 'sha256', b'OAEP-SHA1': 'sha1'}
        if a.require_mode and modes.get(mode) != a.require_mode:
            raise ValueError('required OAEP mode mismatch')
        token = crypto(['pkeyutl', '-decrypt', '-inkey', a.key] + options(modes[mode]),
                       base64.b64decode(encoded, validate=True))
        value = token[:-1] if token.endswith(b'\n') else token
        if not 16 <= len(value) <= 4096 or any(c < 33 or c > 126 for c in value):
            raise ValueError('invalid decrypted token')
        exclusive(a.output, token)
        if a.wipe_key:
            fd = os.open(a.key, os.O_WRONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'r+b') as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError('unsafe private key')
                size = os.fstat(f.fileno()).st_size
                f.write(b'\0' * size)
                f.flush()
                os.fsync(f.fileno())
                f.truncate(0)
                os.fsync(f.fileno())
            if a.wipe_key != 'truncate-only':
                os.unlink(a.key)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Provisioning failed; no credential diagnostics emitted')
