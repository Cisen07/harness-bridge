"""Regression tests for review F1-F6; synthetic CLI data and loopback only."""
import http.client
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


def _source_groups(rules):
    return {ip: [dict(rule, name=ip)] for ip, rule in rules.items()}


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
                           HARNESS_LEDGER=str(ledger), HARNESS_BRIDGE_TOKEN_FILE=str(T.TMP / 'bridge.token'), PATH=folder + os.pathsep + os.environ['PATH'])
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


class HttpBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), T.B.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, extra=None, omit=(), method='POST'):
        headers = {'Host': f'{T.B.HOST}:{T.B.PORT}', 'Content-Type': 'application/json',
                   'Authorization': 'Bearer test-key'}
        headers.update(extra or {})
        for key in omit:
            headers.pop(key, None)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            with mock.patch.object(T.B, 'authorized', return_value=True), mock.patch.object(T.B.POOL, 'submit'):
                conn.request(method, '/jobs' if method == 'POST' else '/health',
                             body=b'{"prompt":"test"}' if method == 'POST' else None, headers=headers)
                response = conn.getresponse()
                response.read()
                return response.status
        finally:
            conn.close()

    def test_json_required(self):
        for headers, omit in (({}, ('Content-Type',)), ({'Content-Type': 'text/plain'}, ())):
            self.assertEqual(self.call(headers, omit), 415)
        self.assertEqual(self.call({'Content-Type': 'application/json; charset=utf-8'}), 202)

    def test_browser_headers_forbidden_even_empty(self):
        for name in ('Origin', 'Sec-Fetch-Site'):
            for value in ('', 'same-origin', 'https://attacker.example'):
                self.assertEqual(self.call({name: value}), 403)

    def test_exact_host_required(self):
        for host in ('attacker.example', 'localhost:18542', T.B.HOST):
            self.assertEqual(self.call({'Host': host}), 403)
        self.assertEqual(self.call(), 202)

    def test_bearer_required_for_read_and_write(self):
        for method in ('GET', 'POST'):
            self.assertEqual(self.call(omit=('Authorization',), method=method), 403)
            for auth in ('Bearer wrong', 'Basic test-key', 'Bearer test-key extra'):
                self.assertEqual(self.call({'Authorization': auth}, method=method), 403)
            self.assertEqual(self.call(method=method), 200 if method == 'GET' else 202)

    @mock.patch.dict(T.B._SERVER, require_per_source_tokens=False)
    @mock.patch.object(T.B, 'ALLOWED_SOURCES', _source_groups({'127.0.0.1': {}}))
    def test_token_files_fail_closed_and_opt_out(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = pathlib.Path(folder) / 'credential'
            with mock.patch.dict(os.environ, HARNESS_BRIDGE_TOKEN_FILE=str(path)):
                self.assertEqual(self.call(), 403)
                for content, mode in (('', 0o600), ('test-key', 0o644), ('bad\nheader', 0o600)):
                    path.write_text(content)
                    path.chmod(mode)
                    self.assertEqual(self.call(), 403)
                path.write_text('test-key\n')
                path.chmod(0o600)
                self.assertEqual(self.call(), 202)
                with mock.patch.dict(T.B._SERVER, require_token=False):
                    path.unlink()
                    self.assertEqual(self.call(omit=('Authorization',)), 202)

    @mock.patch.dict(T.B._SERVER, require_per_source_tokens=False)
    @mock.patch.object(T.B, 'ALLOWED_SOURCES', _source_groups({'127.0.0.1': {}}))
    def test_startup_requires_valid_token_before_listening(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = pathlib.Path(folder) / 'credential'
            legacy_config = json.loads(json.dumps(T.CONF))
            legacy_config['server']['require_per_source_tokens'] = False
            legacy_config['access']['allowed_sources'] = [{'ip': '127.0.0.1', 'node': 'fake-test'}]
            config_path = pathlib.Path(folder) / 'legacy.json'
            config_path.write_text(json.dumps(legacy_config))
            env = dict(os.environ, HARNESS_BRIDGE_TOKEN_FILE=str(path), HARNESS_BRIDGE_CONFIG=str(config_path))
            for mode in (None, 0o644):
                if mode is not None:
                    path.write_text('test-key\n')
                    path.chmod(mode)
                result = subprocess.run([sys.executable, '-B', str(ROOT / 'bridge/codex_bridge.py')],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot start: Bearer token file', result.stderr)
                self.assertNotIn('listening on', result.stdout)
                self.assertNotIn('test-key', result.stderr)
            path.chmod(0o600)
            with mock.patch.dict(os.environ, HARNESS_BRIDGE_TOKEN_FILE=str(path)):
                T.B.validate_startup_token()
                path.unlink()
                with mock.patch.dict(T.B._SERVER, require_token=False):
                    T.B.validate_startup_token()

    def test_read_timeout(self):
        self.assertEqual(T.B.Handler.timeout, 30)


class AdditionalPolicies(unittest.TestCase):
    def test_webhook_transport_and_allowlist(self):
        for url in ('http://127.0.0.1/hook', 'http://[::1]/hook', 'https://hooks.example/hook'):
            T.B.validate_webhook_url(url)
        import ipaddress
        tailnet = str(ipaddress.ip_network('100.64.0.0/10').network_address + 1)
        T.B.validate_webhook_url('http://' + tailnet + '/hook')
        for url in ('http://hooks.example/hook', 'http://192.0.2.1/hook', 'ftp://hooks.example/hook',
                    'https://user@hooks.example/hook', 'https://hooks.example/hook/../other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                T.B.validate_webhook_url(url)
        with mock.patch.dict(T.B._SERVER, webhook_url_prefix='https://hooks.example/hook'):
            T.B.validate_webhook_url('https://HOOKS.example:443/hook/child')
            for url in ('https://evil.example/hook', 'https://hooks.example/hook-other',
                        'https://hooks.example:444/hook', 'https://hooks.example/hook/%2e%2e/other'):
                with self.assertRaises(ValueError):
                    T.B.validate_webhook_url(url)

    def test_wake_rejects_ambiguous_paths(self):
        with mock.patch.object(S, 'WAKE_URL_PREFIX', 'https://hooks.example/hook'):
            for path in ('../other', '%2e%2e/other', 'child;other', 'a..b'):
                with self.assertRaises(ValueError):
                    S.validate_wake({'url': 'https://hooks.example/hook/' + path, 'key_env': S.WAKE_KEY_ENV})

    def test_ssh_destination_and_proxy_quoting(self):
        target = ('host.example', 'user', '/', 'codex', 'codex', 'key', None)
        with mock.patch.object(S, 'TARGETS', {'test': target}), mock.patch.object(S, 'BASE', pathlib.Path('/space dir')):
            args = S.ssh_args('test', 'true')
            self.assertEqual(args[-3:], ['--', 'user@host.example', 'true'])
            self.assertIn("ProxyCommand='/space dir/bin/tailscale' --socket='/space dir/run/tailscaled.sock' nc %h %p", args)
        for host, user in (('-oProxyCommand=evil', 'user'), ('host', 'user@evil'), ('host;evil', 'user')):
            with mock.patch.object(S, 'TARGETS', {'test': (host, user, *target[2:])}), self.assertRaises(ValueError):
                S.ssh_args('test', 'true')

    def test_dispatcher_token_helper(self):
        spec = importlib.util.spec_from_file_location('http_auth', ROOT / 'dispatcher/bin/_bridge_auth.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = pathlib.Path(folder) / 'credential'
            with mock.patch.dict(os.environ, HARNESS_BRIDGE_TOKEN_FILE=str(path), HARNESS_BRIDGE_REQUIRE_TOKEN='1'):
                with self.assertRaises(OSError):
                    module.auth_header()
                for text, mode in (('', 0o600), ('test-key', 0o644), ('bad\nheader', 0o600)):
                    path.write_text(text)
                    path.chmod(mode)
                    with self.assertRaises(ValueError):
                        module.auth_header()
                path.write_text('test-key\n')
                path.chmod(0o600)
                self.assertEqual(module.auth_header(), 'Authorization: Bearer test-key')
                with mock.patch.dict(os.environ, HARNESS_BRIDGE_REQUIRE_TOKEN='0'):
                    self.assertEqual(module.auth_header(), '')

    def test_summary_hides_denied_tool_input(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = pathlib.Path(folder) / 'job.json'
            path.write_text(json.dumps({'permission_denials': [{'tool_name': 'Bash', 'tool_input': 'private-value'},
                                                             'private-value']}))
            result = subprocess.run([sys.executable, str(ROOT / 'dispatcher/bin/_job_summary.py'),
                                     str(path), 'mac', 'test', 'test'], capture_output=True, text=True, check=True)
            self.assertIn('Bash', result.stdout)
            self.assertNotIn('private-value', result.stdout)

    def test_relay_streams_auth_and_json_without_secret_in_argv(self):
        # Replace only SSH/timeout with local adapters; exercise the real remote shell and curl.
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), T.B.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(dir=ROOT) as folder:
                base = pathlib.Path(folder)
                (base / 'ssh').write_text('#!/usr/bin/env python3\nimport os,sys\n'
                                         'assert not any("test-key" in arg for arg in sys.argv)\n'
                                         'assert sys.argv[-3] == "--"\n'
                                         'os.execl("/bin/sh", "sh", "-c", sys.argv[-1])\n')
                (base / 'timeout').write_text('#!/usr/bin/env python3\nimport os,sys\n'
                                             'os.execvp(sys.argv[2],sys.argv[2:])\n')
                for name in ('ssh', 'timeout'):
                    (base / name).chmod(0o700)
                env = dict(os.environ, PATH=folder + os.pathsep + os.environ['PATH'],
                           HARNESS_HOME=folder, HARNESS_HOSTS_FILE=str(base / 'absent'),
                           HARNESS_BRIDGE_REQUIRE_TOKEN='1',
                           HARNESS_BRIDGE_TOKEN_FILE=str(T.TMP / 'bridge.token'),
                           RELAY_SSH_TARGET='user@host.example', RELAY_SSH_KEY='unused',
                           RELAY_TARGET_HOSTPORT='127.0.0.1:%d' % server.server_port)
                with mock.patch.object(T.B, 'authorized', return_value=True), \
                        mock.patch.object(T.B, 'PORT', server.server_port), mock.patch.object(T.B.POOL, 'submit') as submit:
                    for method, path, body in (('GET', '/health', ''), ('POST', '/jobs', '{"prompt":"relay test"}')):
                        result = subprocess.run(['bash', str(ROOT / 'dispatcher/relay/relay_via_host.sh'), method, path],
                                                input=body, env=env, text=True, capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        response = json.loads(result.stdout)
                        self.assertTrue(response.get('ok') or response.get('status') == 'queued', response)
                        self.assertNotIn('test-key', result.stdout + result.stderr)
                    submit.assert_called_once()
                env['HARNESS_BRIDGE_TOKEN_FILE'] = str(base / 'missing')
                result = subprocess.run(['bash', str(ROOT / 'dispatcher/relay/relay_via_host.sh'), 'GET', '/health'],
                                        env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)
        finally:
            server.shutdown()
            server.server_close()

    @mock.patch.dict(T.B._SERVER, require_per_source_tokens=False)
    @mock.patch.object(T.B, 'ALLOWED_SOURCES', _source_groups({'127.0.0.1': {}}))
    def test_duplicate_auth_and_unsafe_file_rejected(self):
        from email.message import Message
        headers = Message()
        headers.add_header('Authorization', 'Bearer test-key')
        with mock.patch.object(T.B.hmac, 'compare_digest', wraps=T.B.hmac.compare_digest) as compare:
            self.assertTrue(T.B.bearer_authorized(headers, '127.0.0.1'))
            compare.assert_called_once()
        headers.add_header('Authorization', 'Bearer test-key')
        self.assertFalse(T.B.bearer_authorized(headers, '127.0.0.1'))
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            link = pathlib.Path(folder) / 'link'
            link.symlink_to(T.TMP / 'bridge.token')
            with mock.patch.dict(os.environ, HARNESS_BRIDGE_TOKEN_FILE=str(link)):
                self.assertFalse(T.B.bearer_authorized(headers, '127.0.0.1'))
