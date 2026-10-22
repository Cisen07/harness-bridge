#!/usr/bin/env bash
# usage: mac_wait.sh <alias|host:port> <job_id> <outfile> [--max SECONDS(default 760)] [--bot NAME] [--no-ledger]
# Waits for one job on a Mac bridge. Only does GET /jobs/<id> through the box's userspace tailscaled SOCKS5
# proxy; never submits or resubmits. Polls quietly about every 10 s; the full JSON goes to <outfile>
# (temp file + mv), stdout only gets a summary. Aliases come from MAC_HOSTS in $HARNESS_HOME/hosts.env.
# Exit codes: 0 terminal state; 2 timeout (not finished, run again); 4 job not found (404);
#             5 repeated fetch failures (network/API down; the job may still be running - do not resubmit).
# With webhook push enabled on the bridge you normally do not need this script; keep it as a fallback.
set -u
umask 077
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/_env.sh"
H="${1:-}"; J="${2:-}"; O="${3:-}"; shift 3 2>/dev/null || { echo "usage: mac_wait.sh <alias|host:port> <job_id> <outfile>" >&2; exit 64; }
MAX=760; BOT="${HARNESS_BOT_NAME:-unknown}"; LEDGER="$HARNESS_LEDGER"
while [ $# -gt 0 ]; do case "$1" in
  --max) MAX="$2"; shift 2;; --bot) BOT="$2"; shift 2;; --no-ledger) LEDGER=-; shift;;
  *) echo "unknown argument $1" >&2; exit 64;; esac; done
HP=""; L="$H"
for pair in ${MAC_HOSTS:-}; do [ "${pair%%=*}" = "$H" ] && HP="${pair#*=}"; done
if [ -z "$HP" ]; then case "$H" in *:*) HP="$H";; *) echo "host must be an alias from MAC_HOSTS or host:port" >&2; exit 64;; esac; fi
[[ "$J" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || { echo "bad job_id" >&2; exit 64; }
[ -n "$O" ] || { echo "missing outfile" >&2; exit 64; }
AUTH_HEADER=$(python3 "$D/_bridge_auth.py") || exit 1
T=$(mktemp "$O.tmp.XXXXXXXX") || exit 1
chmod 600 "$T" || exit 1
trap 'rm -f "$T"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
t0=$(date +%s); fails=0; S=""
while :; do
  code=$(curl -H @/dev/fd/3 3<<<"$AUTH_HEADER" -sS --proxy "socks5h://$HARNESS_SOCKS_ADDR" --noproxy '' --connect-timeout 10 --max-time 20 \
         -o "$T" -w '%{http_code}' "http://$HP/jobs/$J" 2>/dev/null)
  if [ "$code" = 200 ]; then
    fails=0
    S=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('status',''))" "$T" 2>/dev/null)
    case "$S" in completed|failed|interrupted|needs_attention)
      chmod 600 "$T" && mv "$T" "$O" || exit 1; python3 "$D/_job_summary.py" "$O" mac "$L" "$BOT" "$LEDGER"; exit 0;; esac
  elif [ "$code" = 404 ]; then rm -f "$T"; echo "job not found ($L returned 404): $J"; exit 4
  else fails=$((fails+1)); [ $fails -ge 6 ] && { rm -f "$T"; echo "6 fetches in a row failed (HTTP ${code:-none}); the $L job API may be down; the job may still be running, do not resubmit"; echo "job_id: $J"; exit 5; }
  fi
  el=$(( $(date +%s) - t0 ))
  if [ $el -ge "$MAX" ]; then [ -f "$T" ] && chmod 600 "$T" && mv "$T" "$O" || exit 1; echo "not finished, run again (status=${S:-unknown}, waited ${el}s)"; echo "job_id: $J"; echo "latest snapshot: $O"; exit 2; fi
  sleep 10
done
