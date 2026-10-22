"""Patch 4: fake credentials, temporary files, mocked listeners and HTTP."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

import bridge_config as C
from test_patch1 import cutover
from test_patch2 import H


class SandboxValidation(unittest.TestCase):
    def load(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'config.json'
            p.write_text(json.dumps({'server': {'bind_host': '127.0.0.1',
                'allow_loopback_for_tests': True}, 'codex': {'sandbox': mode}}))
            with mock.patch.object(C, 'config_path', return_value=p):
                return C.load()

    def test_known_modes_and_full_access_warning(self):
        for mode in ('read-only', 'workspace-write'):
            with mock.patch.object(C.logging, 'warning') as warning:
                self.assertEqual(self.load(mode)['codex']['sandbox'], mode)
                warning.assert_not_called()
        with self.assertLogs(level='WARNING') as logs:
            self.assertEqual(self.load('danger-full-access')['codex']['sandbox'], 'danger-full-access')
        self.assertEqual(len(logs.output), 1)
        self.assertIn('danger-full-access', logs.output[0])

    def test_unknown_or_non_string_modes_refused(self):
        for mode in ('', 'unknown', None, True, [], {}):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'codex.sandbox'):
                self.load(mode)


class CutoverObservations(unittest.TestCase):
    def test_retry_spacing_and_no_sleep_after_last_failure(self):
        for results, expected_sleeps in (([OSError(), OSError(), 'ok'], 2),
                                         ([OSError(), 'ok'], 1),
                                         ([OSError(), OSError(), OSError()], 2)):
            with mock.patch.object(cutover, 'run', side_effect=results), \
                 mock.patch.object(cutover.time, 'sleep') as sleep:
                if isinstance(results[-1], str):
                    self.assertEqual(cutover.lsof_output(12345), 'ok')
                else:
                    with self.assertRaises(OSError): cutover.lsof_output(12345)
                self.assertEqual(sleep.call_args_list, [mock.call(1)] * expected_sleeps)

    def test_http_status_separate_from_health(self):
        for code, via_error in ((200, False), (403, False), (403, True), (500, True)):
            now = [0]
            def sleep(n): now[0] += n
            opener = mock.MagicMock()
            if via_error:
                opener.open.side_effect = urllib.error.HTTPError('http://127.0.0.1:12345/health', code, 'fake', {}, None)
            else:
                opener.open.return_value.__enter__.return_value.status = code
            with mock.patch.object(cutover, 'listener', return_value='123'), \
                 mock.patch.object(cutover.urllib.request, 'build_opener', return_value=opener):
                result = cutover.check('127.0.0.1', 12345, 'http://127.0.0.1:12345/health',
                    clock=lambda: now[0], sleep=sleep)
            self.assertEqual(result['http_status'], code)
            self.assertEqual(cutover.success(result), code == 403)

    def test_no_http_response_status_is_null(self):
        now = [0]
        def sleep(n): now[0] += n
        with mock.patch.object(cutover, 'listener', side_effect=ValueError('fake')):
            result = cutover.check('127.0.0.1', 12345, clock=lambda: now[0], sleep=sleep)
        self.assertIsNone(result['http_status'])
        self.assertFalse(cutover.success(result))


class ChallengeRetention(unittest.TestCase):
    def invoke(self, args):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['challenge'] + args), contextlib.redirect_stdout(output):
            code = H.main()
        return code, output.getvalue()

    def test_truncate_match_mismatch_and_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); token = d / 'token'; token.write_text('fake-test-token-only'); token.chmod(0o600)
            challenge = d / 'challenge'
            base = ['verify', '--token-file', str(token), '--challenge-file', str(challenge)]
            nonce = '00' * 32
            _, output = self.invoke(['make', '--token-file', str(token), '--nonce', nonce])
            mac = json.loads(output)['mac']
            for candidate, expected in ((mac, (0, '一致\n')), ('00' * 32, (1, '不一致\n')), ('bad', (1, '不一致\n'))):
                challenge.write_text(nonce + '\n'); challenge.chmod(0o600)
                inode = challenge.stat().st_ino
                with mock.patch.object(H.os, 'fsync', wraps=os.fsync) as fsync:
                    self.assertEqual(self.invoke(base + ['--mac', candidate, '--truncate-only']), expected)
                    fsync.assert_called_once()
                self.assertEqual(challenge.stat().st_ino, inode)
                self.assertEqual(challenge.stat().st_mode & 0o777, 0o600)
                self.assertEqual(challenge.read_bytes(), b'')
                self.assertEqual(self.invoke(base + ['--mac', candidate, '--truncate-only']), (1, '不一致\n'))
            self.assertEqual(token.read_text(), 'fake-test-token-only')

    def test_truncate_unsafe_challenge_refused_without_modification(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder); p = d / 'challenge'; p.write_text('00' * 32); p.chmod(0o644)
            link = d / 'link'; link.symlink_to(p)
            for source in (p, link):
                args = ['verify', '--token-file', str(d / 'unused'), '--challenge-file', str(source),
                        '--mac', '00' * 32, '--truncate-only']
                self.assertEqual(self.invoke(args), (1, '不一致\n'))
                self.assertEqual(p.read_text(), '00' * 32)

    def test_new_challenge_rejects_truncate_option(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.invoke(['verify', '--token-file', '/unused', '--challenge-file', '/unused',
                         '--new-challenge', '--truncate-only'])
        self.assertEqual(error.exception.code, 2)
