"""Patch 3 regressions: temporary control files, fake tokens, mocked OS commands/time."""
import concurrent.futures
import contextlib
import email.message
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import test_bridge as T
import bridge_config as C
from test_patch1 import cutover


class Confirmation(unittest.TestCase):
    def flow(self, style, complete=True):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            for name in ('new', 'old'):
                (d / name).write_text(name); (d / name).chmod(0o600)
            p = d / 'confirm'
            a = types.SimpleNamespace(label='test.bridge', plist_new=d / 'new', plist_snapshot=d / 'old',
                host='127.0.0.1', port=12345, self_check_url='http://127.0.0.1:12345/health',
                state_file=d / 'state', delay=0, expect_release='/fake/release', confirm_file=p,
                confirm_nonce='fake-nonce', confirm_timeout=6, post_confirm_watch=0)
            now, phases = [0], []
            def write(value, mode=0o600):
                p.write_bytes(value); p.chmod(mode)
            dump = json.dump
            def save(state, stream):
                phases.append(state['phase']); dump(state, stream)
                if state['phase'] == 'awaiting_confirm':
                    if style == 'empty': write(b'')
                    elif style == 'chmod': write(b'fake-nonce\n', 0o644)
                    elif style == 'partial': write(b'fake-')
                    elif style == 'no_newline': write(b'fake-nonce')
                    elif style == 'short_line': write(b'x\n')
                    elif style == 'wrong': write(b'wrong-nonc\n')
                    elif style == 'multiline': write(b'fake-nonce\nextra\n')
                    elif style == 'symlink': p.symlink_to(d / 'new')
                    elif style == 'directory': p.mkdir()
            def sleep(seconds):
                now[0] += seconds
                if complete and now[0] == 2:
                    if style == 'chmod': p.chmod(0o600)
                    elif style == 'rename':
                        temporary = d / '.confirmation.tmp'
                        temporary.write_bytes(b'fake-nonce\n'); temporary.chmod(0o600)
                        os.replace(temporary, p)
                    else: write(b'fake-nonce\n')
            with mock.patch.object(cutover, 'run') as run, \
                 mock.patch.object(cutover, 'listener', return_value='123'), \
                 mock.patch.object(cutover, 'check', return_value={'listener': True, 'pid': '123'}), \
                 mock.patch.object(cutover.json, 'dump', side_effect=save):
                result = cutover.execute(a, d / 'target', clock=lambda: now[0], sleep=sleep)
            self.assertEqual(result['phase'], 'complete' if complete else 'rolled_back')
            self.assertEqual((d / 'target').read_text(), 'new' if complete else 'old')
            if not complete:
                self.assertNotIn('confirmed', phases)
            return now[0]

    def test_empty_then_write(self): self.assertEqual(self.flow('empty'), 2)
    def test_write_then_chmod(self): self.assertEqual(self.flow('chmod'), 2)
    def test_atomic_rename(self): self.assertEqual(self.flow('rename'), 2)
    def test_partial_then_complete(self): self.assertEqual(self.flow('partial'), 2)
    def test_newline_added_later(self): self.assertEqual(self.flow('no_newline'), 2)
    def test_short_line_then_complete(self): self.assertEqual(self.flow('short_line'), 2)
    def test_wrong_complete_content_rolls_back_immediately(self): self.assertEqual(self.flow('wrong', False), 0)
    def test_multiline_rolls_back_immediately(self): self.assertEqual(self.flow('multiline', False), 0)
    def test_symlink_rolls_back_immediately(self): self.assertEqual(self.flow('symlink', False), 0)
    def test_directory_rolls_back_immediately(self): self.assertEqual(self.flow('directory', False), 0)
    def test_incomplete_waits_until_timeout(self):
        for style in ('empty', 'chmod', 'partial', 'no_newline', 'short_line', 'missing'):
            with self.subTest(style=style): self.assertEqual(self.flow(style, False), 6)

    def test_wrong_owner_immediate(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'confirm'; p.write_bytes(b''); p.chmod(0o644)
            with mock.patch.object(cutover.os, 'geteuid', return_value=os.geteuid() + 1):
                with self.assertRaises(ValueError): cutover.confirmation(p, 'fake-nonce')

    def test_existing_confirmation_refuses_execute_before_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'confirm'
            for style in ('regular', 'directory', 'dangling_symlink'):
                if style == 'regular': p.write_text('')
                elif style == 'directory': p.mkdir()
                else: p.symlink_to(Path(folder) / 'missing')
                with mock.patch.object(cutover, 'run') as run:
                    with self.assertRaisesRegex(ValueError, 'already exists'):
                        cutover.execute(types.SimpleNamespace(confirm_file=p), Path(folder) / 'target')
                    run.assert_not_called()
                if p.is_dir(): p.rmdir()
                else: p.unlink()

    def test_existing_confirmation_refuses_cli_before_fork(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'confirm'; p.touch()
            args = ['cutover', '--execute', '--label', 'test.bridge', '--plist-new', '/unused/new',
                '--plist-snapshot', '/unused/old', '--bind', '127.0.0.1:12345', '--state-file', '/unused/state',
                '--self-check-url', 'http://127.0.0.1:12345/health', '--expect-release', '/fake/release',
                '--confirm-file', str(p), '--confirm-nonce', 'fake-nonce']
            with mock.patch.object(sys, 'argv', args), mock.patch.object(cutover.os, 'fork') as fork, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                cutover.main()
            self.assertEqual(error.exception.code, 2); fork.assert_not_called()


class OptionalFixes(unittest.TestCase):
    def test_two_transient_lsof_failures_are_tolerated(self):
        output = 'p123\nn127.0.0.1:12345\n'
        for count in (1, 2):
            with mock.patch.object(cutover, 'run', side_effect=[OSError('fake')] * count + [output]) as run:
                self.assertEqual(cutover.listener('127.0.0.1', 12345), '123')
                self.assertEqual(run.call_count, count + 1)

    def test_third_lsof_failure_raises(self):
        with mock.patch.object(cutover, 'run', side_effect=OSError('fake')) as run:
            with self.assertRaises(OSError): cutover.listener('127.0.0.1', 12345)
            self.assertEqual(run.call_count, 3)

    def test_unsafe_lsof_output_is_not_retried(self):
        with mock.patch.object(cutover, 'run', return_value='p123\nn*:12345\n') as run:
            with self.assertRaises(ValueError): cutover.listener('127.0.0.1', 12345)
            run.assert_called_once()

    def test_old_health_200_does_not_mark_rollback_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); (d / 'old').write_text('old'); (d / 'old').chmod(0o600)
            a = types.SimpleNamespace(label='test.bridge', plist_snapshot=d / 'old', plist_new=d / 'missing',
                state_file=d / 'state', delay=0, host='127.0.0.1', port=12345,
                self_check_url='http://127.0.0.1:12345/health')
            now = [0]
            def sleep(n): now[0] += n
            def run(command):
                if command[0].endswith('lsof'): return 'p123\nn127.0.0.1:12345\n'
                if command[1] == 'print': return 'pid = 123\n'
                return ''
            opener = mock.MagicMock(); opener.open.return_value.__enter__.return_value.status = 200
            with mock.patch.object(cutover, 'run', side_effect=run), \
                 mock.patch.object(cutover.urllib.request, 'build_opener', return_value=opener):
                result = cutover.execute(a, d / 'target', clock=lambda: now[0], sleep=sleep)
            self.assertEqual(result['phase'], 'rollback_failed')
            self.assertFalse(result['checks']['rollback']['self_check'])
            self.assertNotIn('pid', result['checks']['rollback'])
            self.assertEqual(json.loads(a.state_file.read_text())['phase'], 'rollback_failed')

    def test_counter_concurrent_requests_use_lock(self):
        headers = email.message.Message(); headers['Authorization'] = 'Bearer fake-current'
        def read(path):
            if path == 'next': raise ValueError('fake invalid slot')
            return b'fake-current'
        before = T.B.INVALID_NEXT_SLOTS
        lock = mock.MagicMock(wraps=T.B.COUNTER_LOCK)
        # Retain a real lock for the concurrent calls, and count entry through a wrapper.
        lock.__enter__.side_effect = T.B.COUNTER_LOCK.__enter__
        lock.__exit__.side_effect = T.B.COUNTER_LOCK.__exit__
        with mock.patch.object(T.B, 'COUNTER_LOCK', lock), \
             mock.patch.object(T.B, 'read_bearer_token', side_effect=read), \
             mock.patch.object(T.B, 'ALLOWED_SOURCES', {'a': {'token_files': ['current', 'next']}}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                self.assertTrue(all(pool.map(lambda _: T.B.bearer_authorized(headers, 'a'), range(200))))
        self.assertEqual(T.B.INVALID_NEXT_SLOTS - before, 200)
        self.assertEqual(lock.__enter__.call_count, 200)


class Sandbox(unittest.TestCase):
    def test_default_and_explicit_migration_override(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'config.json'
            for mode in (None, 'danger-full-access'):
                config = {'server': {'bind_host': '127.0.0.1', 'allow_loopback_for_tests': True}}
                if mode: config['codex'] = {'sandbox': mode}
                p.write_text(json.dumps(config))
                with mock.patch.object(C, 'config_path', return_value=p): cfg = C.load()
                expected = mode or 'workspace-write'
                self.assertEqual(cfg['codex']['sandbox'], expected)
                self.assertEqual(cfg['codex']['approval_policy'], 'never')
                with mock.patch.object(T.B, 'CONFIG', cfg):
                    self.assertEqual(T.B.codex_mode_summary(), 'codex sandbox=' + expected + '; approval_policy=never')
