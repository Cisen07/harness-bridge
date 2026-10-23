#!/usr/bin/env python3
"""Generic harness submit adapter: attach one-to-one wake, then submit.

Reads a job request JSON on stdin, resolves wake for the named bot via
wake_finalize.py (routing file), refuses foreign wake overwrite, and pipes the
enriched request to the dispatcher client submit command.

Private Ada/team targets and prompts stay out of this repository; pass them in
the request body / private targets.json only.

Usage:
  echo '{"backend":"ubuntu-a-codex","prompt":"ping"}' \\
    | harness_submit.py BotA

Env:
  HARNESS_HOME           deployment root (default: repo root when run from checkout)
  HARNESS_SUBMIT_CLIENT  override client path (default: $HARNESS_HOME/dispatcher/direct_ssh_client.py
                         or $HARNESS_HOME/direct_ssh_client.py)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def die(msg: str, code: int = 2) -> None:
    print(f"harness_submit: {msg}", file=sys.stderr)
    raise SystemExit(code)


def harness_home() -> Path:
    raw = os.environ.get("HARNESS_HOME")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2]


def find_client(home: Path) -> Path:
    override = os.environ.get("HARNESS_SUBMIT_CLIENT")
    if override:
        return Path(override)
    for cand in (
        home / "dispatcher" / "direct_ssh_client.py",
        home / "direct_ssh_client.py",
    ):
        if cand.is_file():
            return cand
    die("missing direct_ssh_client.py (set HARNESS_SUBMIT_CLIENT)")


def find_wake_finalize() -> Path:
    here = Path(__file__).resolve().parent / "wake_finalize.py"
    if here.is_file():
        return here
    die("missing wake_finalize.py next to harness_submit.py")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bot", help="bot name or alias from wake routing file")
    ap.add_argument(
        "--dry-inject",
        action="store_true",
        help="only print the wake-injected request JSON; do not submit",
    )
    args = ap.parse_args()

    home = harness_home()
    wake_bin = find_wake_finalize()
    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"stdin JSON invalid: {e}")
    if not isinstance(req, dict):
        die("stdin must be a JSON object")

    inj = subprocess.run(
        [sys.executable, str(wake_bin), args.bot, "--inject"],
        input=json.dumps(req, ensure_ascii=False).encode(),
        capture_output=True,
        env=os.environ.copy(),
    )
    if inj.returncode != 0:
        sys.stderr.buffer.write(inj.stderr)
        raise SystemExit(inj.returncode)

    if args.dry_inject:
        sys.stdout.buffer.write(inj.stdout)
        return

    client = find_client(home)
    proc = subprocess.run(
        [sys.executable, str(client), "submit"],
        input=inj.stdout,
        capture_output=True,
        env={**os.environ, "HARNESS_HOME": str(home)},
    )
    sys.stdout.buffer.write(proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
