"""Unit tests for harness-bridge (run from bridge/: python3 -B -m unittest discover -s tests -v).

Uses only RFC 5737 / loopback addresses and *.example.ts.net names. No network access beyond 127.0.0.1.
"""
import atexit
import shutil
import http.server
import json
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
TMP = pathlib.Path(tempfile.mkdtemp(dir=HERE.parents[1]))
atexit.register(shutil.rmtree, TMP)
(TMP / 'ws').mkdir()
(TMP / 'bridge.token').write_text('test-key\n')
(TMP / 'bridge.token').chmod(0o600)
CONF = {
    'server': {'bind_host': '127.0.0.1', 'port': 0, 'allow_loopback_for_tests': True, 'host_label': 'mac-test',
               'journal': str(TMP / 'jobs.json'), 'webhook_conf': str(TMP / 'webhook.conf'),
               'token_file': str(TMP / 'bridge.token'), 'tailscale_bin': '/nonexistent/tailscale'},
    'workspace': {'path': str(TMP / 'ws')},
    'codex': {'bin': '/opt/test/codex', 'home': str(TMP / 'codex-home')},
    'claude': {'bin': 'claude', 'path_prepend': ['/opt/test/bin']},
    'access': {'allowed_sources': [
        {'ip': '192.0.2.10', 'tag': 'tag:dispatcher'},
        {'ip': '192.0.2.20', 'node': 'peer-a.example.ts.net'},
        {'ip': '127.0.0.1', 'tag': 'tag:dispatcher'},
    ]},
}
(TMP / 'config.json').write_text(json.dumps(CONF))
os.environ['HARNESS_BRIDGE_CONFIG'] = str(TMP / 'config.json')

import agent_adapters as A  # noqa: E402
import codex_bridge as B  # noqa: E402

DEFAULT_CODEX = ['/opt/test/codex', 'exec', '--skip-git-repo-check', '--json',
                 '--sandbox', 'danger-full-access', '-c', 'approval_policy="never"', '-C', str(TMP / 'ws')]


def whois(name='', tags=None):
    return types.SimpleNamespace(stdout=json.dumps({'Node': {'Name': name, 'Tags': tags or []}}))


class Adapters(unittest.TestCase):
    def test_default_codex_command(self):
        self.assertEqual(A.command_for('mac-codex'), DEFAULT_CODEX + ['-'])
        self.assertEqual(A.command_for('mac-codex', 'abc'), DEFAULT_CODEX + ['resume', 'abc', '-'])

    def test_codex_model_and_effort(self):
        self.assertEqual(A.command_for('mac-codex', None, 'gpt-6-astra', 'medium'),
                         DEFAULT_CODEX + ['-m', 'gpt-6-astra', '-c', 'model_reasoning_effort="medium"', '-'])
        cmd = A.command_for('mac-codex', 'sid', 'gpt-6-astra:medium')
        self.assertLess(cmd.index('-m'), cmd.index('resume'))
        self.assertEqual(cmd[-3:], ['resume', 'sid', '-'])

    def test_rejects(self):
        bad = [('gpt-6-astra; rm -rf /', None), ('unknown-model', None), ('gpt-6-astra', 'turbo'),
               ('gpt-6-luna', 'ultra'), ('gpt-6-astra:low', 'high'), ('', None), (5, None), (None, 3), ('-c', None)]
        for model, effort in bad:
            with self.subTest(model=model, effort=effort), self.assertRaises(A.ModelError):
                A.normalize_selection('mac-codex', model, effort)
        with self.assertRaises(A.ModelError):
            A.normalize_selection('other-backend', 'gpt-6-astra')

    def test_claude_flag_mode(self):
        cmd = A.command_for('mac-claudecode', 's1', 'claude-opus-5-5')
        self.assertEqual(cmd[-4:], ['--model', 'claude-opus-5-5', '--resume', 's1'])
        self.assertIn('export PATH=/opt/test/bin:"$PATH"', cmd[2])
        self.assertNotIn('--model', A.command_for('mac-claudecode'))
        for bad in (('claude opus', None), ('--dangerous', None), ('claude-opus-5-5', 'high'), ('x;y', None)):
            with self.subTest(bad=bad), self.assertRaises(A.ModelError):
                A.normalize_selection('mac-claudecode', *bad)

    def test_claude_switch_mode_quotes_model(self):
        with mock.patch.dict(A._CLAUDE, model_mode='switch', switch_helper='/opt/test/switch run.sh'):
            cmd = A.command_for('mac-claudecode', None, 'some model')
            self.assertNotIn('--model', cmd)
            self.assertIn("'/opt/test/switch run.sh' export 'some model'", cmd[2])
            with self.assertRaises(A.ModelError):
                A.normalize_selection('mac-claudecode', 'a;b')

    def test_claude_env_script(self):
        with mock.patch.dict(A._CLAUDE, env_script='/opt/test/env.zsh'):
            script = A.command_for('mac-claudecode')[2]
            self.assertIn("source /opt/test/env.zsh || exit 78", script)
            self.assertIn(A.CLAUDE_ENV_MISSING, script)

    def test_parse_codex_and_claude(self):
        out = '\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started', 'thread_id': 't1'},
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'notice'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
            {'type': 'turn.completed', 'usage': {}}])
        r = A.parse_events('codex', out)
        self.assertEqual((r['session_id'], r['result'], r['failed'], r['warnings']), ('t1', 'done', False, ['notice']))
        c = A.parse_events('claude', 'banner\n' + json.dumps({'type': 'result', 'session_id': 's', 'result': 'ok',
                                                             'permission_denials': [], 'modelUsage': {'m1': {}}}))
        self.assertEqual((c['session_id'], c['result'], c['failed'], c['models']), ('s', 'ok', False, ['m1']))


