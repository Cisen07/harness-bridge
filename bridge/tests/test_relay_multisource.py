"""Synthetic multi-source regressions; no production config or credentials."""
import copy
import email.message
import http.client
import http.server
import io
import json
import pathlib
import tempfile
import threading
import types
import unittest
from unittest import mock

import test_bridge as T
import bridge_config as C

B = T.B


class RelaySources(unittest.TestCase):
    def setUp(self):
        self.ip = '192.0.2.10'
        self.node = 'peer-a.example.ts.net'
        self.rules = {self.ip: [
            {'name': 'relay-host', 'node': self.node,
             'token_files': [str(T.TMP / 'box.token')]},
            {'name': 'relayed-client', 'node': self.node,
             'token_files': [str(T.TMP / 'peer.token')]},
        ]}
        patcher = mock.patch.object(B, 'ALLOWED_SOURCES', self.rules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def headers(self, which='box'):
        headers = email.message.Message()
        value = (T.TMP / (which + '.token')).read_text().strip()
        headers['Authorization'] = 'Bearer ' + value
        return headers

    def whois(self, name=None, tags=None):
        return types.SimpleNamespace(stdout=json.dumps({
            'Node': {'Name': (name or self.node) + '.', 'Tags': tags or []},
        }))

    def test_two_credentials_same_ip_resolve_distinct_sources(self):
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois()) as run:
            self.assertEqual(B.authenticated_source(self.headers(), self.ip), 'relay-host')
            self.assertEqual(B.authenticated_source(self.headers('peer'), self.ip), 'relayed-client')
            self.assertEqual(run.call_args.args[0], [B.TAILSCALE, 'whois', '--json', self.ip])

    def test_every_slot_compared_even_first_match(self):
        self.rules[self.ip][0]['token_files'].append(str(T.TMP / 'missing-optional'))
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois()), \
                mock.patch.object(B.hmac, 'compare_digest', wraps=B.hmac.compare_digest) as compare:
            self.assertEqual(B.authenticated_source(self.headers(), self.ip), 'relay-host')
            self.assertEqual(compare.call_count, 3)

    def test_unknown_ip_and_forged_forward_header_rejected(self):
        headers = self.headers()
        headers['X-Forwarded-For'] = self.ip
        with mock.patch.object(B.subprocess, 'run') as run:
            self.assertIsNone(B.authenticated_source(headers, '192.0.2.11'))
            run.assert_not_called()

    def test_missing_duplicate_wrong_headers_rejected(self):
        for headers in (email.message.Message(), self.headers('bridge')):
            self.assertIsNone(B.authenticated_source(headers, self.ip))
        headers = self.headers()
        headers['Authorization'] = headers['Authorization']
        self.assertIsNone(B.authenticated_source(headers, self.ip))

    def test_whois_wrong_node_failure_and_missing_node_rejected(self):
        for response in (self.whois('other.example.ts.net'),
                         types.SimpleNamespace(stdout='{}'),
                         types.SimpleNamespace(stdout='invalid')):
            with mock.patch.object(B.subprocess, 'run', return_value=response):
                self.assertIsNone(B.authenticated_source(self.headers(), self.ip))
        with mock.patch.object(B.subprocess, 'run', side_effect=OSError):
            self.assertIsNone(B.authenticated_source(self.headers(), self.ip))

    def test_credential_and_identity_must_match_the_same_rule(self):
        self.rules[self.ip][0]['node'] = 'other.example.ts.net'
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois()):
            self.assertTrue(B.bearer_authorized(self.headers(), self.ip))
            self.assertTrue(B.authorized(self.ip))
            self.assertIsNone(B.authenticated_source(self.headers(), self.ip))
            self.assertEqual(B.authenticated_source(self.headers('peer'), self.ip), 'relayed-client')

    def test_tag_rules_use_the_matching_credential(self):
        first = self.rules[self.ip][0]
        first.pop('node')
        first['tag'] = 'tag:relay'
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois(tags=['tag:relay'])):
            self.assertEqual(B.authenticated_source(self.headers(), self.ip), 'relay-host')
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois()):
            self.assertIsNone(B.authenticated_source(self.headers(), self.ip))

    def test_ambiguous_runtime_credentials_rejected(self):
        self.rules[self.ip][1]['token_files'] = self.rules[self.ip][0]['token_files']
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois()):
            self.assertIsNone(B.authenticated_source(self.headers(), self.ip))
        with self.assertRaises(RuntimeError):
            B.validate_startup_token()

    def test_missing_primary_and_empty_sources_fail_closed(self):
        self.rules[self.ip][0]['token_files'] = [str(T.TMP / 'missing-primary')]
        self.assertIsNone(B.authenticated_source(self.headers(), self.ip))
        with self.assertRaises(RuntimeError):
            B.validate_startup_token()
        with mock.patch.object(B, 'ALLOWED_SOURCES', {}):
            self.assertIsNone(B.authenticated_source(self.headers(), self.ip))

    def handler(self, path='/health'):
        handler = B.Handler.__new__(B.Handler)
        handler.client_address = (self.ip, 123)
        handler.headers = self.headers('peer')
        handler.path = path
        handler.reply = mock.Mock()
        return handler

    def test_http_get_and_post_reject_at_boundary(self):
        for method in ('do_GET', 'do_POST'):
            handler = self.handler()
            handler.headers = email.message.Message()
            getattr(handler, method)()
            self.assertEqual(handler.reply.call_args.args[0], 403)

    def test_http_mismatch_and_ambiguity_rejected_before_dispatch(self):
        original = copy.deepcopy(self.rules)
        for scenario in ('mismatch', 'ambiguity'):
            self.rules[self.ip] = copy.deepcopy(original[self.ip])
            if scenario == 'mismatch':
                self.rules[self.ip][1]['node'] = 'other.example.ts.net'
            else:
                self.rules[self.ip][0]['token_files'] = self.rules[self.ip][1]['token_files']
            for method in ('do_GET', 'do_POST'):
                with self.subTest(scenario=scenario, method=method), \
                        mock.patch.object(B.subprocess, 'run', return_value=self.whois()), \
                        mock.patch.object(B.POOL, 'submit') as submit:
                    handler = self.handler('/jobs')
                    getattr(handler, method)()
                    self.assertEqual(handler.reply.call_args.args[0], 403)
                    submit.assert_not_called()

    def test_get_success_and_job_source_is_name_only(self):
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois()):
            handler = self.handler()
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], 200)
            handler = self.handler('/jobs')
            data = json.dumps({'prompt': 'synthetic test'}).encode()
            handler.rfile = io.BytesIO(data)
            handler.headers['Host'] = f'{B.HOST}:{B.PORT}'
            handler.headers['Content-Type'] = 'application/json'
            handler.headers['Content-Length'] = str(len(data))
            with mock.patch.object(B, 'JOBS', {}), mock.patch.object(B, 'save'), \
                    mock.patch.object(B.POOL, 'submit'):
                handler.do_POST()
                self.assertEqual(handler.reply.call_args.args[0], 202)
                row = next(iter(B.JOBS.values()))
                self.assertEqual(row['source'], 'relayed-client')
                self.assertFalse(set(row) & {'token', 'token_files', 'Authorization'})

    def test_caller_cannot_supply_job_source(self):
        handler = self.handler('/jobs')
        data = json.dumps({'prompt': 'synthetic', 'source': 'forged-source'}).encode()
        handler.rfile = io.BytesIO(data)
        handler.headers['Host'] = f'{B.HOST}:{B.PORT}'
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['Content-Length'] = str(len(data))
        with mock.patch.object(B.subprocess, 'run', return_value=self.whois()), \
                mock.patch.object(B.POOL, 'submit') as submit:
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0], 400)
            submit.assert_not_called()

    def test_loopback_http_resolves_sources_and_rejects_mismatch(self):
        self.rules['127.0.0.1'] = self.rules.pop(self.ip)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), B.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(B.subprocess, 'run', return_value=self.whois()), \
                    mock.patch.object(B, 'JOBS', {}), mock.patch.object(B, 'save'), \
                    mock.patch.object(B.POOL, 'submit') as submit:
                for which, source in [('box', 'relay-host'), ('peer', 'relayed-client')]:
                    headers = dict(self.headers(which).items())
                    headers.update({'Host': f'{B.HOST}:{B.PORT}', 'Content-Type': 'application/json'})
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    try:
                        conn.request('POST', '/jobs', body='{"prompt":"synthetic"}', headers=headers)
                        response = conn.getresponse()
                        self.assertEqual(response.status, 202)
                        job_id = json.loads(response.read())['job_id']
                        self.assertEqual(B.JOBS[job_id]['source'], source)
                    finally:
                        conn.close()
                self.rules['127.0.0.1'][1]['node'] = 'other.example.ts.net'
                count = submit.call_count
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    conn.request('GET', '/health', headers=dict(self.headers('peer').items()))
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 403)
                    self.assertEqual(submit.call_count, count)
                finally:
                    conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def load(self, entries):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'config.json'
            path.write_text(json.dumps({
                'server': {'bind_host': '127.0.0.1', 'allow_loopback_for_tests': True},
                'access': {'allowed_sources': entries},
            }))
            with mock.patch.object(C, 'config_path', return_value=path):
                return C.load()

    def entries(self):
        return [dict(rule, ip=self.ip) for rule in self.rules[self.ip]]

    def test_config_same_ip_and_legacy_name(self):
        cfg = self.load(self.entries())
        self.assertEqual(len(cfg['access']['rules'][self.ip]), 2)
        entries = self.entries()[:1]
        entries[0].pop('name')
        self.assertEqual(self.load(entries)['access']['rules'][self.ip][0]['name'], self.ip)

    def test_config_duplicate_empty_name_and_missing_slots_rejected(self):
        for kind in ('duplicate', 'empty', 'missing', 'nonstr', 'implicit-collision'):
            with self.subTest(kind=kind):
                entries = self.entries()
                if kind == 'duplicate':
                    entries[1]['name'] = entries[0]['name']
                elif kind == 'empty':
                    entries[0]['name'] = ' '
                elif kind == 'nonstr':
                    entries[0]['name'] = 1
                elif kind == 'implicit-collision':
                    for entry in entries:
                        entry.pop('name')
                else:
                    entries[0].pop('token_files')
                with self.assertRaises(ValueError):
                    self.load(entries)

    def test_config_empty_and_bad_document(self):
        self.assertEqual(self.load([])['access']['rules'], {})
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'config.json'
            path.write_text('{')
            with mock.patch.object(C, 'config_path', return_value=path), self.assertRaises(ValueError):
                C.load()
