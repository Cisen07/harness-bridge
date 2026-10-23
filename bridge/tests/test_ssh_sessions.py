"""SSH admission: temporary STATE and mocked workers; no remote commands."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from test_security import S


class SSHSessionAdmission(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.state = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(S, 'STATE', self.state))
        targets = {name: ('host', 'user', '/', 'claude', 'claude', 'key', None)
                   for name in ('test-a', 'test-b')}
        self.stack.enter_context(mock.patch.object(S, 'TARGETS', targets))
        self.popen = self.stack.enter_context(mock.patch.object(S.subprocess, 'Popen'))
        self.session = str(uuid.uuid4())

    def submit(self, backend='test-a', session=True):
        req = dict(backend=backend, prompt='reply OK')
        if session:
            req['session_id'] = self.session
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['client', 'submit']), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(req))), \
                contextlib.redirect_stdout(output):
            S.main()
        return json.loads(output.getvalue())

    def test_queued_and_running_resumes_rejected_before_worker(self):
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                self.popen.reset_mock()
                job = self.submit()
                job['status'] = status
                S.write(S.job_path(job['job_id']), job)
                self.assertEqual(job['session_id'], self.session)
                with self.assertRaisesRegex(SystemExit, '409 Conflict: This session_id already has a queued or running job'):
                    self.submit()
                self.assertEqual(self.popen.call_count, 1)
                S.job_path(job['job_id']).unlink()

    def test_terminal_jobs_other_backends_and_new_sessions_allowed(self):
        for status in ('completed', 'failed', 'needs_attention', 'interrupted'):
            job = self.submit()
            job['status'] = status
            S.write(S.job_path(job['job_id']), job)
        self.submit()  # active on test-a
        self.submit(backend='test-b')
        self.submit(session=False)
        self.submit(session=False)
        self.assertEqual(self.popen.call_count, 8)

    def test_legacy_active_record_uses_request_session(self):
        job = self.submit()
        del job['session_id']
        S.write(S.job_path(job['job_id']), job)
        with self.assertRaisesRegex(SystemExit, '409 Conflict'):
            self.submit()
        self.assertEqual(self.popen.call_count, 1)

    def test_different_sessions_on_same_backend_allowed(self):
        self.submit()
        self.session = str(uuid.uuid4())
        self.submit()
        self.assertEqual(self.popen.call_count, 2)

    def test_unreadable_legacy_active_record_fails_closed(self):
        job = self.submit()
        del job['session_id']
        S.write(S.job_path(job['job_id']), job)
        (self.state / (job['job_id'] + '.request')).unlink()
        with self.assertRaises(FileNotFoundError):
            self.submit()
        self.assertEqual(self.popen.call_count, 1)

    def test_non_job_caches_are_ignored(self):
        (self.state / 'test-a-models.json').write_text('[]')
        (self.state / 'test-a-session-models.json').write_text('{}')
        self.submit()
        self.popen.assert_called_once()

    def test_simultaneous_admission_only_launches_one_worker(self):
        barrier = threading.Barrier(2)
        def attempt(_):
            req = dict(backend='test-a', session_id=self.session, prompt='reply OK')
            job = dict(job_id=str(uuid.uuid4()), backend='test-a', status='queued')
            barrier.wait(timeout=5)
            try:
                S.submit_job(req, job)
                return 'accepted'
            except SystemExit:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), ['accepted', 'conflict'])
        self.popen.assert_called_once()
        self.assertEqual(len(list(self.state.glob('*.json'))), 1)


if __name__ == '__main__':
    unittest.main()
