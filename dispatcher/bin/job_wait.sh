#!/usr/bin/env bash
# usage: job_wait.sh <job_id> [--max SECONDS(default 760)] [--bot NAME] [--no-ledger]
# Waits for an SSH-mode job (direct_ssh_client.py). Only reads the local $HARNESS_HOME/direct-jobs/<id>.json,
# which the detached worker rewrites when it finishes: no remote connection, no model call.
# Exit codes: 0 terminal state (summary printed); 2 timeout (run again); 3 worker gone without a terminal
#             state, or queued >10 min with no worker; 4 job not found.
# Prefer submitting with "wake" (webhook push) and ending the turn; use this only as a fallback.
set -u
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/_env.sh"
J="${1:-}"; shift || true
MAX=760; BOT="${HARNESS_BOT_NAME:-unknown}"; LEDGER="$HARNESS_LEDGER"
while [ $# -gt 0 ]; do case "$1" in
  --max) MAX="$2"; shift 2;; --bot) BOT="$2"; shift 2;; --no-ledger) LEDGER=-; shift;;
  *) echo "unknown argument $1" >&2; exit 64;; esac; done
[[ "$J" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || { echo "usage: job_wait.sh <job_id> [--max SECONDS] [--bot NAME]" >&2; exit 64; }
F="$HARNESS_HOME/direct-jobs/$J.json"
[ -f "$F" ] || { echo "job not found: $F"; exit 4; }
t0=$(date +%s)
while :; do
  read -r S P < <(python3 -c "import json,sys;j=json.load(open(sys.argv[1]));print(j.get('status',''),j.get('worker_pid') or 0)" "$F" 2>/dev/null)
  case "$S" in completed|failed|interrupted|needs_attention)
    python3 "$D/_job_summary.py" "$F" direct "" "$BOT" "$LEDGER"; exit 0;; esac
  if [ "${P:-0}" != 0 ] && [ "$S" = running ] && ! kill -0 "$P" 2>/dev/null; then
    sleep 2; S2=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('status',''))" "$F" 2>/dev/null)
    case "$S2" in completed|failed|interrupted|needs_attention) continue;; esac
    echo "status: $S (worker pid $P is gone and no terminal state was written)"; echo "job_id: $J"; echo "full: $F"; exit 3; fi
  if [ "$S" = queued ] && [ "${P:-0}" = 0 ]; then
    age=$(python3 -c "import json,sys,time;print(int(time.time()-json.load(open(sys.argv[1])).get('created_at',time.time())))" "$F" 2>/dev/null)
    if [ "${age:-0}" -gt 600 ]; then echo "status: queued (${age}s without a worker; it probably never started)"; echo "job_id: $J"; echo "full: $F"; exit 3; fi; fi
  el=$(( $(date +%s) - t0 ))
  if [ $el -ge "$MAX" ]; then echo "not finished, run again (status=$S, waited ${el}s)"; echo "job_id: $J"; echo "full: $F"; exit 2; fi
  sleep 10
done
