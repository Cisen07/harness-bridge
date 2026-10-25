"""Offline regression coverage for the TW registry and Cursor backfill."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WAKE = ROOT / 'dispatcher/bin/wake_for_bot.py'
REGISTRY = ROOT / 'dispatcher/bin/registry.example.json'
spec = importlib.util.spec_from_file_location('cursor_client', ROOT / 'dispatcher/direct_ssh_client.py')
S = importlib.util.module_from_spec(spec)
with mock.patch.dict(os.environ, HARNESS_TARGETS=str(ROOT / 'nonexistent-test-targets.json')):
    spec.loader.exec_module(S)


class RegistryWake(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'wake').mkdir()
        self.registry = self.home / 'wake/registry.json'
        self.data = json.loads(REGISTRY.read_text())
        self.registry.write_text(json.dumps(self.data))
        self.env = {'PATH': os.environ.get('PATH', ''), 'HARNESS_HOME': str(self.home),
                    'HARNESS_WAKE_URL_PREFIX': 'https://hooks.example.invalid/wake',
                    'TW_WAKE_URL': 'https://hooks.example.invalid/wake/tw0123456789',
                    'TW_WAKE_KEY': 'synthetic-test-key'}

    def run_wake(self, bot='TW', *args, request=None):
        return subprocess.run([sys.executable, str(WAKE), bot, *args], env=self.env,
                              input=json.dumps(request) if request is not None else None,
                              text=True, capture_output=True)

    def test_all_aliases_and_no_key_value_in_output(self):
        for alias, name in [('Ada', 'Ada'), ('TW', 'TW Grok Bot'), ('TF', 'TF Grok Bot'),
                            ('Air', 'Air Grok Bot'), ('MBP', 'MBP Grok Bot')]:
            ent = self.data['bots'][name]
            self.env[ent['url_env']] = self.env['TW_WAKE_URL']
            self.env[ent['key_env']] = 'synthetic-test-key'
            r = self.run_wake(alias, '--json')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)['bot'], name)
            self.assertNotIn('synthetic-test-key', r.stdout + r.stderr)

    def test_inject_is_compatible_with_dispatcher_wake_policy(self):
        req = {'backend': 'tw-cursor', 'prompt': 'ping'}
        r = self.run_wake('tw', '--inject', request=req)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(set(out), {'backend', 'prompt', 'wake'})
        with mock.patch.object(S, 'WAKE_URL_PREFIX', self.env['HARNESS_WAKE_URL_PREFIX']), \
                mock.patch.object(S, 'WAKE_KEY_ENV', 'TW_WAKE_KEY'):
            self.assertEqual(S.validate_wake(out['wake']), out['wake'])
        for foreign in ({}, {'url': 'https://hooks.example.invalid/wake/foreign', 'key_env': 'OTHER'}):
            req['wake'] = foreign
            self.assertEqual(self.run_wake('TW', '--inject', request=req).returncode, 2)

    def test_registry_override_and_url_file_fallback(self):
        path = self.home / 'custom.json'
        self.registry.rename(path)
        self.env['HARNESS_WAKE_REGISTRY'] = str(path)
        url = self.env.pop('TW_WAKE_URL')
        (self.home / 'wake/tw_webhook_url').write_text(url + '\n')
        r = self.run_wake()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['url'], url)

    def test_collision_ambiguous_and_legacy_cross_use(self):
        self.data['bots']['Ada']['url_fp'] = {'last12': 'tw0123456789'}
        self.registry.write_text(json.dumps(self.data))
        self.assertIn('collides', self.run_wake().stderr)
        self.data['bots']['Ada'].pop('url_fp')
        self.registry.write_text(json.dumps(self.data))
        self.assertIn('ambiguous', self.run_wake('T').stderr)
        self.env['WEBHOOK_URL'] = self.env['TW_WAKE_URL']
        self.assertIn('refusing WEBHOOK_URL', self.run_wake().stderr)

    def test_mbp_legacy_url_needs_real_key_alias(self):
        self.env['WEBHOOK_URL'] = self.env['TW_WAKE_URL']
        self.env['WEBHOOK_AUTH'] = 'Bearer fake-key'
        r = self.run_wake('MBP')
        self.assertEqual(r.returncode, 2)
        self.assertNotIn('synthetic-test-key', r.stdout + r.stderr)
        self.env['MBP_WAKE_KEY'] = 'synthetic-test-key'
        r = self.run_wake('MBP')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['key_env'], 'MBP_WAKE_KEY')

    def test_url_boundary_and_missing_config(self):
        for url in ('http://hooks.example.invalid/wake/a',
                    'https://hooks.example.invalid.attacker.invalid/wake/a',
                    'https://hooks.example.invalid/wake-other/a',
                    'https://user@hooks.example.invalid/wake/a',
                    'https://hooks.example.invalid/wake/a?key=synthetic-test-key'):
            self.env['TW_WAKE_URL'] = url
            r = self.run_wake()
            self.assertEqual(r.returncode, 2)
            self.assertNotIn('synthetic-test-key', r.stdout + r.stderr)
        self.env['TW_WAKE_URL'] = 'https://hooks.example.invalid/wake/a'
        self.env.pop('HARNESS_WAKE_URL_PREFIX')
        self.assertEqual(self.run_wake().returncode, 2)


class CursorDispatch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.target = ('host.invalid', 'user', '/work tree', '/opt/bin/agent', 'cursor', 'key', '/unused-switch')
        for name, value in [('TARGETS', {'tw-cursor': self.target}), ('STATE', self.state), ('SSH_PROXY', 'none')]:
            patch = mock.patch.object(S, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_command_model_and_resume(self):
        session = str(uuid.uuid4())
        cmd = S.command('tw-cursor', session, 'test-model')
        shell = shlex.split(cmd[-1])[2]
        self.assertIn("cd -- '/work tree'", shell)
        args = shlex.split(shell.split('exec ', 1)[1])
        self.assertEqual(args, ['/opt/bin/agent', '-p', '--output-format', 'json', '--force',
                                '--trust', '--sandbox', 'disabled', '--resume', session,
                                '--model', 'test-model'])
        self.assertNotIn('unused-switch', shell)
        default = S.command('tw-cursor')[-1]
        self.assertNotIn('--model', default)
        self.assertNotIn('--resume', default)

    def test_example_load_and_capabilities(self):
        self.assertEqual(S.load_targets(ROOT / 'dispatcher/targets.example.json')['tw-cursor'][4], 'cursor')
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['client', 'targets']), contextlib.redirect_stdout(out):
            S.main()
        self.assertTrue(json.loads(out.getvalue())['tw-cursor']['supports_model'])

    def submit(self, **extra):
        req = dict(backend='tw-cursor', prompt='ping', **extra)
        with mock.patch.object(sys, 'argv', ['client', 'submit']), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(req))), \
                contextlib.redirect_stdout(io.StringIO()), mock.patch.object(S, 'submit_job') as submit:
            S.main()
            return submit.call_args.args

    def test_model_without_switch_helper_and_effort_rejection(self):
        with mock.patch.object(S, 'TARGETS', {'tw-cursor': self.target[:-1] + (None,)}):
            req, job = self.submit(model='test-model')
            self.assertEqual(job['model'], req['model'])
        with self.assertRaisesRegex(ValueError, 'only supported for codex'):
            self.submit(effort='medium')
        with self.assertRaisesRegex(ValueError, 'Invalid model'):
            self.submit(model='bad;command')

    def test_result_and_worker_fail_closed(self):
        session = str(uuid.uuid4())
        event = dict(type='result', subtype='success', is_error=False, result='OK', session_id=session)
        parsed = S.parse('cursor', json.dumps(event))
        self.assertEqual((parsed['session_id'], parsed['result'], parsed['failed']), (session, 'OK', False))
        for text in ('', 'unstructured output', '[]', '{"type":"system"}',
                     json.dumps(dict(event, result=[]))):
            with self.subTest(text=text), self.assertRaises(ValueError):
                S.parse('cursor', text)
        self.assertTrue(S.parse('cursor', json.dumps(dict(event, is_error=True)))['failed'])
        for stdout, expected in [(json.dumps(event), 'completed'), ('unstructured output', 'failed'),
                                 (json.dumps(dict(event, session_id=str(uuid.uuid4()))), 'failed')]:
            jid = str(uuid.uuid4())
            S.write(S.job_path(jid), dict(job_id=jid, backend='tw-cursor', status='queued'))
            S.write(self.state / (jid + '.request'), dict(prompt='ping', session_id=session))
            with mock.patch.object(S.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout, '')):
                S.worker(jid)
            self.assertEqual(json.loads(S.job_path(jid).read_text())['status'], expected)


if __name__ == '__main__':
    unittest.main()