class Capture(http.server.BaseHTTPRequestHandler):
    seen = []
    status = 200

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        Capture.seen.append((self.path, dict(self.headers), json.loads(body)))
        if Capture.status in (301, 302, 307):
            self.send_response(Capture.status)
            self.send_header('Location', '/elsewhere')
        else:
            self.send_response(Capture.status)
        self.send_header('Content-Length', '0')
        self.end_headers()


class Webhook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Capture)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:%d/hook' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Capture.seen.clear()
        Capture.status = 200

    def write_conf(self, text):
        B.WEBHOOK_CONF.write_text(text)

    def tearDown(self):
        if B.WEBHOOK_CONF.exists():
            B.WEBHOOK_CONF.unlink()

    def test_auth_sent_verbatim(self):
        self.write_conf(f'# comment\nWEBHOOK_URL={self.url}\nWEBHOOK_AUTH=Bearer test-key\n')
        payload = {'job_id': 'j', 'status': 'completed', 'backend': 'mac-codex', 'host': 'mac-test'}
        B.post_terminal_webhook(payload)
        self.assertEqual(len(Capture.seen), 1)
        path, headers, body = Capture.seen[0]
        self.assertEqual(headers['Authorization'], 'Bearer test-key')
        self.assertEqual(body, payload)

    def test_missing_conf_is_silent(self):
        B.post_terminal_webhook({'job_id': 'j'})
        self.assertEqual(Capture.seen, [])

    def test_unknown_key_rejected_and_not_logged(self):
        self.write_conf(f'WEBHOOK_URL={self.url}\nEXTRA=1\nWEBHOOK_AUTH=Bearer secret-value\n')
        with mock.patch('builtins.print') as p:
            B.post_terminal_webhook({'job_id': 'j'})
        self.assertEqual(Capture.seen, [])
        logged = ' '.join(str(a) for call in p.call_args_list for a in call.args)
        self.assertIn('Invalid', logged.replace('ValueError', 'Invalid'))
        self.assertNotIn('secret-value', logged)
        self.assertNotIn('127.0.0.1', logged)

    def test_redirect_not_followed(self):
        Capture.status = 307
        self.write_conf(f'WEBHOOK_URL={self.url}\nWEBHOOK_AUTH=Bearer k\n')
        with mock.patch('builtins.print'):
            B.post_terminal_webhook({'job_id': 'j'})
        self.assertEqual([s[0] for s in Capture.seen], ['/hook'])

    def test_notify_skips_non_terminal_and_uses_label(self):
        with mock.patch.object(B, 'post_terminal_webhook') as post, mock.patch('threading.Thread') as T:
            B.notify_terminal({'job_id': 'j', 'status': 'running'})
            T.assert_not_called()
            B.notify_terminal({'job_id': 'j', 'status': 'failed', 'backend': 'mac-claudecode'})
            args = T.call_args.kwargs['args'][0]
            self.assertEqual(args, {'job_id': 'j', 'status': 'failed', 'backend': 'mac-claudecode', 'host': 'mac-test'})
            self.assertTrue(T.call_args.kwargs['daemon'])


class Authorization(unittest.TestCase):
    def test_tag_required(self):
        with mock.patch('subprocess.run', return_value=whois('x.example.ts.net.', ['tag:dispatcher'])):
            self.assertTrue(B.authorized('192.0.2.10'))
        with mock.patch('subprocess.run', return_value=whois('x.example.ts.net.', ['tag:other'])):
            self.assertFalse(B.authorized('192.0.2.10'))

    def test_node_name(self):
        with mock.patch('subprocess.run', return_value=whois('peer-a.example.ts.net.')):
            self.assertTrue(B.authorized('192.0.2.20'))
        with mock.patch('subprocess.run', return_value=whois('peer-b.example.ts.net.')):
            self.assertFalse(B.authorized('192.0.2.20'))

    def test_unlisted_skips_whois(self):
        with mock.patch('subprocess.run') as run:
            self.assertFalse(B.authorized('192.0.2.99'))
            self.assertFalse(B.authorized('not-an-ip'))
            run.assert_not_called()

    def test_whois_failure_denies(self):
        with mock.patch('subprocess.run', side_effect=OSError('boom')):
            self.assertFalse(B.authorized('192.0.2.10'))


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), B.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json', 'Host': f'{B.HOST}:{B.PORT}',
                                              'Authorization': 'Bearer test-key'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_403_without_tag(self):
        with mock.patch('subprocess.run', return_value=whois('x', ['tag:other'])):
            self.assertEqual(self.call('GET', '/health')[0], 403)

    def test_health_and_submit(self):
        with mock.patch('subprocess.run', return_value=whois('x', ['tag:dispatcher'])), \
                mock.patch.object(B.POOL, 'submit') as submit:
            status, health = self.call('GET', '/health')
            self.assertEqual((status, health['host']), (200, 'mac-test'))
            status, job = self.call('POST', '/jobs', {'backend': 'mac-codex', 'prompt': 'hi',
                                                      'model': 'gpt-6-astra:medium'})
            self.assertEqual((status, job['status']), (202, 'queued'))
            submit.assert_called_once()
            self.assertEqual(self.call('POST', '/jobs', {'prompt': 'x', 'cwd': '/'})[0], 400)
            self.assertEqual(self.call('POST', '/jobs', {'prompt': 'x', 'backend': 'other'})[0], 400)
            status, got = self.call('GET', '/jobs/' + job['job_id'])
            self.assertEqual((got['model'], got['effort'], got['model_source']), ('gpt-6-astra', 'medium', 'request'))


if __name__ == '__main__':
    unittest.main()
