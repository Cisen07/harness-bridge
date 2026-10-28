"""Mac HTTP routing metadata contracts; synthetic state and no agent launches."""
import copy
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import test_bridge as T
import bridge_config as C

B = T.B


class TierConfig(unittest.TestCase):
    def load_tier(self, tier):
        config = copy.deepcopy(T.CONF)
        config['tier'] = tier
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            path.write_text(json.dumps(config))
            with mock.patch.object(C, 'config_path', return_value=path):
                return C.load()['tier']

    def test_defaults_and_config_types(self):
        self.assertEqual(self.load_tier({}), {'allowed_tiers': [], 'require_explicit_selection': False})
        self.assertEqual(self.load_tier({'allowed_tiers': ['example-tier']})['allowed_tiers'], ['example-tier'])
        for tier in (None, [], 'bad', {'allowed_tiers': 'bad'}, {'allowed_tiers': [None]},
                     {'allowed_tiers': [' ']}, {'require_explicit_selection': 1},
                     {'require_explicit_selection': 'false'}):
            with self.subTest(tier=tier), self.assertRaises(ValueError):
                self.load_tier(tier)

    @unittest.skipUnless(C._toml, 'TOML parser requires Python 3.11+ or tomli')
    def test_example_defaults(self):
        example = C._toml.loads((Path(__file__).parents[1] / 'config.example.toml').read_text())
        self.assertEqual(example['tier'], {'allowed_tiers': [], 'require_explicit_selection': False})

    def test_old_journal_load(self):
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'jobs.json'
            journal.write_text(json.dumps({'old': {'status': 'completed'}}))
            server = dict(C.CONFIG['server'], journal=str(journal))
            with mock.patch.dict(C.CONFIG, server=server):
                spec = importlib.util.spec_from_file_location('tier_journal_test', Path(B.__file__))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            try:
                self.assertIsNone(module.JOBS['old']['tier'])
                self.assertIsNone(module.JOBS['old']['model'])
                self.assertEqual(module.JOBS['old']['status'], 'completed')
            finally:
                module.POOL.shutdown()


class TierApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        T.Api.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    call = T.Api.call

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('JOBS', {}), ('REQUIRE_EXPLICIT_SELECTION', False), ('ALLOWED_TIERS', ())]:
            self.stack.enter_context(mock.patch.object(B, name, value))
        self.stack.enter_context(mock.patch.object(B, 'authorized', return_value=True))
        self.stack.enter_context(mock.patch.object(B, 'save'))
        self.submit = self.stack.enter_context(mock.patch.object(B.POOL, 'submit'))

    def post(self, **extra):
        return self.call('POST', '/jobs', dict(prompt='synthetic test', **extra))

    def history(self, model='gpt-6-astra', effort='medium'):
        B.JOBS['old'] = dict(session_id='synthetic-session', status='completed',
                             workspace=B.BACKENDS['mac-codex']['workspace'], backend='mac-codex',
                             model=model, effort=effort, created_at='2026-01-01')

    def test_guard_requires_model_even_with_tier_or_effort(self):
        for guard in (False, True):
            with mock.patch.object(B, 'REQUIRE_EXPLICIT_SELECTION', guard):
                for fields in ({'tier': 'example-tier'}, {'tier': 'example-tier', 'effort': 'medium'}):
                    with self.subTest(guard=guard, fields=fields):
                        status, body = self.post(**fields)
                        self.assertEqual(status, 400)
                        self.assertIn('the bridge does not resolve tiers', body['error'])
        with mock.patch.object(B, 'REQUIRE_EXPLICIT_SELECTION', True):
            for fields in ({}, {'effort': 'medium'}):
                self.assertEqual(self.post(**fields), (400, {'error': 'an explicit model selection or a session with model history is required'}))
        self.submit.assert_not_called()

    def test_guard_resume_inherits_session_model(self):
        with mock.patch.object(B, 'REQUIRE_EXPLICIT_SELECTION', True):
            for fields in ({}, {'tier': 'example-tier'}):
                self.history()
                status, response = self.post(session_id='synthetic-session', **fields)
                self.assertEqual(status, 202)
                job = B.JOBS.pop(response['job_id'])
                self.assertEqual((job['model'], job['effort'], job['model_source']), ('gpt-6-astra', 'medium', 'session'))
                self.assertEqual(job['tier'], fields.get('tier'))

    def test_guard_rejects_session_without_model(self):
        self.history(model=None)
        with mock.patch.object(B, 'REQUIRE_EXPLICIT_SELECTION', True):
            for session in ('synthetic-session', 'unknown'):
                self.assertEqual(self.post(session_id=session)[0], 400)
        self.submit.assert_not_called()

    def test_guard_off_legacy_and_metadata_only(self):
        self.assertEqual(self.post()[0], 202)
        for backend, model in [('mac-codex', 'gpt-6-astra'), ('mac-claudecode', 'claude-opus-5-5')]:
            status, response = self.post(backend=backend, model=model, tier=' example-tier ')
            self.assertEqual(status, 202)
            self.assertEqual(B.JOBS[response['job_id']]['tier'], 'example-tier')
            self.assertNotIn('example-tier', self.submit.call_args.args)  # metadata never reaches the worker
        status, response = self.post(tier=None)
        self.assertEqual(status, 202)
        self.assertIsNone(B.JOBS[response['job_id']]['tier'])
        status, backends = self.call('GET', '/backends')
        self.assertEqual(status, 200)
        self.assertTrue(all('supports_tier' not in b for b in backends.values()))

    def test_invalid_tier_and_allowlist(self):
        for tier in ('', ' ', 1, True, [], {}):
            with self.subTest(tier=tier):
                self.assertEqual(self.post(model='gpt-6-astra', tier=tier), (400, {'error': 'Invalid JSON request, prompt, or session_id'}))
        with mock.patch.object(B, 'ALLOWED_TIERS', ('example-z', 'example-a')):
            self.assertEqual(self.post(model='gpt-6-astra', tier='unknown'), (400, {'error': 'Unknown tier; allowed: example-a, example-z'}))
            self.assertEqual(self.post(model='gpt-6-astra', tier='example-a')[0], 202)
