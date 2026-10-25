#!/usr/bin/env python3
"""Resolve wake={url,key_env} for a named bot from wake/registry.json. Refuse cross-use.

Usage:
  python3 wake_for_bot.py BotA
  python3 wake_for_bot.py b --json
  echo '{"backend":"ubuntu-a-codex","prompt":"..."}' | python3 wake_for_bot.py a --inject
       # aliases are defined in the private registry
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

def harness_home() -> Path:
  return Path(os.environ.get("HARNESS_HOME") or "~/harness").expanduser()


def registry_path() -> Path:
  override = os.environ.get("HARNESS_WAKE_REGISTRY")
  return Path(override).expanduser() if override else harness_home() / "wake" / "registry.json"


ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def die(msg: str, code: int = 2) -> None:
  print(f"wake_for_bot: {msg}", file=sys.stderr)
  raise SystemExit(code)


def load_reg() -> dict:
  path = registry_path()
  if not path.is_file():
    die(f"missing {path} (copy dispatcher/bin/registry.example.json)")
  data = json.loads(path.read_text(encoding="utf-8"))
  if not isinstance(data, dict) or not isinstance(data.get("bots"), dict):
    die("registry must be an object with a bots object")
  return data


def check_url(url: str, name: str) -> None:
  """Use the dispatcher's exact HTTPS origin/path boundary, never string prefix matching."""
  def endpoint(value):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
      raise ValueError("invalid URL")
    p = urlsplit(value)
    if (p.scheme != "https" or not p.hostname or p.username is not None
        or p.password is not None or p.query or p.fragment
        or any(".." in part or "%" in part or ";" in part for part in p.path.split("/"))
        or "%" in p.netloc or "\\" in value):
      raise ValueError("invalid HTTPS endpoint")
    return p, (p.hostname.lower(), p.port if p.port is not None else 443)
  try:
    target, origin = endpoint(url)
    allowed, allowed_origin = endpoint(os.environ.get("HARNESS_WAKE_URL_PREFIX", ""))
    base = allowed.path.rstrip("/")
    if origin != allowed_origin or (base and target.path != base and not target.path.startswith(base + "/")):
      raise ValueError("outside trusted origin/path")
  except ValueError:
    die(f"{name}: URL rejected; configure HARNESS_WAKE_URL_PREFIX with the trusted HTTPS origin/path")


def resolve_name(bot_key: str, bots: dict) -> str:
  index = {}
  for name, ent in bots.items():
    if not isinstance(ent, dict):
      die("registry bot entries must be objects")
    aliases = ent.get("aliases", [])
    if not isinstance(aliases, list):
      die("registry aliases must be lists")
    for alias in [name, *aliases]:
      if not isinstance(alias, str) or not alias.strip():
        die("registry aliases must be non-empty strings")
      key = alias.lower()
      if key in index and index[key] != name:
        die("ambiguous registry alias or bot name")
      index[key] = name
  key = bot_key.lower()
  if key in index:
    return index[key]
  matches = [name for name in bots if key and name.lower().startswith(key)]
  if len(matches) != 1:
    die("unknown or ambiguous bot")
  return matches[0]


def resolve(bot_key: str) -> dict:
  reg = load_reg()
  bots = reg["bots"]
  name = resolve_name(bot_key, bots)
  ent = bots[name]
  url_env = ent["url_env"]
  key_env = ent["key_env"]
  if not all(isinstance(v, str) and ENV_NAME_RE.fullmatch(v) for v in (url_env, key_env)):
    die(f"{name}: invalid environment variable name")
  url = os.environ.get(url_env)
  if not url and ent.get("url_file"):
    p = harness_home() / ent["url_file"]
    if p.is_file():
      url = p.read_text().strip()
  allow_legacy = ent.get("allow_legacy_env") is True
  legacy = ent.get("legacy_env") or {}
  if not isinstance(legacy, dict):
    die("legacy_env must be an object")
  if not url and allow_legacy and legacy.get("url"):
    url = os.environ.get(legacy["url"])
  if not url:
    die(f"{name}: {url_env} not set (and no url_file fallback)")
  if not os.environ.get(key_env):
    # A child cannot install aliases in the dispatcher/worker environment.
    die(f"{name}: {key_env} not set; configure the bare key in the dispatcher environment")
  check_url(url, name)
  # Refuse if this resolution accidentally equals another bot's known URL from registry fps
  my_last = url.rstrip("/").split("/")[-1][:12]
  for other, oent in bots.items():
    if other == name:
      continue
    ofp = (oent.get("url_fp") or {}).get("last12")
    if ofp and ofp == my_last and other != name:
      die(f"{name}: resolved URL fingerprint collides with {other} — abort")
  # Non-legacy entries cannot reuse any configured legacy URL.
  if not allow_legacy:
    forbidden = reg.get("forbidden_legacy_env_for_non_legacy", ["WEBHOOK_URL"])
    if not isinstance(forbidden, list):
      die("forbidden_legacy_env_for_non_legacy must be a list")
    forbidden = set(forbidden)
    for entry in bots.values():
      legacy_entry = entry.get("legacy_env") or {}
      if not isinstance(legacy_entry, dict):
        die("legacy_env must be an object")
      if legacy_entry.get("url"):
        forbidden.add(legacy_entry["url"])
    for env_name in forbidden:
      if not isinstance(env_name, str) or not ENV_NAME_RE.fullmatch(env_name):
        die("invalid legacy URL environment variable name")
      if os.environ.get(env_name) == url:
        die(f"{name}: refusing legacy URL from {env_name}; fix {url_env}")
  wake = {"url": url, "key_env": key_env}
  return {"bot": name, "wake": wake, "url_env": url_env, "key_env": key_env}


def main() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("bot")
  ap.add_argument("--json", action="store_true", help="print full resolve record")
  ap.add_argument("--inject", action="store_true", help="read request JSON on stdin, attach wake, print")
  args = ap.parse_args()
  try:
    rec = resolve(args.bot)
  except (OSError, ValueError, KeyError, TypeError):
    die("invalid or unreadable registry/URL file")
  if args.inject:
    try:
      req = json.loads(sys.stdin.read())
    except ValueError:
      die("stdin must be valid JSON")
    if not isinstance(req, dict):
      die("stdin must be JSON object")
    if req.get("wake") is not None and req["wake"] != rec["wake"]:
      die("request already has a different wake; refusing overwrite of foreign wake")
    req["wake"] = rec["wake"]
    print(json.dumps(req, ensure_ascii=False))
    return
  if args.json:
    # URL is request data; pipe stdout to submit rather than a shared log. Keys are never emitted.
    print(json.dumps(rec, ensure_ascii=False))
  else:
    print(json.dumps(rec["wake"], ensure_ascii=False))


if __name__ == "__main__":
  main()
