"""Regression tests for review F1-F6; synthetic CLI data and loopback only."""
import http.server
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import test_bridge as T

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('direct_ssh_client', ROOT / 'dispatcher/direct_ssh_client.py')
S = importlib.util.module_from_spec(spec)
# Do not inspect the operator's real dispatcher configuration.
with mock.patch.dict(os.environ, HARNESS_TARGETS=str(ROOT / 'nonexistent-test-targets.json')):
    spec.loader.exec_module(S)


def events(*items):
    return '\n'.join(json.dumps(item) for item in items)


class OutputProtocol(unittest.TestCase):
    def test_empty_incomplete_and_malformed(self):
        for text in ('', 'banner', '[]', events({'type': 'thread.started', 'thread_id': 's'}),
                     events({'type': 'item.completed', 'item': None}),
                     events({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 7}})):
            with self.subTest(text=text):
                self.assertTrue(T.A.parse_events('codex', text)['failed'])
                with self.assertRaises(ValueError):
                    S.parse('codex', text)
        for text in ('', 'banner', '[]', events({'type': 'result', 'session_id': 's', 'result': []})):
            self.assertTrue(T.A.parse_events('claude', text)['failed'])
            with self.assertRaises(ValueError):
                S.parse('claude', text)

    def test_terminal_with_empty_body_is_valid(self):
        codex = events({'type': 'thread.started', 'thread_id': 's'},
                       {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 0}})
        claude = events({'type': 'result', 'subtype': 'success', 'session_id': 's',
                         'result': '', 'is_error': False, 'permission_denials': []})
        for engine, text in (('codex', codex), ('claude', claude)):
            self.assertFalse(T.A.parse_events(engine, text)['failed'])
            self.assertFalse(S.parse(engine, text)['failed'])
        self.assertTrue(S.parse('claude', events({'type': 'result', 'subtype': 'error_during_execution',
                                                'session_id': 's'}))['failed'])

    def test_worker_failure_persisted_and_notified(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder, mock.patch.object(S, 'STATE', pathlib.Path(folder)), \
                mock.patch.object(S, 'TARGETS', {'test': ('h', 'u', '/', 'codex', 'codex', 'key', None)}), \
                mock.patch.object(S, 'command', return_value=['fake']), \
                mock.patch.object(S.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[]', '')), \
                mock.patch.object(S, 'send_wake', return_value={'ok': True}) as wake:
            jid = str(uuid.uuid4())
            path = S.job_path(jid)
            S.write(path, {'job_id': jid, 'backend': 'test', 'status': 'queued'})
            S.write(pathlib.Path(folder) / (jid + '.request'), {'prompt': 'test', 'wake': {'test': True}})
            S.worker(jid)
            job = json.loads(path.read_text())
            self.assertEqual(job['status'], 'failed')
            self.assertIn('ValueError', job['error'])
            wake.assert_called_once()


class WakePolicy(unittest.TestCase):
    def test_origin_and_path(self):
        with mock.patch.object(S, 'WAKE_URL_PREFIX', 'https://hooks.example/hook'), \
                mock.patch.object(S, 'WAKE_KEY_ENV', 'HARNESS_WAKE_KEY'):
            def validate(url, key='HARNESS_WAKE_KEY'):
                return S.validate_wake({'url': url, 'key_env': key})
            for url in ('https://hooks.example/hook', 'https://HOOKS.example:443/hook/child'):
                self.assertEqual(validate(url)['url'], url)
            for url in ('https://hooks.example.attacker.example/hook', 'https://hooks.example:444/hook',
                        'http://hooks.example/hook', 'https://user@hooks.example/hook',
                        'https://hooks.example/hook-other', 'https://hooks.example/hook#fragment',
                        'https://hooks.example/hook?x=1', 'https://hooks.example:0/hook'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    validate(url)
            with self.assertRaises(ValueError):
                validate('https://hooks.example/hook', 'OTHER_SECRET')
            with mock.patch.object(S, 'WAKE_URL_PREFIX', ''), self.assertRaises(ValueError):
                validate('https://hooks.example/hook')

    def test_cross_origin_redirects_never_reach_destination(self):
        seen = []
        class Destination(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
            do_POST = do_GET
        target = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Destination)
        class Redirect(Destination):
            code = 302
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(self.code)
                self.send_header('Location', 'http://127.0.0.1:%d/collect' % target.server_port)
                self.end_headers()
        source = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        for server in (target, source):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            wake = {'url': 'http://127.0.0.1:%d/hook' % source.server_port, 'key_env': 'HARNESS_WAKE_KEY'}
            # Bypass only HTTPS validation to exercise the production opener over loopback.
            with mock.patch.object(S, 'validate_wake', side_effect=lambda w: w), \
                    mock.patch.dict(os.environ, HARNESS_WAKE_KEY='test-key'):
                for code in (301, 302, 303, 307, 308):
                    Redirect.code = code
                    self.assertEqual(S.send_wake({'job_id': 'test', 'backend': 'test', 'status': 'failed'}, wake),
                                     {'ok': False, 'http': code, 'attempts': 1})
                    self.assertEqual(seen, [])
        finally:
            for server in (source, target):
                server.shutdown()
                server.server_close()


class PrivateFilesAndProcesses(unittest.TestCase):
    def test_poller_and_ledger_under_umask_022(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            base = pathlib.Path(folder)
            curl = base / 'curl'
            curl.write_text('#!/usr/bin/env python3\nimport json,sys\np=sys.argv[sys.argv.index("-o")+1]\n'
                            'open(p,"w").write(json.dumps({"status":"completed","job_id":"test","result":"private"}))\n'
                            'print("200",end="")\n')
            curl.chmod(0o700)
            for timeout in (False, True):
                if timeout:
                    curl.write_text(curl.read_text().replace('"completed"', '"running"'))
                output, ledger = base / 'result.json', base / 'ledger.jsonl'
                ledger.write_text('')
                ledger.chmod(0o644)
                env = dict(os.environ, HARNESS_HOME=folder, HARNESS_HOSTS_FILE=str(base / 'absent'),
                           HARNESS_LEDGER=str(ledger), PATH=folder + os.pathsep + os.environ['PATH'])
                run = subprocess.run(['bash', '-c', 'umask 022; exec bash "$@"', 'test',
                                      str(ROOT / 'dispatcher/bin/mac_wait.sh'), '127.0.0.1:1',
                                      str(uuid.uuid4()), str(output), '--max', '0'],
                                     env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 2 if timeout else 0, run.stderr)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                if not timeout:
                    self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(base.glob('result.json.tmp.*')), [])

    def test_codex_timeout_kills_descendant(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            marker, ready = pathlib.Path(folder) / 'marker', pathlib.Path(folder) / 'ready'
            child = 'import time,pathlib; pathlib.Path(%r).touch(); time.sleep(0.8); pathlib.Path(%r).touch()' % (str(ready), str(marker))
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",%r]); time.sleep(30)' % child
            jid = str(uuid.uuid4())
            T.B.JOBS[jid] = {'job_id': jid, 'status': 'queued'}
            try:
                with mock.patch.object(T.B, 'command_for', return_value=[sys.executable, '-c', parent]), \
                        mock.patch.object(T.B, 'JOB_TIMEOUT', 0.4), mock.patch.object(T.B, 'save'), \
                        mock.patch.object(T.B, 'notify_terminal') as notify:
                    T.B.run_job(jid, 'test', None)
                self.assertTrue(ready.exists(), 'child must start before timeout')
                self.assertEqual(T.B.JOBS[jid]['status'], 'failed')
                notify.assert_called_once()
                time.sleep(1)
                self.assertFalse(marker.exists(), 'descendant survived the process group timeout')
            finally:
                T.B.JOBS.pop(jid, None)
