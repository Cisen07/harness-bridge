"""Offline regression coverage for the registry and Cursor backfill."""
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
                    'BOT_A_WAKE_URL': 'https://hooks.example.invalid/wake/aaaabbbbcccc',
                    'BOT_A_WAKE_KEY': 'synthetic-test-key'}

    def run_wake(self, bot='BotA', *args, request=None):
        return subprocess.run([sys.executable, str(WAKE), bot, *args], env=self.env,
                              input=json.dumps(request) if request is not None else None,
                              text=True, capture_output=True)

    def test_all_aliases_and_no_key_value_in_output(self):
        for alias, name in [('a', 'BotA'), ('BOTB', 'BotB'), ('legacy', 'LegacyBot')]:
            ent = self.data['bots'][name]
            self.env[ent['url_env']] = self.env['BOT_A_WAKE_URL']
            self.env[ent['key_env']] = 'synthetic-test-key'
            r = self.run_wake(alias, '--json')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)['bot'], name)
            self.assertNotIn('synthetic-test-key', r.stdout + r.stderr)

    def test_inject_is_compatible_with_dispatcher_wake_policy(self):
        req = {'backend': 'ubuntu-a-codex', 'prompt': 'ping'}
        r = self.run_wake('a', '--inject', request=req)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(set(out), {'backend', 'prompt', 'wake'})
        with mock.patch.object(S, 'WAKE_URL_PREFIX', self.env['HARNESS_WAKE_URL_PREFIX']), \
                mock.patch.object(S, 'WAKE_KEY_ENV', 'BOT_A_WAKE_KEY'):
            self.assertEqual(S.validate_wake(out['wake']), out['wake'])
        for foreign in ({}, {'url': 'https://hooks.example.invalid/wake/foreign', 'key_env': 'OTHER'}):
            req['wake'] = foreign
            self.assertEqual(self.run_wake('BotA', '--inject', request=req).returncode, 2)

    def test_registry_override_and_url_file_fallback(self):
        path = self.home / 'custom.json'
        self.registry.rename(path)
        self.env['HARNESS_WAKE_REGISTRY'] = str(path)
        url = self.env.pop('BOT_A_WAKE_URL')
        (self.home / 'wake/bota.url').write_text(url + '\n')
        r = self.run_wake()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['url'], url)

    def test_collision_ambiguous_and_legacy_cross_use(self):
        self.data['bots']['BotB']['url_fp'] = {'last12': 'aaaabbbbcccc'}
        self.registry.write_text(json.dumps(self.data))
        self.assertIn('collides', self.run_wake().stderr)
        self.data['bots']['BotB'].pop('url_fp')
        self.registry.write_text(json.dumps(self.data))
        self.assertIn('ambiguous', self.run_wake('Bot').stderr)
        self.env['WEBHOOK_URL'] = self.env['BOT_A_WAKE_URL']
        self.assertIn('refusing legacy URL', self.run_wake().stderr)

    def test_legacy_url_needs_real_key_alias(self):
        self.env['WEBHOOK_URL'] = self.env['BOT_A_WAKE_URL']
        self.env['WEBHOOK_AUTH'] = 'Bearer fake-key'
        r = self.run_wake('LegacyBot')
        self.assertEqual(r.returncode, 2)
        self.assertNotIn('synthetic-test-key', r.stdout + r.stderr)
        self.env['LEGACY_WAKE_KEY'] = 'synthetic-test-key'
        r = self.run_wake('LegacyBot')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['key_env'], 'LEGACY_WAKE_KEY')

    def test_registry_controls_aliases_and_legacy_after_rename(self):
        entry = self.data['bots'].pop('LegacyBot')
        entry['aliases'] = ['retired-endpoint']
        entry['legacy_env']['url'] = 'OLD_BOT_URL'
        self.data['bots']['RenamedBot'] = entry
        self.registry.write_text(json.dumps(self.data))
        self.env['OLD_BOT_URL'] = 'https://hooks.example.invalid/wake/legacy'
        self.env['LEGACY_WAKE_KEY'] = 'synthetic-test-key'
        r = self.run_wake('RETIRED-ENDPOINT', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['bot'], 'RenamedBot')
        self.assertEqual(self.run_wake('legacy').returncode, 2)
        self.env['BOT_A_WAKE_URL'] = self.env['OLD_BOT_URL']
        self.assertIn('refusing legacy URL', self.run_wake().stderr)
        for opt_in in (False, None, 'true'):
            entry['allow_legacy_env'] = opt_in
            self.registry.write_text(json.dumps(self.data))
            self.assertEqual(self.run_wake('retired-endpoint').returncode, 2)
        entry.pop('allow_legacy_env')
        self.registry.write_text(json.dumps(self.data))
        self.assertEqual(self.run_wake('retired-endpoint').returncode, 2)

    def test_alias_collisions_fail_in_both_registry_orders(self):
        for alias in ('A', 'bota'):
            self.data['bots']['BotB']['aliases'] = [alias]
            for reverse in (False, True):
                self.data['bots'] = dict(sorted(self.data['bots'].items(), reverse=reverse))
                self.registry.write_text(json.dumps(self.data))
                r = self.run_wake('a')
                self.assertEqual(r.returncode, 2)
                self.assertIn('ambiguous', r.stderr)

    def test_url_boundary_and_missing_config(self):
        for url in ('http://hooks.example.invalid/wake/a',
                    'https://hooks.example.invalid.attacker.invalid/wake/a',
                    'https://hooks.example.invalid/wake-other/a',
                    'https://user@hooks.example.invalid/wake/a',
                    'https://hooks.example.invalid/wake/a?key=synthetic-test-key'):
            self.env['BOT_A_WAKE_URL'] = url
            r = self.run_wake()
            self.assertEqual(r.returncode, 2)
            self.assertNotIn('synthetic-test-key', r.stdout + r.stderr)
        self.env['BOT_A_WAKE_URL'] = 'https://hooks.example.invalid/wake/a'
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
