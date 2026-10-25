"""P1 shadow routing: suggest-only, never reject submit."""
import importlib.util
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / 'dispatcher' / 'shadow_policy.example.json'


def load_shadow():
    path = ROOT / 'dispatcher' / 'shadow_route.py'
    spec = importlib.util.spec_from_file_location('shadow_route_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = load_shadow()


class ShadowPolicy(unittest.TestCase):
    def test_example_policy_loads_and_weights(self):
        policy = S.load_policy(POLICY)
        self.assertEqual(policy['schema_version'], 1)
        self.assertEqual(sum(t['weight_pct'] for t in policy['tiers']), 100)
        by_id = {t['id']: t for t in policy['tiers']}
        self.assertEqual(by_id['astra']['weight_pct'], 20)
        self.assertEqual(by_id['sol']['weight_pct'], 50)
        self.assertEqual(by_id['deepseek']['weight_pct'], 30)
        self.assertEqual(policy['deny_targets'], [])
        self.assertEqual(by_id['astra']['model'], 'gpt-6-astra')
        self.assertEqual(policy['hard_bindings']['ada']['effort'], 'medium')


class ShadowSuggest(unittest.TestCase):
    def setUp(self):
        self.policy = S.load_policy(POLICY)

    def test_named_target_recorded(self):
        out = S.suggest_route(
            backend='ubuntu-a-codex',
            engine='codex',
            model='gpt-6.1-sol',
            effort='high',
            named_model=True,
            named_effort=True,
            policy=self.policy,
            rng=random.Random(0),
        )
        self.assertEqual(out['selection'], 'named')
        self.assertIn(S.REASON_NAMED, out['reasons'])
        self.assertNotIn(S.REASON_HARD_BINDING_CONFLICT, out['reasons'])
        self.assertEqual(out['suggested']['model'], 'gpt-6.1-sol')
        self.assertEqual(out['suggested']['effort'], 'high')
        self.assertTrue(out['match'])

    def test_ada_named_conflict_records_only(self):
        out = S.suggest_route(
            backend='ubuntu-a-codex',
            engine='codex',
            model='gpt-6.1-sol',
            effort='high',
            bot='Ada',
            named_model=True,
            named_effort=True,
            policy=self.policy,
            rng=random.Random(0),
        )
        self.assertEqual(out['selection'], 'ada_hard_binding')
        self.assertIn(S.REASON_ADA_HARD_BINDING, out['reasons'])
        self.assertIn(S.REASON_HARD_BINDING_CONFLICT, out['reasons'])
        self.assertEqual(out['suggested']['model'], 'gpt-6-astra')
        self.assertEqual(out['suggested']['effort'], 'medium')
        self.assertEqual(out['actual']['model'], 'gpt-6.1-sol')
        self.assertFalse(out['match'])
        # Shadow never encodes a reject action (notes may say "no reject").
        self.assertEqual(out['mode'], 'shadow')
        self.assertNotIn('action', out)
        self.assertTrue(any('no reject' in n for n in out['notes']))

    def test_ada_backend_without_bot_name_still_hard_binds(self):
        out = S.suggest_route(
            backend='tw-tl-codex',
            engine='codex',
            model=None,
            effort=None,
            named_model=False,
            named_effort=False,
            policy=self.policy,
            rng=random.Random(0),
        )
        self.assertTrue(out['ada_identity'])
        self.assertEqual(out['suggested']['model'], 'gpt-6-astra')
        self.assertIn(S.REASON_ADA_HARD_BINDING, out['reasons'])
        self.assertNotIn(S.REASON_HARD_BINDING_CONFLICT, out['reasons'])

    def test_non_ada_does_not_hard_bind(self):
        out = S.suggest_route(
            backend='ubuntu-a-codex',
            engine='codex',
            bot='BotA',
            named_model=False,
            named_effort=False,
            policy=self.policy,
            rng=random.Random(1),
        )
        self.assertFalse(out['ada_identity'])
        self.assertEqual(out['selection'], 'weight_sample')
        self.assertIn(S.REASON_WEIGHT_SAMPLE, out['reasons'])
        self.assertNotIn(S.REASON_ADA_HARD_BINDING, out['reasons'])

    def test_weight_sample_injectable_rng(self):
        # Astra 20 / Sol 50 / deepseek 30 → cumulative 20, 70, 100.
        # randrange(100): 0..19 astra, 20..69 sol, 70..99 deepseek.
        class Seq:
            def __init__(self, values):
                self.values = list(values)

            def randrange(self, total):
                self.assert_total = total
                return self.values.pop(0)

        rng = Seq([0, 19, 20, 69, 70, 99])
        picks = []
        for _ in range(6):
            out = S.suggest_route(
                backend='ubuntu-a-codex',
                engine='codex',
                named_model=False,
                named_effort=False,
                policy=self.policy,
                rng=rng,
            )
            picks.append(out['suggested']['tier'])
        self.assertEqual(rng.assert_total, 100)
        self.assertEqual(picks, ['astra', 'astra', 'sol', 'sol', 'deepseek', 'deepseek'])

    def test_openrouter_scheme_banned_marker(self):
        out = S.suggest_route(
            backend='ubuntu-a-claudecode',
            engine='claude',
            model='deepseek-v4.1-flash',
            named_model=True,
            ai_switch_scheme='SCHEME_8',
            policy=self.policy,
            rng=random.Random(0),
        )
        self.assertIn(S.REASON_OPENROUTER_BANNED, out['reasons'])
        self.assertEqual(out['banned_scheme']['id'], 'SCHEME_8')
        self.assertEqual(out['banned_scheme']['panel'], 'banned')
        self.assertEqual(out['banned_scheme']['active'], 'banned')

    def test_openrouter_marker_in_model_string(self):
        out = S.suggest_route(
            backend='ubuntu-a-claudecode',
            engine='claude',
            model='openrouter/deepseek',
            named_model=True,
            policy=self.policy,
            rng=random.Random(0),
        )
        self.assertIn(S.REASON_OPENROUTER_BANNED, out['reasons'])


class ShadowAttach(unittest.TestCase):
    def test_attach_writes_job_and_ledger_without_raising(self):
        policy = S.load_policy(POLICY)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'shadow' / 'ledger.jsonl'
            job = {
                'job_id': '00000000-0000-4000-8000-000000000001',
                'backend': 'ubuntu-a-codex',
                'model': 'gpt-6.1-sol',
                'effort': 'medium',
                'created_at': 1.0,
            }
            req = {'backend': 'ubuntu-a-codex', 'prompt': 'ping', 'bot': 'Ada'}
            with mock.patch.dict(os.environ, {
                'HARNESS_SHADOW_LEDGER': str(ledger),
                'HARNESS_HOME': tmp,
            }):
                record = S.attach_shadow_to_job(
                    job, req, engine='codex', bot='Ada',
                    named_model=True, named_effort=True, policy=policy,
                    rng=random.Random(0),
                )
            self.assertIsNotNone(record)
            self.assertIn('shadow', job)
            self.assertIn(S.REASON_HARD_BINDING_CONFLICT, job['shadow']['reasons'])
            self.assertTrue(ledger.is_file())
            line = json.loads(ledger.read_text().strip())
            self.assertEqual(line['job_id'], job['job_id'])
            self.assertEqual(line['mode'], 'shadow')

    def test_attach_swallows_policy_errors(self):
        job = {'job_id': 'x', 'backend': 'ubuntu-a-codex'}
        bad = {'schema_version': 99, 'tiers': []}
        out = S.attach_shadow_to_job(job, {}, policy=bad, ledger=False)
        self.assertIsNone(out)
        self.assertEqual(job['shadow']['mode'], 'shadow')
        self.assertIn('error', job['shadow'])


class ShadowSubmitWiring(unittest.TestCase):
    """Ensure submit path records shadow and does not reject on conflict."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'ssh').mkdir()
        key = self.home / 'ssh' / 'id_ed25519_ubuntu-a'
        key.write_text('not-a-real-key\n')
        targets = {
            'ubuntu-a-codex': {
                'host': 'host-a.example.invalid',
                'user': 'tester',
                'workspace': '/opt/example/src',
                'entry': '/opt/example/bin/codex',
                'engine': 'codex',
                'ssh_key': 'ssh/id_ed25519_ubuntu-a',
            },
            'tw-tl-codex': {
                'host': 'host-b.example.invalid',
                'user': 'tester',
                'workspace': '/opt/example/go/src-tech-leader',
                'entry': '/opt/example/bin/codex',
                'engine': 'codex',
                'ssh_key': 'ssh/id_ed25519_ubuntu-a',
            },
        }
        self.targets = self.home / 'targets.json'
        self.targets.write_text(json.dumps(targets))
        self.env = {
            'PATH': os.environ.get('PATH', ''),
            'HARNESS_HOME': str(self.home),
            'HARNESS_TARGETS': str(self.targets),
            'HARNESS_SSH_PROXY': 'none',
            'HARNESS_SHADOW_POLICY': str(POLICY),
            'HARNESS_SHADOW_LEDGER': str(self.home / 'shadow' / 'ledger.jsonl'),
        }

    def _load_client(self):
        client = ROOT / 'dispatcher' / 'direct_ssh_client.py'
        spec = importlib.util.spec_from_file_location('ssh_client_shadow', client)
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, self.env, clear=False):
            spec.loader.exec_module(mod)
        mod.BASE = self.home
        mod.STATE = self.home / 'direct-jobs'
        mod.TARGETS_FILE = self.targets
        mod.TARGETS = mod.load_targets(self.targets)
        mod.STATE.mkdir(parents=True, exist_ok=True)
        # Seed static codex allowlist cache so submit does not SSH.
        cache = {
            'fetched_at': 1e12,
            'source': 'test',
            'models': {
                'gpt-6.1-sol': {'efforts': list(mod.EFFORTS)},
                'gpt-6-astra': {'efforts': list(mod.EFFORTS)},
            },
        }
        for name in mod.TARGETS:
            if mod.TARGETS[name][4] == 'codex':
                (mod.STATE / (name + '-models.json')).write_text(json.dumps(cache))
        return mod

    def _submit(self, req):
        import io
        import sys
        mod = self._load_client()
        captured = {}

        def fake_submit(request, job):
            captured['req'] = dict(request)
            captured['job'] = dict(job)
            # Persist like the real path so callers can inspect .request if needed.
            mod.write(mod.STATE / (job['job_id'] + '.request'), request)
            mod.write(mod.job_path(job['job_id']), job)

        buf = io.StringIO(json.dumps(req))
        with mock.patch.object(sys, 'stdin', buf), \
                mock.patch.object(sys, 'argv', ['direct_ssh_client.py', 'submit']), \
                mock.patch.object(mod, 'submit_job', side_effect=fake_submit), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            mod.main()
            job = json.loads(out.getvalue())
        self.assertIn('job', captured)
        self.assertEqual(captured['job']['job_id'], job['job_id'])
        return job, captured['req']

    def test_submit_records_named_and_continues(self):
        job, req = self._submit({
            'backend': 'ubuntu-a-codex',
            'prompt': 'ping',
            'model': 'gpt-6.1-sol',
            'effort': 'medium',
        })
        self.assertEqual(job['status'], 'queued')
        self.assertIn('shadow', job)
        self.assertEqual(job['shadow']['mode'], 'shadow')
        self.assertIn(S.REASON_NAMED, job['shadow']['reasons'])
        self.assertNotIn('bot', req)

    def test_submit_ada_conflict_still_queues(self):
        job, req = self._submit({
            'backend': 'ubuntu-a-codex',
            'prompt': 'ping',
            'model': 'gpt-6.1-sol',
            'effort': 'high',
            'bot': 'Ada',
        })
        self.assertEqual(job['status'], 'queued')
        self.assertIn(S.REASON_HARD_BINDING_CONFLICT, job['shadow']['reasons'])
        # .request must not carry shadow-only bot field.
        self.assertNotIn('bot', req)
        self.assertEqual(req.get('backend'), 'ubuntu-a-codex')


if __name__ == '__main__':
    unittest.main()
