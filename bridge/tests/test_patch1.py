"""Patch 1: offline tests; no launchctl operations or external requests."""
import email.message
import contextlib
import io
import sys
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock
import test_bridge as T
import bridge_config as C

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cutover', ROOT / 'ops/macos/cutover.py')
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


def _source_groups(rules):
    return {ip: [dict(rule, name=ip)] for ip, rule in rules.items()}


class Patch1(unittest.TestCase):
    def test_bind_validation(self):
        for host, opt, valid in [('0.0.0.0', True, False), ('::', True, False),
                                 ('192.0.2.1', False, False), ('127.0.0.1', False, False),
                                 ('127.0.0.1', True, True), (str(ipaddress.ip_network('100.64.0.0/10')[1]), False, True),
                                 (str(ipaddress.ip_network('100.64.0.0/10')[-1]), False, True), ('100.128.0.1', False, False)]:
            config = {'server': {'bind_host': host, 'allow_loopback_for_tests': opt}}
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / 'config.json'
                p.write_text(json.dumps(config))
                with mock.patch.object(C, 'config_path', return_value=p):
                    if valid:
                        C.load()
                    else:
                        with self.assertRaises(ValueError):
                            C.load()

    def test_source_slots_rotation_revocation(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [Path(d) / str(i) for i in range(3)]
            for p, value in zip(paths, ['source-a', 'next-a', 'source-b']):
                p.write_text(value)
                p.chmod(0o600)
            rules = {'192.0.2.1': {'node': 'a', 'token_files': list(map(str, paths[:2]))},
                     '192.0.2.2': {'node': 'b', 'token_files': [str(paths[2])]}}
            def auth(value, source):
                headers = email.message.Message()
                headers['Authorization'] = 'Bearer ' + value
                return T.B.bearer_authorized(headers, source)
            with mock.patch.object(T.B, 'ALLOWED_SOURCES', _source_groups(rules)):
                T.B.validate_startup_token()
                for value in ('source-a', 'next-a'):
                    self.assertTrue(auth(value, '192.0.2.1'))
                    self.assertFalse(auth(value, '192.0.2.2'))
                self.assertFalse(auth('source-b', '192.0.2.1'))
                with mock.patch.object(T.B.hmac, 'compare_digest', wraps=T.B.hmac.compare_digest) as compare:
                    self.assertTrue(auth('source-a', '192.0.2.1'))
                    self.assertEqual(compare.call_count, 2)
                paths[1].unlink()
                T.B.validate_startup_token()
                paths[0].unlink()
                self.assertFalse(auth('source-a', '192.0.2.1'))
                self.assertTrue(auth('source-b', '192.0.2.2'))
                with self.assertRaises(RuntimeError):
                    T.B.validate_startup_token()

    def test_cutover_lsof_stability(self):
        now = [0]
        def sleep(n): now[0] += n
        output = 'p123\nn127.0.0.1:12345\n'
        with mock.patch.object(cutover, 'run', return_value=output):
            self.assertTrue(cutover.success(cutover.check('127.0.0.1', 12345, clock=lambda: now[0], sleep=sleep)))
        now[0] = 0
        with mock.patch.object(cutover, 'run', return_value='p123\nn*:12345\n'):
            self.assertFalse(cutover.success(cutover.check('127.0.0.1', 12345, clock=lambda: now[0], sleep=sleep)))

    def test_cutover_rollback_and_state(self):
        for failure in (False, True):
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                new, old, target, state = [d / n for n in ('new', 'old', 'target', 'state')]
                new.write_bytes(b'new'); old.write_bytes(b'old')
                args = types.SimpleNamespace(label='test.bridge', plist_new=new, plist_snapshot=old,
                                             host='127.0.0.1', port=12345, self_check_url=None,
                                             state_file=state, delay=0, expect_release='/private/release',
                                             confirm_file=d / 'confirm', confirm_nonce='fake-nonce', confirm_timeout=0, post_confirm_watch=0)
                def publish(*args, **kwargs):
                    (d / 'confirm').write_text('fake-nonce\n'); (d / 'confirm').chmod(0o600)
                    return checks.pop(0)
                checks = [{'listener': not failure, 'pid': '123'}, {'listener': True}]
                with mock.patch.object(cutover, 'run') as run, mock.patch.object(cutover, 'check', side_effect=publish), mock.patch.object(cutover, 'listener', return_value='123'):
                    result = cutover.execute(args, target)
                self.assertEqual(target.read_bytes(), b'old' if failure else b'new')
                self.assertEqual(result['rolled_back'], failure)
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                self.assertEqual(run.call_args_list[0].args[0][1], 'bootout')

    def test_token_roundtrip_exclusive_and_wipe(self):
        scripts = ROOT / 'ops/token-provision'
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); d.chmod(0o700)
            public = subprocess.check_output([str(scripts / 'gen_keypair.sh'), str(d)])
            (d / 'public.pem').write_bytes(public)
            spec = importlib.util.spec_from_file_location('provision_fake', scripts / 'provision.py')
            provision = importlib.util.module_from_spec(spec); spec.loader.exec_module(provision)
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['provision', 'encrypt', str(d / 'public.pem'), str(d / 'token')]), mock.patch.object(provision.secrets, 'token_urlsafe', return_value='fake-test-token-only-000000000000'), contextlib.redirect_stdout(output):
                provision.main()
            envelope = output.getvalue().encode()
            subprocess.run([str(scripts / 'decrypt_store.sh'), str(d / 'private.pem'), str(d / 'stored'), '--wipe-key'], input=envelope, check=True, capture_output=True)
            self.assertEqual((d / 'token').read_bytes(), (d / 'stored').read_bytes())
            self.assertEqual((d / 'stored').stat().st_mode & 0o777, 0o600)
            self.assertFalse((d / 'private.pem').exists())
            with mock.patch.object(sys, 'argv', ['provision', 'encrypt', str(d / 'public.pem'), str(d / 'token')]), mock.patch.object(provision.secrets, 'token_urlsafe', return_value='fake-test-token-only-000000000000'), self.assertRaises(FileExistsError):
                provision.main()

    def test_cutover_bootstrap_failure_restores(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name in ('new', 'old'):
                (d / name).write_text(name)
            args = types.SimpleNamespace(label='test.bridge', plist_new=d / 'new', plist_snapshot=d / 'old',
                                         host='127.0.0.1', port=12345, self_check_url=None, state_file=d / 'state', delay=0)
            with mock.patch.object(cutover, 'run', side_effect=['', subprocess.CalledProcessError(1, 'bootstrap'), '', '']), mock.patch.object(cutover, 'check', return_value={'listener': True}):
                state = cutover.execute(args, d / 'target')
            self.assertEqual(state['phase'], 'rolled_back')
            self.assertEqual((d / 'target').read_text(), 'old')

    def test_cutover_self_check_403(self):
        import urllib.error
        now = [0]
        def sleep(n): now[0] += n
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('http://127.0.0.1:12345/health', 403, 'Forbidden', {}, None)
        with mock.patch.object(cutover, 'run', return_value='p123\nn127.0.0.1:12345\n'), mock.patch.object(cutover.urllib.request, 'build_opener', return_value=opener):
            self.assertTrue(cutover.success(cutover.check('127.0.0.1', 12345, 'http://127.0.0.1:12345/health', clock=lambda: now[0], sleep=sleep)))

    def test_slots_config_validation(self):
        for slots in ([], ['one', 'two', 'three'], '', [None]):
            with tempfile.TemporaryDirectory() as d:
                config = dict(T.CONF)
                config['access'] = {'allowed_sources': [{'ip': '192.0.2.1', 'node': 'a', 'token_files': slots}]}
                p = Path(d) / 'config.json'; p.write_text(json.dumps(config))
                with mock.patch.object(C, 'config_path', return_value=p), self.assertRaises(ValueError):
                    C.load()

    def test_oaep_sha1_legacy_default(self):
        spec = importlib.util.spec_from_file_location('provision', ROOT / 'ops/token-provision/provision.py')
        provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provision)
        self.assertNotIn('rsa_oaep_md:sha1', provision.options('sha1'))
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / 'private.pem'
            private = provision.crypto(['genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072'])
            key.write_bytes(private); key.chmod(0o600)
            public = Path(d) / 'public.pem'
            public.write_bytes(provision.crypto(['pkey', '-pubout'], private))
            encrypted = provision.crypto(['pkeyutl', '-encrypt', '-pubin', '-inkey', str(public)] + provision.options('sha1'), b'fake-legacy-value')
            self.assertEqual(provision.crypto(['pkeyutl', '-decrypt', '-inkey', str(key)] + provision.options('sha1'), encrypted), b'fake-legacy-value')

    def test_cutover_never_stable_pid(self):
        now = [0]
        def sleep(n): now[0] += n
        def output(command, **kwargs): return 'p%d\nn127.0.0.1:12345\n' % (now[0] + 1)
        with mock.patch.object(cutover, 'run', side_effect=output):
            self.assertFalse(cutover.success(cutover.check('127.0.0.1', 12345, clock=lambda: now[0], sleep=sleep)))
