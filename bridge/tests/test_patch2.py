"""Patch 2 regression: fake credentials, mock launchctl/lsof/ps and virtual time."""
import contextlib
import email.message
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error

import test_bridge as T
import bridge_config as C
from test_patch1 import cutover, ROOT


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


P = module('provision2', 'ops/token-provision/provision.py')
H = module('challenge', 'ops/token-provision/hmac_challenge.py')


class Cutover(unittest.TestCase):
    def flow(self, scenario):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            for name in ('new', 'old'):
                (d / name).write_bytes(name.encode())
                (d / name).chmod(0o600)
            a = types.SimpleNamespace(label='test.bridge', plist_new=d / 'new', plist_snapshot=d / 'old',
                                      host='127.0.0.1', port=12345, self_check_url='http://127.0.0.1:12345/health',
                                      state_file=d / 'state', delay=0, expect_release='/private/new-release',
                                      confirm_file=d / 'confirm', confirm_nonce='fake-nonce', confirm_timeout=20,
                                      post_confirm_watch=300)
            now, phases = [0], []
            def confirm(value='fake-nonce\n'):
                a.confirm_file.write_text(value); a.confirm_file.chmod(0o600)
            def sleep(seconds):
                now[0] += seconds
                if scenario == 'crash35' and now[0] >= 40:
                    confirm()
            def run(command, **kwargs):
                if command[1] in ('bootout', 'bootstrap'):
                    return ''
                new = (d / 'target').read_bytes() == b'new'
                pid = '123' if new else '321'
                if new and scenario == 'pid_change' and now[0] >= 35:
                    pid = '124'
                if command[0].endswith('lsof'):
                    if new and scenario in ('crash35', 'watch_loss') and now[0] >= 35:
                        raise subprocess.CalledProcessError(1, command)
                    if new and scenario == 'port_occupied':
                        pid = '999'
                    return 'p%s\nn127.0.0.1:12345\n' % pid
                if command[1] == 'print':
                    return '\tpid = %s\n' % ('123' if new and scenario == 'port_occupied' else pid)
                if command[0].endswith('ps'):
                    return '/usr/bin/python3 /private/%s/bridge.py' % ('new-release' if new and scenario != 'old_argv' and not (scenario == 'watch_argv' and now[0] >= 35) else 'old-release')
                self.fail('unexpected command')
            original_dump = json.dump
            def dump(state, stream):
                phases.append(state['phase']); original_dump(state, stream)
                if state['phase'] == 'awaiting_confirm':
                    if scenario not in ('timeout', 'crash35'):
                        confirm('wrong\n' if scenario == 'wrong' else 'fake-nonce\n')
                        if scenario == 'confirm_mode': a.confirm_file.chmod(0o644)
                        if scenario == 'confirm_symlink':
                            safe = d / 'safe-confirm'; a.confirm_file.rename(safe); a.confirm_file.symlink_to(safe)

            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(a.self_check_url, 403, 'Forbidden', {}, None)
            with mock.patch.object(cutover, 'run', side_effect=run), mock.patch.object(cutover.urllib.request, 'build_opener', return_value=opener), mock.patch.object(cutover.json, 'dump', side_effect=dump):
                result = cutover.execute(a, d / 'target', clock=lambda: now[0], sleep=sleep)
            expected = 'complete' if scenario == 'normal' else 'rolled_back'
            self.assertEqual(result['phase'], expected)
            self.assertEqual((d / 'target').read_bytes(), b'new' if scenario == 'normal' else b'old')
            self.assertEqual(a.state_file.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('fake-nonce', a.state_file.read_text())
            if scenario == 'normal':
                self.assertEqual(phases, ['armed', 'switched', 'self_check', 'awaiting_confirm', 'confirmed', 'watching', 'complete'])
                self.assertEqual(now[0], 330)
            if scenario == 'crash35':
                self.assertIn('awaiting_confirm', phases)
                self.assertNotIn('confirmed', phases)
            if scenario == 'pid_change':
                self.assertIn('watching', phases)

    def test_port_occupied(self): self.flow('port_occupied')
    def test_old_release_argv(self): self.flow('old_argv')
    def test_crash_at_35_after_stable_30(self): self.flow('crash35')
    def test_confirmation_timeout(self): self.flow('timeout')
    def test_confirmation_wrong_content(self): self.flow('wrong')
    def test_watch_pid_change(self): self.flow('pid_change')
    def test_normal_full_flow(self): self.flow('normal')
    def test_watch_listener_loss(self): self.flow('watch_loss')
    def test_watch_release_argv_change(self): self.flow('watch_argv')
    def test_confirmation_mode(self): self.flow('confirm_mode')
    def test_confirmation_symlink(self): self.flow('confirm_symlink')

    def test_unsafe_control_files(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); p = d / 'snapshot'; p.write_bytes(b'fake')
            for mode, valid in ((0o600, True), (0o644, True), (0o666, False), (0o700, False)):
                p.chmod(mode)
                if valid:
                    self.assertEqual(cutover.owned_file(p, {0o600, 0o644}, 100), b'fake')
                else:
                    with self.assertRaises(ValueError): cutover.owned_file(p, {0o600, 0o644}, 100)
            link = d / 'link'; link.symlink_to(p)
            with self.assertRaises(OSError): cutover.owned_file(link, {0o600, 0o644}, 100)
            with self.assertRaises(ValueError): cutover.owned_file(d, {0o600, 0o644}, 100)
            p.chmod(0o600)
            with mock.patch.object(cutover.os, 'geteuid', return_value=os.geteuid() + 1), self.assertRaises(ValueError):
                cutover.owned_file(p, {0o600, 0o644}, 100)
            p.chmod(0o644)
            with self.assertRaises(ValueError): cutover.owned_file(p, {0o600}, 100)

    def test_execute_requires_options_and_defaults(self):
        base = ['cutover', '--label', 'test.bridge', '--plist-new', '/unused/new', '--plist-snapshot', '/unused/old',
                '--bind', '127.0.0.1:12345', '--state-file', '/unused/state', '--execute']
        for extra in ([], ['--self-check-url', 'http://127.0.0.1:12345/health'],
                      ['--self-check-url', 'http://127.0.0.1:12345/health', '--expect-release', '/private/release']):
            with mock.patch.object(sys, 'argv', base + extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                cutover.main()
            self.assertEqual(error.exception.code, 2)
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'snapshot'
            p.write_bytes(plistlib.dumps({'Label': 'test.bridge'})); p.chmod(0o600)
            args = base[:-1]
            args[args.index('/unused/new')] = str(p); args[args.index('/unused/old')] = str(p)
            parsed = []
            parse = cutover.argparse.ArgumentParser.parse_args
            def capture(parser):
                result = parse(parser); parsed.append(result); return result
            with mock.patch.object(sys, 'argv', args), mock.patch.object(cutover, 'run') as run, mock.patch.object(cutover.argparse.ArgumentParser, 'parse_args', capture), contextlib.redirect_stdout(io.StringIO()):
                cutover.main()
            self.assertEqual(parsed[0].confirm_timeout, 600)
            self.assertEqual(parsed[0].post_confirm_watch, 300)
            run.assert_not_called()

    def test_rollback_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); (d / 'old').write_bytes(b'old'); (d / 'old').chmod(0o600)
            a = types.SimpleNamespace(plist_snapshot=d / 'old', label='test.bridge', state_file=d / 'state', delay=0, plist_new=d / 'missing')
            with mock.patch.object(cutover, 'run', side_effect=OSError):
                self.assertEqual(cutover.execute(a, d / 'target')['phase'], 'rollback_failed')


class Tokens(unittest.TestCase):
    def test_invalid_next_does_not_disable_current(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); current, nxt = d / 'current', d / 'next'
            current.write_text('fake-current'); current.chmod(0o600)
            rule = {'192.0.2.1': {'node': 'a', 'token_files': [str(current), str(nxt)]}}
            headers = email.message.Message(); headers['Authorization'] = 'Bearer fake-current'
            with mock.patch.object(T.B, 'ALLOWED_SOURCES', rule):
                for kind in ('mode', 'empty', 'symlink', 'directory'):
                    if nxt.is_symlink() or nxt.is_file(): nxt.unlink()
                    elif nxt.is_dir(): nxt.rmdir()
                    if kind == 'directory': nxt.mkdir()
                    elif kind == 'symlink': nxt.symlink_to(current)
                    else:
                        nxt.write_text('fake-next' if kind == 'mode' else ''); nxt.chmod(0o644 if kind == 'mode' else 0o600)
                    count = T.B.INVALID_NEXT_SLOTS
                    self.assertTrue(T.B.bearer_authorized(headers, '192.0.2.1'))
                    self.assertEqual(T.B.INVALID_NEXT_SLOTS, count + 1)
                    with self.assertRaises(RuntimeError): T.B.validate_startup_token()
                current.unlink()
                self.assertFalse(T.B.bearer_authorized(headers, '192.0.2.1'))

    def test_per_source_default_and_explicit_legacy(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'config.json'
            for setting, valid in ((None, False), (True, False), (False, True), ('true', False)):
                cfg = {'server': {'bind_host': '127.0.0.1', 'allow_loopback_for_tests': True},
                       'access': {'allowed_sources': [{'ip': '192.0.2.1', 'node': 'a'}]}}
                if setting is not None: cfg['server']['require_per_source_tokens'] = setting
                p.write_text(json.dumps(cfg))
                with mock.patch.object(C, 'config_path', return_value=p):
                    if valid: self.assertFalse(C.load()['server']['require_per_source_tokens'])
                    else:
                        with self.assertRaises(ValueError): C.load()
                if setting is None:
                    cfg['access']['allowed_sources'][0]['token_files'] = ['/private/current']
                    p.write_text(json.dumps(cfg))
                    with mock.patch.object(C, 'config_path', return_value=p):
                        self.assertTrue(C.load()['server']['require_per_source_tokens'])
            cfg['access']['allowed_sources'][0]['token_files'] = ['relative']
            p.write_text(json.dumps(cfg))
            with mock.patch.object(C, 'config_path', return_value=p), self.assertRaises(ValueError): C.load()

    def test_missing_next_path_reuse_config_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'config.json'
            cfg = json.loads(json.dumps(T.CONF))
            cfg['access']['allowed_sources'][0]['token_files'].append('/private/fake-not-created-next')
            cfg['access']['allowed_sources'][1]['token_files'].append('/private/fake-not-created-next')
            p.write_text(json.dumps(cfg))
            with mock.patch.object(C, 'config_path', return_value=p), self.assertRaises(ValueError): C.load()

    def test_duplicate_contents_and_paths_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / str(i) for i in range(2)]
            for p in paths: p.write_text('fake-duplicate'); p.chmod(0o600)
            for slots in ([str(paths[0]), str(paths[1])], [str(paths[0]), str(paths[0])]):
                for same_source in (True, False):
                    rules = {'a': {'token_files': slots}} if same_source else {'a': {'token_files': slots[:1]}, 'b': {'token_files': slots[1:]}}
                    with mock.patch.object(T.B, 'ALLOWED_SOURCES', rules), self.assertRaises(RuntimeError):
                        T.B.validate_startup_token()
            paths[1].write_text('fake-unique')
            with mock.patch.object(T.B, 'ALLOWED_SOURCES', {'a': {'token_files': list(map(str, paths))}}):
                T.B.validate_startup_token()

    def test_production_default_ignores_global_fallback(self):
        headers = email.message.Message(); headers['Authorization'] = 'Bearer test-key'
        self.assertTrue(T.B._SERVER['require_per_source_tokens'])
        with mock.patch.dict(os.environ, HARNESS_BRIDGE_TOKEN_FILE='/nonexistent/fake-token'):
            T.B.validate_startup_token()
            self.assertTrue(T.B.bearer_authorized(headers, '127.0.0.1'))
            self.assertFalse(T.B.bearer_authorized(headers, '192.0.2.10'))
            self.assertFalse(T.B.bearer_authorized(headers, 'unknown'))

    def test_legacy_fallback_reuse_rejected(self):
        with mock.patch.dict(T.B._SERVER, require_per_source_tokens=False), mock.patch.object(T.B, 'ALLOWED_SOURCES', {'a': {}, 'b': {}}), self.assertRaises(RuntimeError):
            T.B.validate_startup_token()

    def test_forced_command_curl_pinned(self):
        text = (ROOT / 'dispatcher/relay/forced-command.example.sh').read_text()
        self.assertIn('CURL=/usr/bin/curl', text)
        self.assertNotIn('${CURL', text)


class Provision(unittest.TestCase):
    def test_encrypt_parent_and_required_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); d.chmod(0o755)
            args = ['provision', 'encrypt', 'fake-public', str(d / 'token'), '--require-mode', 'sha256']
            with mock.patch.object(sys, 'argv', args), mock.patch.object(P, 'crypto') as crypto, self.assertRaises(ValueError): P.main()
            crypto.assert_not_called()
            d.chmod(0o700)
            with mock.patch.object(sys, 'argv', args), mock.patch.object(P.os, 'geteuid', return_value=os.geteuid() + 1), self.assertRaises(ValueError): P.main()
            with mock.patch.object(sys, 'argv', args), mock.patch.object(P.secrets, 'token_urlsafe', return_value='fake-test-token-only'), mock.patch.object(P, 'crypto', side_effect=subprocess.CalledProcessError(1, 'mock')) as crypto, self.assertRaises(subprocess.CalledProcessError): P.main()
            self.assertEqual(crypto.call_count, 1)

    def decrypt(self, d, token, extra=(), envelope=b'OAEP-SHA256\nZmFrZQ==\n'):
        with mock.patch.object(sys, 'argv', ['provision', 'decrypt', str(d / 'key'), str(d / 'output')] + list(extra)), mock.patch.object(P.sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(envelope))), mock.patch.object(P, 'crypto', return_value=token):
            P.main()

    def test_decrypt_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            for value in (b'', b'a' * 15, b'a' * 4097, b'a' * 16 + b' ', b'a' * 16 + b'\r\n', b'a' * 16 + b'\x80', b'a' * 16 + b'\n\n'):
                with self.assertRaises(ValueError): self.decrypt(d, value)
                self.assertFalse((d / 'output').exists())
            self.decrypt(d, b'fake-token-value-0000\n')
            self.assertEqual((d / 'output').stat().st_mode & 0o777, 0o600)

    def test_decrypt_required_mode_rejects_sha1(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            with self.assertRaises(ValueError): self.decrypt(d, b'fake-token-value-0000', ['--require-mode', 'sha256'], b'OAEP-SHA1\nZmFrZQ==\n')
            self.assertFalse((d / 'output').exists())

    def test_wipe_zero_fsync_truncate_unlink(self):
        for mode in ('--wipe-key', '--wipe-key=truncate-only'):
            with tempfile.TemporaryDirectory() as folder:
                d = Path(folder); key = d / 'key'; key.write_bytes(b'fake-private-key-material'); key.chmod(0o600)
                sizes = []
                fsync = os.fsync
                def sync(fd):
                    sizes.append(os.fstat(fd).st_size)
                    if len(sizes) == 1: self.assertEqual(key.read_bytes(), bytes(25))
                    fsync(fd)
                # Key bytes are a fake fixture, not a private key.
                original_size = key.stat().st_size
                with mock.patch.object(P.os, 'fsync', side_effect=sync):
                    self.decrypt(d, b'fake-token-value-0000', [mode])
                self.assertEqual(sizes, [original_size, 0])
                self.assertEqual(key.exists(), mode.endswith('truncate-only'))
                if key.exists(): self.assertEqual(key.read_bytes(), b'')


class Challenge(unittest.TestCase):
    def invoke(self, args):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['challenge'] + args), contextlib.redirect_stdout(output):
            code = H.main()
        return code, output.getvalue()

    def test_make_verify_and_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'token'; p.write_text('fake-test-token-only'); p.chmod(0o600)
            challenge = Path(folder) / 'challenge'
            base = ['verify', '--token-file', str(p), '--challenge-file', str(challenge)]
            def new():
                self.assertEqual(self.invoke(base + ['--new-challenge']), (0, ''))
                self.assertEqual(challenge.stat().st_mode & 0o777, 0o600)
                nonce = challenge.read_text().strip()
                self.assertEqual(len(bytes.fromhex(nonce)), 32)
                code, output = self.invoke(['make', '--token-file', str(p), '--nonce', nonce])
                self.assertEqual(code, 0)
                data = json.loads(output)
                self.assertEqual(data['nonce'], nonce)
                return data['mac']
            mac = new()
            self.assertEqual(self.invoke(base + ['--new-challenge']), (1, ''))
            with mock.patch.object(H.hmac, 'compare_digest', wraps=H.hmac.compare_digest) as compare:
                self.assertEqual(self.invoke(base + ['--mac', mac]), (0, '一致\n'))
                compare.assert_called_once()
            self.assertEqual(self.invoke(base + ['--mac', mac]), (1, '不一致\n'))
            mac = new(); p.write_text('fake-other-token-only')
            self.assertEqual(self.invoke(base + ['--mac', mac]), (1, '不一致\n'))
            for bad in ('not-hex', '00'):
                new()
                self.assertEqual(self.invoke(base + ['--mac', bad]), (1, '不一致\n'))
            new(); p.chmod(0o644)
            self.assertEqual(self.invoke(base + ['--mac', mac]), (1, '不一致\n'))
            self.assertEqual(self.invoke(['make', '--token-file', str(p), '--nonce', '00' * 32]), (1, ''))

    def test_same_file_rules_as_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); p = d / 'token'
            for value in (b'', b'fake valid internal space', b'\x80', b'x' * 4097):
                p.write_bytes(value); p.chmod(0o600)
                for read in (H.read_token, T.B.read_bearer_token):
                    with self.assertRaises(ValueError): read(p)
            p.write_bytes(b'fake-test\n'); p.chmod(0o600)
            self.assertEqual(H.read_token(p), T.B.read_bearer_token(p))
            link = d / 'link'; link.symlink_to(p)
            for read in (H.read_token, T.B.read_bearer_token):
                with self.assertRaises(OSError): read(link)
                with mock.patch.object(os, 'geteuid', return_value=os.geteuid() + 1), self.assertRaises(ValueError): read(p)
