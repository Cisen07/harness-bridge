#!/usr/bin/env bash
# Compatible call surface for short backend aliases.
# Usage: push_file.sh <local file> <alias> <remote absolute path>
#
# Resolves <alias> via $HARNESS_HOME/ops/compat/aliases.json (private) or the
# path in HARNESS_PUSH_ALIASES, then exec's the shared transport ops/push_file.sh.
# Does not reimplement copy/verify logic.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export HARNESS_HOME="${HARNESS_HOME:-$ROOT}"

[ $# -eq 3 ] || { echo "usage: $0 <local file> <alias> <remote path>" >&2; exit 2; }
src=$1; alias_name=$2; dst=$3

ALIASES_FILE="${HARNESS_PUSH_ALIASES:-$HARNESS_HOME/ops/compat/aliases.json}"
if [ ! -f "$ALIASES_FILE" ]; then
  example="$HARNESS_HOME/ops/compat/aliases.example.json"
  echo "missing aliases file: $ALIASES_FILE (copy from $example)" >&2
  exit 2
fi

backend=$(python3 - "$ALIASES_FILE" "$alias_name" <<'PY'
import json, sys
path, alias = sys.argv[1], sys.argv[2]
data = json.load(open(path))
aliases = data.get("aliases") if isinstance(data, dict) else None
if not isinstance(aliases, dict):
    sys.exit("aliases.json must contain an object key 'aliases'")
allowed = data.get("allowed")
if isinstance(allowed, list) and alias not in allowed:
    sys.exit("alias not in allowed list: " + alias)
if alias not in aliases:
    sys.exit("unknown alias: " + alias)
backend = aliases[alias]
if not isinstance(backend, str) or not backend:
    sys.exit("alias maps to empty backend: " + alias)
# Keep call-surface aliases short and backend names targets-safe.
import re
if not re.fullmatch(r"[A-Za-z0-9._-]+", alias):
    sys.exit("invalid alias name")
if not re.fullmatch(r"[A-Za-z0-9._-]+", backend):
    sys.exit("invalid backend name for alias")
print(backend)
PY
)

CORE="$HARNESS_HOME/ops/push_file.sh"
[ -x "$CORE" ] || { echo "missing transport: $CORE" >&2; exit 2; }
exec "$CORE" "$src" "$backend" "$dst"
