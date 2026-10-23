#!/usr/bin/env python3
"""One-to-one wake routing, inject, and idempotent closeout (no network I/O).

Resolve a caller/bot name to wake={url,key_env} from a private routing file.
Refuse cross-bot fingerprint collisions, foreign wake overwrite, and legacy
env reuse for non-legacy bots. Finalize marks a job_id closed in a local
state file (idempotent).

Usage:
  wake_finalize.py BotA
  wake_finalize.py BotA --json
  echo '{"backend":"ubuntu-a-codex","prompt":"..."}' | wake_finalize.py BotA --inject
  wake_finalize.py --finalize --job-id <uuid> --state-file /path/to/wake-closeout.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT_ENV = "HARNESS_HOME"
ROUTING_ENV = "HARNESS_WAKE_ROUTING"
PREFIX_ENV = "HARNESS_WAKE_URL_PREFIX"
DEFAULT_ROUTING_REL = Path("wake") / "routing.json"


def die(msg: str, code: int = 2) -> None:
    print(f"wake_finalize: {msg}", file=sys.stderr)
    raise SystemExit(code)


def harness_home() -> Path:
    raw = os.environ.get(ROOT_ENV)
    if raw:
        return Path(raw)
    # dispatcher/bin -> repo root when running from a checkout
    return Path(__file__).resolve().parents[2]


def routing_path() -> Path:
    override = os.environ.get(ROUTING_ENV)
    if override:
        return Path(override)
    return harness_home() / DEFAULT_ROUTING_REL


def load_routing() -> dict[str, Any]:
    path = routing_path()
    if not path.is_file():
        die(f"missing routing file: {path} (copy examples/wake-routing.example.json)")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("bots"), dict):
        die("routing file must be an object with a 'bots' object")
    return data


def build_alias_index(bots: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for name, ent in bots.items():
        index[name.lower()] = name
        for a in ent.get("aliases") or []:
            if not isinstance(a, str):
                continue
            key = a.lower()
            if key in index and index[key] != name:
                die(f"alias {a!r} maps to both {index[key]!r} and {name!r}")
            index[key] = name
    return index


def resolve_name(bot_key: str, bots: dict[str, Any]) -> str:
    index = build_alias_index(bots)
    if bot_key.lower() in index:
        return index[bot_key.lower()]
    for k in bots:
        if k.lower() == bot_key.lower() or k.lower().startswith(bot_key.lower()):
            return k
    die(f"unknown bot {bot_key!r}; known={sorted(bots)}")


def url_prefix() -> str:
    prefix = os.environ.get(PREFIX_ENV, "").strip()
    if not prefix:
        # Optional routing-level default (still not a secret).
        return ""
    return prefix.rstrip("/")


def read_url(ent: dict[str, Any], name: str) -> str:
    url_env = ent.get("url_env")
    if not isinstance(url_env, str) or not url_env:
        die(f"{name}: url_env missing")
    url = os.environ.get(url_env)
    url_file = ent.get("url_file")
    if not url and isinstance(url_file, str) and url_file:
        p = harness_home() / url_file
        if p.is_file():
            url = p.read_text(encoding="utf-8").strip()
    legacy = ent.get("legacy_env") if ent.get("allow_legacy_env") else None
    if not url and isinstance(legacy, dict):
        leg_url = legacy.get("url")
        if isinstance(leg_url, str) and leg_url:
            url = os.environ.get(leg_url)
    if not url:
        die(f"{name}: {url_env} not set (and no url_file fallback)")
    return url


def require_key_env(ent: dict[str, Any], name: str) -> str:
    key_env = ent.get("key_env")
    if not isinstance(key_env, str) or not key_env:
        die(f"{name}: key_env missing")
    if os.environ.get(key_env):
        return key_env
    legacy = ent.get("legacy_env") if ent.get("allow_legacy_env") else None
    if isinstance(legacy, dict):
        alt = legacy.get("auth_header_or_key")
        if isinstance(alt, str) and alt and os.environ.get(alt):
            # Caller still receives the declared key_env name; operator must alias.
            return key_env
    die(f"{name}: {key_env} not set")


def check_prefix(url: str, name: str) -> None:
    prefix = url_prefix()
    if not prefix:
        # Without a configured trusted origin, only require https.
        if not url.startswith("https://"):
            die(f"{name}: url must be https when {PREFIX_ENV} is unset")
        return
    if not url.startswith(prefix):
        die(f"{name}: url failed trusted prefix check")


def check_fingerprint_collision(url: str, name: str, bots: dict[str, Any]) -> None:
    my_last = url.rstrip("/").split("/")[-1][:12]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,12}", my_last):
        # Non-matching shape: still compare declared fingerprints only.
        my_last = ""
    for other, oent in bots.items():
        if other == name:
            continue
        ofp = (oent.get("url_fp") or {}).get("last12")
        if ofp and my_last and ofp == my_last:
            die(f"{name}: resolved URL fingerprint collides with {other} — abort")


def check_forbidden_legacy(url: str, name: str, ent: dict[str, Any], routing: dict[str, Any]) -> None:
    if ent.get("allow_legacy_env"):
        return
    forbidden = routing.get("forbidden_legacy_env_for_non_legacy") or ["WEBHOOK_URL"]
    if not isinstance(forbidden, list):
        return
    for env_name in forbidden:
        if not isinstance(env_name, str):
            continue
        wh = os.environ.get(env_name)
        if wh and url == wh:
            die(f"{name}: refusing legacy env {env_name}; fix {ent.get('url_env')}")


def resolve(bot_key: str) -> dict[str, Any]:
    routing = load_routing()
    bots = routing["bots"]
    name = resolve_name(bot_key, bots)
    ent = bots[name]
    if not isinstance(ent, dict):
        die(f"{name}: entry must be an object")
    url = read_url(ent, name)
    key_env = require_key_env(ent, name)
    check_prefix(url, name)
    check_fingerprint_collision(url, name, bots)
    check_forbidden_legacy(url, name, ent, routing)
    wake = {"url": url, "key_env": key_env}
    return {
        "bot": name,
        "wake": wake,
        "url_env": ent.get("url_env"),
        "key_env": key_env,
    }


def inject(bot_key: str, req: dict[str, Any]) -> dict[str, Any]:
    rec = resolve(bot_key)
    existing = req.get("wake")
    if existing and existing != rec["wake"]:
        die("request already has a different wake; refusing overwrite of foreign wake")
    out = dict(req)
    out["wake"] = rec["wake"]
    out["_wake_bot"] = rec["bot"]
    return out


def finalize(job_id: str, state_file: Path) -> dict[str, Any]:
    try:
        uuid.UUID(job_id)
    except ValueError:
        die("job_id must be a UUID")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if state_file.is_file():
        data = json.loads(state_file.read_text(encoding="utf-8"))
    else:
        data = {"closed": {}}
    if not isinstance(data, dict):
        die("state file must be a JSON object")
    closed = data.setdefault("closed", {})
    if not isinstance(closed, dict):
        die("state.closed must be an object")
    if job_id in closed:
        return {"job_id": job_id, "status": "already_closed", "record": closed[job_id]}
    import datetime as _dt

    record = {
        "closed_at": _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "wake_finalize",
    }
    closed[job_id] = record
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(state_file)
    return {"job_id": job_id, "status": "closed", "record": record}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bot", nargs="?", help="bot name or alias from routing file")
    ap.add_argument("--json", action="store_true", help="print full resolve record")
    ap.add_argument(
        "--inject",
        action="store_true",
        help="read request JSON on stdin, attach wake, print",
    )
    ap.add_argument("--finalize", action="store_true", help="idempotent closeout for a job_id")
    ap.add_argument("--job-id", help="UUID for --finalize")
    ap.add_argument("--state-file", type=Path, help="local closeout state JSON path")
    args = ap.parse_args()

    if args.finalize:
        if not args.job_id or not args.state_file:
            die("--finalize requires --job-id and --state-file")
        print(json.dumps(finalize(args.job_id, args.state_file), ensure_ascii=False))
        return

    if not args.bot:
        die("bot name required unless --finalize")

    if args.inject:
        raw = sys.stdin.read()
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            die(f"stdin JSON invalid: {e}")
        if not isinstance(req, dict):
            die("stdin must be a JSON object")
        print(json.dumps(inject(args.bot, req), ensure_ascii=False))
        return

    rec = resolve(args.bot)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False))
    else:
        print(json.dumps(rec["wake"], ensure_ascii=False))


if __name__ == "__main__":
    main()
