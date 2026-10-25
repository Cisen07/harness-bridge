"""Authoritative schema plus semantic checks; standard library only."""

import json
import math
import re

MAX_BYTES = 65536
IDS = {"astra", "sol", "deepseek"}
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\Z")


class ValidationError(ValueError):
    status = 422


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def fields(value, required, optional=()):
    require(type(value) is dict, "expected JSON object")
    require(set(required) <= value.keys(), "missing required fields")
    require(value.keys() <= set(required) | set(optional), "unknown fields")


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum, "invalid integer")


def token(value):
    require(type(value) is str and TOKEN.fullmatch(value), "invalid identifier")


def prose(value, limit=2048):
    require(type(value) is str and len(value) <= limit, "invalid text")
    # Structural allowlists forbid host/shell/secret fields. Also reject common
    # executable/address/credential forms in free text; this is not a DLP system.
    require(not re.search(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff`]|\$\(|\$\{|://|"
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b|-----BEGIN|"
        r"(?i:bearer\s|(?:password|secret|token|api[_-]?key)\s*[:=])",
        value), "unsafe text")


def loads(raw):
    require(type(raw) is bytes and len(raw) <= MAX_BYTES, "JSON exceeds 64 KiB")

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValidationError("non-finite JSON number")

    def floating(text):
        value = float(text)
        require(math.isfinite(value), "non-finite JSON number")
        return value

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant, parse_float=floating)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("invalid JSON: " + str(exc)) from exc


def route(tier):
    require(type(tier["engine"]) is str, "invalid engine")
    require(tier["engine"] in ("codex", "claude"), "unsupported engine")
    token(tier["model"])
    effort = tier.get("effort")
    require(effort is None or type(effort) is str, "invalid effort")
    if tier["engine"] == "codex":
        require(effort in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
                "Codex requires explicit supported effort")
    else:
        require(effort is None, "non-Codex effort must be absent or null")
    return tier["id"], tier["engine"], tier["model"], effort


def policy(raw):
    value = loads(raw)
    fields(value, ("schema_version", "policy_revision", "capabilities"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1,
            "unsupported policy schema")
    token(value["policy_revision"])
    require(type(value["capabilities"]) is list and value["capabilities"],
            "empty capabilities")
    allowed = set()
    for entry in value["capabilities"]:
        fields(entry, ("id", "engine", "model"), ("effort",))
        require(type(entry["id"]) is str and entry["id"] in IDS, "unknown tier id")
        combo = route(entry)
        require(combo not in allowed, "duplicate capability")
        allowed.add(combo)
    require({entry[0] for entry in allowed} == IDS, "missing tier capability")
    return value["policy_revision"], allowed


def payload(raw, allowed=None):
    value = loads(raw)
    fields(value, ("schema_version", "tiers"), ("notes",))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1,
            "unsupported payload schema")
    if "notes" in value:
        prose(value["notes"])
    require(type(value["tiers"]) is list and len(value["tiers"]) == 3,
            "exactly three tiers required")
    seen, total = set(), 0
    for tier in value["tiers"]:
        fields(tier, ("id", "label", "weight_pct", "engine", "model"), ("effort",))
        require(type(tier["id"]) is str and tier["id"] in IDS - seen,
                "duplicate or unknown tier id")
        seen.add(tier["id"])
        prose(tier["label"], 128)
        integer(tier["weight_pct"])
        require(tier["weight_pct"] <= 100, "weight outside 0..100")
        total += tier["weight_pct"]
        combo = route(tier)
        if allowed is not None:
            require(combo in allowed, "route absent from trusted capabilities")
    require(total == 100, "weights must sum to 100")
    return value
