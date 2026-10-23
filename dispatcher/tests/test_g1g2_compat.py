"""Offline smoke for G1 compat aliases and G2 wake routing / finalize."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
WAKE = ROOT / "dispatcher" / "bin" / "wake_finalize.py"
SUBMIT = ROOT / "dispatcher" / "bin" / "harness_submit.py"
COMPAT = ROOT / "ops" / "compat" / "push_file.sh"
EXAMPLE_ROUTING = ROOT / "examples" / "wake-routing.example.json"
EXAMPLE_ALIASES = ROOT / "ops" / "compat" / "aliases.example.json"


def run(cmd, env=None, input_text=None, cwd=None):
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        cwd=cwd,
    )


class CompatPushFile(unittest.TestCase):
    def test_bash_syntax(self):
        r = run(["bash", "-n", str(COMPAT)])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_alias_resolve_then_missing_transport_is_ok_config_path(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            (home / "ops" / "compat").mkdir(parents=True)
            aliases = {
                "allowed": ["host-a"],
                "aliases": {"host-a": "ubuntu-a-codex"},
            }
            (home / "ops" / "compat" / "aliases.json").write_text(json.dumps(aliases))
            # No ops/push_file.sh → exit 2 from compat entry (config/transport missing)
            src = home / "payload.txt"
            src.write_text("x")
            env = {**os.environ, "HARNESS_HOME": str(home)}
            r = run(
                ["bash", str(COMPAT), str(src), "host-a", "/tmp/out.txt"],
                env=env,
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("missing transport", r.stderr)

    def test_unknown_alias(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            (home / "ops" / "compat").mkdir(parents=True)
            (home / "ops" / "compat" / "aliases.json").write_text(
                json.dumps({"aliases": {"host-a": "ubuntu-a-codex"}, "allowed": ["host-a"]})
            )
            (home / "ops" / "push_file.sh").write_text("#!/bin/bash\nexit 0\n")
            (home / "ops" / "push_file.sh").chmod(0o755)
            src = home / "payload.txt"
            src.write_text("x")
            env = {**os.environ, "HARNESS_HOME": str(home)}
            r = run(
                ["bash", str(COMPAT), str(src), "nope", "/tmp/out.txt"],
                env=env,
            )
            self.assertNotEqual(r.returncode, 0)

    def test_example_aliases_parse(self):
        data = json.loads(EXAMPLE_ALIASES.read_text())
        self.assertIn("aliases", data)
        self.assertIn("host-a", data["aliases"])


class WakeFinalize(unittest.TestCase):
    def _env(self, home: pathlib.Path, **extra):
        env = {
            **os.environ,
            "HARNESS_HOME": str(home),
            "HARNESS_WAKE_ROUTING": str(home / "wake" / "routing.json"),
            "HARNESS_WAKE_URL_PREFIX": "https://hooks.example.invalid/wake",
            "BOT_A_WAKE_URL": "https://hooks.example.invalid/wake/aaaabbbbcccc",
            "BOT_A_WAKE_KEY": "test-key-a",
            "BOT_B_WAKE_URL": "https://hooks.example.invalid/wake/ddddeeeeffff",
            "BOT_B_WAKE_KEY": "test-key-b",
        }
        env.update(extra)
        return env

    def _home_with_routing(self, td: str) -> pathlib.Path:
        home = pathlib.Path(td)
        (home / "wake").mkdir(parents=True)
        (home / "wake" / "routing.json").write_text(EXAMPLE_ROUTING.read_text())
        return home

    def test_resolve_bot_a(self):
        with tempfile.TemporaryDirectory() as td:
            home = self._home_with_routing(td)
            r = run([sys.executable, str(WAKE), "BotA", "--json"], env=self._env(home))
            self.assertEqual(r.returncode, 0, r.stderr)
            rec = json.loads(r.stdout)
            self.assertEqual(rec["bot"], "BotA")
            self.assertEqual(rec["wake"]["key_env"], "BOT_A_WAKE_KEY")
            self.assertTrue(rec["wake"]["url"].endswith("aaaabbbbcccc"))

    def test_alias_a(self):
        with tempfile.TemporaryDirectory() as td:
            home = self._home_with_routing(td)
            r = run([sys.executable, str(WAKE), "a"], env=self._env(home))
            self.assertEqual(r.returncode, 0, r.stderr)
            wake = json.loads(r.stdout)
            self.assertEqual(wake["key_env"], "BOT_A_WAKE_KEY")

    def test_fingerprint_collision(self):
        with tempfile.TemporaryDirectory() as td:
            home = self._home_with_routing(td)
            # BotA URL last12 equals BotB's declared fingerprint
            env = self._env(
                home,
                BOT_A_WAKE_URL="https://hooks.example.invalid/wake/ddddeeeeffff",
            )
            r = run([sys.executable, str(WAKE), "BotA"], env=env)
            self.assertEqual(r.returncode, 2)
            self.assertIn("collides", r.stderr)

    def test_refuse_legacy_env_for_non_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            home = self._home_with_routing(td)
            shared = "https://hooks.example.invalid/wake/aaaabbbbcccc"
            env = self._env(home, WEBHOOK_URL=shared, BOT_A_WAKE_URL=shared)
            r = run([sys.executable, str(WAKE), "BotA"], env=env)
            self.assertEqual(r.returncode, 2)
            self.assertIn("refusing legacy env", r.stderr)

    def test_inject_refuses_foreign_wake(self):
        with tempfile.TemporaryDirectory() as td:
            home = self._home_with_routing(td)
            req = {
                "backend": "ubuntu-a-codex",
                "prompt": "x",
                "wake": {
                    "url": "https://hooks.example.invalid/wake/ddddeeeeffff",
                    "key_env": "BOT_B_WAKE_KEY",
                },
            }
            r = run(
                [sys.executable, str(WAKE), "BotA", "--inject"],
                env=self._env(home),
                input_text=json.dumps(req),
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("foreign wake", r.stderr)

    def test_inject_ok(self):
        with tempfile.TemporaryDirectory() as td:
            home = self._home_with_routing(td)
            req = {"backend": "ubuntu-a-codex", "prompt": "ping"}
            r = run(
                [sys.executable, str(WAKE), "BotA", "--inject"],
                env=self._env(home),
                input_text=json.dumps(req),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["_wake_bot"], "BotA")
            self.assertEqual(out["wake"]["key_env"], "BOT_A_WAKE_KEY")

    def test_finalize_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            home = self._home_with_routing(td)
            state = home / "wake-closeout.json"
            jid = str(uuid.uuid4())
            env = self._env(home)
            r1 = run(
                [
                    sys.executable,
                    str(WAKE),
                    "--finalize",
                    "--job-id",
                    jid,
                    "--state-file",
                    str(state),
                ],
                env=env,
            )
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(json.loads(r1.stdout)["status"], "closed")
            r2 = run(
                [
                    sys.executable,
                    str(WAKE),
                    "--finalize",
                    "--job-id",
                    jid,
                    "--state-file",
                    str(state),
                ],
                env=env,
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(json.loads(r2.stdout)["status"], "already_closed")


class HarnessSubmit(unittest.TestCase):
    def test_dry_inject(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            (home / "wake").mkdir()
            (home / "wake" / "routing.json").write_text(EXAMPLE_ROUTING.read_text())
            env = {
                **os.environ,
                "HARNESS_HOME": str(home),
                "HARNESS_WAKE_ROUTING": str(home / "wake" / "routing.json"),
                "HARNESS_WAKE_URL_PREFIX": "https://hooks.example.invalid/wake",
                "BOT_A_WAKE_URL": "https://hooks.example.invalid/wake/aaaabbbbcccc",
                "BOT_A_WAKE_KEY": "test-key-a",
            }
            req = {"backend": "ubuntu-a-codex", "prompt": "ping"}
            r = run(
                [sys.executable, str(SUBMIT), "BotA", "--dry-inject"],
                env=env,
                input_text=json.dumps(req),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["wake"]["key_env"], "BOT_A_WAKE_KEY")


if __name__ == "__main__":
    unittest.main()
