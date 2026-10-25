import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tiers_publish import Conflict, Store, ValidationError
from tiers_publish.store import CapacityError, digest, encoded, fsync_dir
from tiers_publish.validation import loads, payload


def sample():
    return {
        "schema_version": 1, "notes": "isolated fixture",
        "tiers": [
            {"id": "astra", "label": "Astra", "weight_pct": 20,
             "engine": "codex", "model": "fixture-astra", "effort": "medium"},
            {"id": "sol", "label": "Sol", "weight_pct": 50,
             "engine": "codex", "model": "fixture-sol", "effort": "medium"},
            {"id": "deepseek", "label": "Deepseek", "weight_pct": 30,
             "engine": "claude", "model": "fixture-deepseek", "effort": None},
        ],
    }


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.config = sample()
        self.policy = encoded({"schema_version": 1, "policy_revision": "fixture-policy-1",
                               "capabilities": [
                                   {k: v for k, v in t.items() if k not in ("label", "weight_pct")}
                                   for t in self.config["tiers"]]})

    def options(self, base=0, key="request-1", **changes):
        result = dict(base_revision=base, publisher="unit-test", reason="test",
                      idempotency_key=key, policy_raw=self.policy)
        result.update(changes)
        return result

    def publish(self, config=None, base=0, key="request-1", **changes):
        return self.store.publish(encoded(config or self.config), **self.options(base, key, **changes))

    def test_publish_roundtrip_and_artifacts(self):
        result = self.publish()
        self.assertEqual(result["pointer"]["revision"], 1)
        self.assertEqual(self.store.get()["config"], self.config)
        directory = self.root / "harness-tiers/versions/1"
        self.assertEqual({p.name for p in directory.iterdir()},
                         {"config.json", "config.sha256", "manifest.json"})
        self.assertEqual((directory / "config.sha256").read_text().strip(),
                         digest((directory / "config.json").read_bytes()))
        self.assertTrue((self.root / "harness-tiers/publish.lock").is_file())

    def test_raw_utf8_bytes_preserved(self):
        raw = json.dumps(self.config, ensure_ascii=False, separators=(",", ":")).encode()
        self.store.publish(raw, **self.options())
        self.assertEqual((self.root / "harness-tiers/versions/1/config.json").read_bytes(), raw)

    def test_bad_weights_rejected_without_version(self):
        for weight in (-1, 101, 1.5, True, "20", None, 19):
            with self.subTest(weight=weight):
                config = copy.deepcopy(self.config)
                config["tiers"][0]["weight_pct"] = weight
                with self.assertRaises(ValidationError):
                    self.publish(config)
                self.assertFalse(self.store.head.exists())
                self.assertFalse(list(self.store.versions.iterdir()))

    def test_zero_weights_legal(self):
        for tier, weight in zip(self.config["tiers"], (0, 100, 0)):
            tier["weight_pct"] = weight
        self.publish()
        self.assertEqual(self.store.get()["config"], self.config)

    def test_duplicate_or_missing_tiers_rejected(self):
        self.config["tiers"][1] = copy.deepcopy(self.config["tiers"][0])
        with self.assertRaises(ValidationError):
            self.publish()

    def test_unknown_fields_rejected_at_all_levels(self):
        for key in ("host", "shell", "command", "url", "api_key", "targets", "deny_targets",
                    "hard_bindings", "revision", "selection", "policy_revision"):
            for nested in (True, False):
                with self.subTest(key=key, nested=nested):
                    config = sample()
                    (config["tiers"][0] if nested else config)[key] = "forbidden"
                    with self.assertRaises(ValidationError):
                        self.publish(config)

    def test_duplicate_keys_nonfinite_and_utf8_rejected(self):
        for raw in (b'{"schema_version":1,"schema_version":1}',
                    b'{"tiers":[{"id":"sol","id":"astra"}]}',
                    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'\xff', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                self.store.publish(raw, **self.options())

    def test_size_cap(self):
        with self.assertRaises(ValidationError):
            self.store.publish(b" " * 65537, **self.options())

    def test_schema_version_and_bad_types(self):
        for field, value in (("schema_version", True), ("schema_version", 2), ("tiers", {})):
            config = sample()
            config[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                self.publish(config)
        for field in ("id", "model", "engine", "effort", "label"):
            config = sample()
            config["tiers"][0][field] = []
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.publish(config)

    def test_untrusted_route_rejected(self):
        self.config["tiers"][1]["model"] = "not-in-current-capabilities"
        with self.assertRaises(ValidationError):
            self.publish()

    def test_non_codex_effort_absent_or_null(self):
        payload(encoded(self.config))
        del self.config["tiers"][2]["effort"]
        payload(encoded(self.config))
        self.config["tiers"][2]["effort"] = "medium"
        with self.assertRaises(ValidationError):
            self.publish()

    def test_obvious_shell_address_or_secret_text_rejected(self):
        for value in ("$(id)", "`id`", "https://example.invalid", "10.0.0.1", "api_key=fixture", "\ud800"):
            config = sample()
            config["notes"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                    self.store.publish(json.dumps(config).encode(), **self.options())

    def test_stale_base_conflict(self):
        self.publish()
        with self.assertRaises(Conflict) as failure:
            self.publish(key="request-2")
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.store.get()["pointer"]["revision"], 1)

    def test_same_revision_conflict(self):
        self.publish()
        with self.assertRaises(Conflict):
            self.publish(base=1, key="request-2", revision=1)

    def test_idempotent_replay_after_newer_publish(self):
        first = self.publish()
        self.publish(base=1, key="request-2")
        replay = self.publish()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["pointer"], first["pointer"])
        self.assertEqual(self.store.get()["pointer"]["revision"], 2)

    def test_key_reuse_conflict(self):
        self.publish()
        with self.assertRaises(Conflict):
            self.publish(base=1)

    def test_rollback_is_forward_and_preserves_original(self):
        self.publish()
        old = (self.root / "harness-tiers/versions/1/config.json").read_bytes()
        self.config["notes"] = "second version"
        self.publish(base=1, key="request-2")
        options = self.options(2, "rollback-1")
        result = self.store.rollback(1, **options)
        self.assertEqual(result["pointer"]["revision"], 3)
        self.assertEqual(result["pointer"]["previous_revision"], 2)
        manifest = loads((self.root / "harness-tiers/versions/3/manifest.json").read_bytes())
        self.assertEqual(manifest["rollback_of"], 1)
        self.assertEqual((self.root / "harness-tiers/versions/3/config.json").read_bytes(), old)
        self.assertEqual((self.root / "harness-tiers/versions/1/config.json").read_bytes(), old)
        self.assertTrue(self.store.rollback(1, **options)["replayed"])

    def test_rollback_current_policy_revalidation(self):
        self.publish()
        policy = loads(self.policy)
        policy["capabilities"][1]["model"] = "new-approved-model"
        with self.assertRaises(ValidationError):
            self.store.rollback(1, **self.options(1, "rollback-1", policy_raw=encoded(policy)))
        self.assertEqual(self.store.get()["pointer"]["revision"], 1)

    def test_rollback_rejects_tampering(self):
        self.publish()
        self.publish(base=1, key="request-2")
        (self.root / "harness-tiers/versions/1/config.json").write_bytes(b"{}")
        with self.assertRaises(ValidationError):
            self.store.rollback(1, **self.options(2, "rollback-1"))

    def test_pointer_path_and_hash_rejected(self):
        self.publish()
        head = loads(self.store.head.read_bytes())
        for path in ("../config.json", "/etc/passwd", "harness-tiers/versions/01/config.json"):
            self.store.head.write_bytes(encoded(dict(head, payload_path=path)))
            with self.assertRaises(ValidationError):
                self.store.get()
        self.store.head.write_bytes(encoded(dict(head, payload_sha256="0" * 64)))
        with self.assertRaises(ValidationError):
            self.store.get()

    def test_symlink_head_payload_parent_and_lock_rejected(self):
        self.publish()
        for path in (self.store.head, self.store.home / "publish.lock",
                     self.root / "harness-tiers/versions/1/config.json",
                     self.root / "harness-tiers/versions/1"):
            with self.subTest(path=path):
                backup = path.with_name(path.name + ".saved")
                path.rename(backup)
                path.symlink_to(backup)
                try:
                    with self.assertRaises(ValidationError):
                        self.publish(base=1, key="request-2")
                finally:
                    path.unlink()
                    backup.rename(path)

    def test_import_existing_revision_three_and_absolute_pointer(self):
        first = self.publish(revision=3)
        head = dict(first["pointer"], payload_path=str(self.root / "harness-tiers/versions/3/config.json"))
        self.store.head.write_bytes(encoded(head))
        # Simulate a legacy manifest with no idempotency metadata.
        (self.root / "harness-tiers/versions/3/manifest.json").write_bytes(encoded({"revision": 3}))
        result = self.publish(base=3, key="request-4")
        self.assertEqual(result["pointer"]["revision"], 4)

    def test_crash_before_version_rename_preserves_old_head(self):
        self.publish()
        old = self.store.head.read_bytes()
        with patch("tiers_publish.store.os.rename", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.publish(base=1, key="request-2")
        self.assertEqual(self.store.head.read_bytes(), old)
        self.assertEqual(self.store.get()["pointer"]["revision"], 1)
        self.assertFalse(list(self.store.versions.glob(".staging-*")))

    def test_crash_before_head_replace_keeps_old_and_skips_orphan(self):
        self.publish()
        with patch("tiers_publish.store.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.publish(base=1, key="request-2")
        self.assertEqual(self.store.get()["pointer"]["revision"], 1)
        self.assertTrue((self.store.versions / "2/config.json").is_file())
        with self.assertRaises(Conflict):
            self.publish(base=1, key="request-2")
        result = self.publish(base=1, key="request-3")
        self.assertEqual(result["pointer"]["revision"], 3)

    def test_failure_after_commit_replay_is_unambiguous(self):
        self.publish()

        def fail_after_commit(path):
            if path == self.root and self.store.get()["pointer"]["revision"] == 2:
                raise OSError("injected after replace")
            fsync_dir(path)

        with patch("tiers_publish.store.fsync_dir", side_effect=fail_after_commit):
            with self.assertRaises(OSError):
                self.publish(base=1, key="request-2")
        self.assertEqual(self.store.get()["pointer"]["revision"], 2)
        self.assertTrue(self.publish(base=1, key="request-2")["replayed"])

    def test_orphan_cannot_be_rollback_source(self):
        self.publish()
        with patch("tiers_publish.store.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.publish(base=1, key="request-2")
        self.publish(base=1, key="request-3")
        with self.assertRaises(ValidationError):
            self.store.rollback(2, **self.options(3, "rollback-orphan"))

    def test_capacity_rejects_without_gc(self):
        self.publish()
        with patch("tiers_publish.store.MAX_VERSIONS", 1):
            with self.assertRaises(CapacityError):
                self.publish(base=1, key="request-2")
        self.assertEqual(self.store.get()["pointer"]["revision"], 1)

    def test_readers_see_only_complete_snapshots_during_publishes(self):
        from concurrent.futures import ThreadPoolExecutor
        self.publish()

        def reader():
            revisions = []
            for _ in range(100):
                snapshot = self.store.get()
                revisions.append(snapshot["pointer"]["revision"])
                self.assertEqual(snapshot["config"], self.config)
            return revisions

        with ThreadPoolExecutor(max_workers=3) as pool:
            readers = [pool.submit(reader) for _ in range(3)]
            for base in range(1, 11):
                self.publish(base=base, key=f"request-{base + 1}")
            for future in readers:
                self.assertTrue(all(1 <= rev <= 11 for rev in future.result()))

    def cli(self, *args):
        return [sys.executable, "-m", "tiers_publish", "--shared-dir", str(self.root), *args]

    def write_inputs(self):
        config = self.root / "input.json"
        policy = self.root / "policy.json"
        config.write_bytes(encoded(self.config))
        policy.write_bytes(self.policy)
        return ["publish", "--config", str(config), "--policy", str(policy),
                "--publisher", "unit-test", "--reason", "test", "--base-revision", "0"]

    def test_cli_concurrent_writers_exactly_one_success(self):
        command = self.write_inputs()
        processes = [subprocess.Popen(self.cli(*command, "--idempotency-key", key),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for key in ("request-a", "request-b")]
        results = [(p, p.communicate(timeout=10)) for p in processes]
        self.assertEqual(sorted(p.returncode for p, _ in results), [0, 3])
        for p, (_, stderr) in results:
            if p.returncode == 3:
                self.assertEqual(json.loads(stderr)["status"], 409)
        self.assertEqual(self.store.get()["pointer"]["revision"], 1)

    def test_cli_get_rollback_and_validation_status(self):
        command = self.write_inputs()
        result = subprocess.run(self.cli(*command, "--idempotency-key", "request-1"),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        get = subprocess.run(self.cli("get"), capture_output=True, text=True)
        self.assertEqual(json.loads(get.stdout)["config"], self.config)
        rollback = subprocess.run(self.cli("rollback", "--source-revision", "1",
                                           "--base-revision", "1", "--policy", str(self.root / "policy.json"),
                                           "--publisher", "unit-test", "--reason", "test",
                                           "--idempotency-key", "rollback-cli"), capture_output=True, text=True)
        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        self.assertEqual(json.loads(rollback.stdout)["pointer"]["revision"], 2)
        (self.root / "input.json").write_bytes(b'{"unknown":true}')
        invalid = subprocess.run(self.cli(*command, "--idempotency-key", "bad"),
                                 capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stderr)["status"], 422)


if __name__ == "__main__":
    unittest.main()
