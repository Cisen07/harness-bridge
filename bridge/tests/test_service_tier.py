"""Mac HTTP service-tier acceptance with synthetic state and a dry agent runner."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import test_bridge as T
import test_tier as tier_tests
import bridge_config as C

A, B = T.A, T.B


class ServiceTierAdapters(unittest.TestCase):
    def test_validation_and_omission(self):
        for bad in (None, '', ' ', 1, True, [], {}, 'fast', 'Priority', ' priority '):
            with self.subTest(bad=bad), self.assertRaises(A.ModelError):
                A.normalize_selection('mac-codex', service_tier=bad)
        self.assertEqual(A.normalize_selection('mac-codex'), (None, None, 'default'))
        self.assertEqual(A.normalize_selection('mac-claudecode'), (None, None, None))
        for value in ('default', 'priority'):
            self.assertEqual(A.normalize_selection('mac-codex', service_tier=value)[2], value)
            with self.assertRaisesRegex(A.ModelError, 'service_tier is not supported for mac-claudecode'):
                A.normalize_selection('mac-claudecode', service_tier=value)

    def test_command_always_overrides_codex_tier(self):
        for session in (None, 'synthetic-session'):
            for fields, expected in [({}, 'default'), ({'service_tier': 'default'}, 'default'),
                                     ({'service_tier': 'priority'}, 'priority')]:
                with self.subTest(session=session, expected=expected):
                    cmd = A.command_for('mac-codex', session, 'gpt-6-luna', 'low', **fields)
                    option = 'service_tier="' + expected + '"'
                    self.assertIn(option, cmd)
                    self.assertEqual(cmd[cmd.index(option) - 1], '-c')
                    self.assertIn('model_reasoning_effort="low"', cmd)
                    self.assertEqual('resume' in cmd, session is not None)
        self.assertFalse(any('service_tier' in x for x in A.command_for('mac-claudecode')))


class ServiceTierApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tier_tests.TierApi.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        tier_tests.TierApi.tearDownClass.__func__(cls)

    call = T.Api.call
    setUp = tier_tests.TierApi.setUp
    post = tier_tests.TierApi.post
    history = tier_tests.TierApi.history

    def test_http_errors_are_specific(self):
        for value in ('default', 'priority'):
            self.assertEqual(self.post(backend='mac-claudecode', service_tier=value),
                             (400, {'error': 'service_tier is not supported for mac-claudecode'}))
        for backend in ('mac-codex', 'mac-claudecode'):
            for bad in (None, '', ' ', 1, True, [], {}):
                with self.subTest(backend=backend, bad=bad):
                    self.assertEqual(self.post(backend=backend, service_tier=bad),
                                     (400, {'error': 'service_tier must be nonempty text'}))
        for bad in ('fast', 'Priority', ' priority ', 'priority";exit'):
            self.assertEqual(self.post(service_tier=bad),
                             (400, {'error': 'service_tier must be one of: default, priority'}))
        self.assertEqual(self.post(backend='mac-claudecode', effort='low'),
                         (400, {'error': 'effort is not supported for mac-claudecode'}))
        self.submit.assert_not_called()

    def test_capabilities_and_queued_record(self):
        status, backends = self.call('GET', '/backends')
        self.assertEqual(status, 200)
        for backend in ('mac-codex', 'mac-claudecode'):
            self.assertTrue(backends[backend]['supports_model'])
            self.assertEqual(backends[backend]['supports_effort'], backend == 'mac-codex')
            self.assertEqual(backends[backend]['supports_service_tier'], backend == 'mac-codex')
            self.assertNotIn('supports_tier', backends[backend])
        for fields, expected in [({}, 'default'), ({'service_tier': 'priority'}, 'priority'),
                                 ({'backend': 'mac-claudecode'}, None)]:
            status, response = self.post(**fields)
            self.assertEqual(status, 202)
            job = B.JOBS[response['job_id']]
            self.assertEqual(job['service_tier'], expected)
            self.assertIsNone(job['effective_service_tier'])

    def test_service_tier_does_not_satisfy_model_guard(self):
        with mock.patch.object(B, 'REQUIRE_EXPLICIT_SELECTION', True):
            for value in ('default', 'priority'):
                self.assertEqual(self.post(service_tier=value),
                                 (400, {'error': 'an explicit model selection or a session with model history is required'}))
                for fields in ({'tier': 'example-tier'}, {'effort': 'low'}):
                    self.assertEqual(self.post(service_tier=value, **fields)[0], 400)
            self.submit.assert_not_called()
            self.history()
            B.JOBS['old']['service_tier'] = 'priority'
            status, response = self.post(session_id='synthetic-session', tier='example-tier')
            self.assertEqual(status, 202)
            job = B.JOBS[response['job_id']]
            self.assertEqual((job['model'], job['effort'], job['model_source'], job['service_tier']),
                             ('gpt-6-astra', 'medium', 'session', 'default'))

    def test_dry_worker_priority_default_resume(self):
        argv = []

        def dry_agent(cmd, prompt):
            argv.append(cmd)
            sid = cmd[cmd.index('resume') + 1] if 'resume' in cmd else 'synthetic-session'
            events = [{'type': 'thread.started', 'thread_id': sid}, {'type': 'turn.completed'}]
            return subprocess.CompletedProcess(cmd, 0, '\n'.join(map(json.dumps, events)), '')

        def submit_and_run(**fields):
            status, response = self.post(**fields)
            self.assertEqual(status, 202)
            # Execute the exact worker arguments enqueued by the HTTP handler.
            worker, *args = self.submit.call_args.args
            worker(*args)
            status, job = self.call('GET', '/jobs/' + response['job_id'])
            self.assertEqual((status, job['status']), (200, 'completed'))
            return job

        with mock.patch.object(B, 'run_agent', side_effect=dry_agent), \
                mock.patch.object(B, 'notify_terminal'), mock.patch.object(B, 'codex_effective', return_value=None):
            first = submit_and_run(model='gpt-6-luna', effort='low', service_tier='priority')
            self.assertEqual((first['service_tier'], first['effective_service_tier']), ('priority', 'priority'))
            self.assertIn('service_tier="priority"', argv[-1])
            resume = submit_and_run(session_id=first['session_id'])
            self.assertEqual((resume['model'], resume['effort'], resume['model_source'],
                              resume['service_tier'], resume['effective_service_tier']),
                             ('gpt-6-luna', 'low', 'session', 'default', 'default'))
            self.assertIn('service_tier="default"', argv[-1])
            self.assertIsNone(resume['tier'])
            again = submit_and_run(session_id=first['session_id'], service_tier='priority')
            self.assertEqual(again['effective_service_tier'], 'priority')
            default = submit_and_run()
            self.assertEqual((default['service_tier'], default['effective_service_tier']), ('default', 'default'))
            self.assertIn('service_tier="default"', argv[-1])

    def test_preparation_failure_does_not_claim_effective_tier(self):
        status, response = self.post(service_tier='priority')
        self.assertEqual(status, 202)
        with mock.patch.object(B, 'command_for', side_effect=A.ModelError('synthetic validation failure')), \
                mock.patch.object(B, 'run_agent') as run, mock.patch.object(B, 'notify_terminal'):
            worker, *args = self.submit.call_args.args
            worker(*args)
        run.assert_not_called()
        job = B.JOBS[response['job_id']]
        self.assertEqual(job['status'], 'failed')
        self.assertIsNone(job['effective_service_tier'])

    def test_legacy_worker_call_and_old_journal(self):
        B.JOBS['synthetic-job'] = {'status': 'queued'}
        result = subprocess.CompletedProcess([], 0, '{"type":"turn.completed"}', '')
        with mock.patch.object(B, 'run_agent', return_value=result) as run, \
                mock.patch.object(B, 'notify_terminal'), mock.patch.object(B, 'codex_effective', return_value=None):
            B.run_job('synthetic-job', 'dry test', None)
        self.assertIn('service_tier="default"', run.call_args.args[0])
        self.assertEqual(B.JOBS['synthetic-job']['effective_service_tier'], 'default')
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'jobs.json'
            journal.write_text(json.dumps({'old': {'status': 'completed'}}))
            with mock.patch.dict(C.CONFIG, server=dict(C.CONFIG['server'], journal=str(journal))):
                spec = importlib.util.spec_from_file_location('service_tier_journal_test', Path(B.__file__))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            try:
                self.assertNotIn('service_tier', module.JOBS['old'])
                self.assertNotIn('effective_service_tier', module.JOBS['old'])
            finally:
                module.POOL.shutdown()
