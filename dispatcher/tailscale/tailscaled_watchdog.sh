#!/usr/bin/env bash
# Control-plane watchdog for the userspace tailscaled on the dispatcher box.
# Checks health every 60 s and only acts after the control plane has been unhealthy for >5 minutes.
# Without root there is no way to drop just the control connection (ss -K is denied; `debug break-tcp-conns`
# also closes the SOCKS5 listener), so the action is: re-check for self-healing; if tailnet sessions are active
# (tailscale nc / curl via the SOCKS port) defer up to 15 more minutes; then back up state (cp -p -r) ->
# SIGTERM tailscaled -> start it again with start-tailscaled.sh (same flags). At most one restart per 30 minutes.
# Never logs out, never changes flags, never deletes state. Usage: tailscaled_watchdog.sh loop | once
set -uo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)"; . "$SELF/../bin/_env.sh"
B="$HARNESS_HOME"; W=$B/watchdog; LOG=$W/watchdog.log; mkdir -p "$W"
TS="$B/bin/tailscale --socket=$B/run/tailscaled.sock"
UNHEALTHY_LIMIT=300; RESTART_GAP=1800; BUSY_EXTRA=900; INTERVAL=60
log(){ printf '%s %s\n' "$(date '+%F %T%z')" "$*" >> "$LOG"; }
get(){ cat "$W/$1" 2>/dev/null || echo 0; }
put(){ printf '%s\n' "$2" > "$W/$1.tmp" && mv "$W/$1.tmp" "$W/$1"; }
tsd_pid(){ pgrep -f "^$B/bin/tailscaled " | head -1; }
health(){ # prints ok / unhealthy:<reason> / down
  local j; j=$(timeout 15 $TS status --json 2>/dev/null) || { echo down; return; }
  printf '%s' "$j" | python3 -c '
import json,sys
d=json.load(sys.stdin); h=" | ".join(d.get("Health") or [])
if d.get("BackendState")!="Running": print("unhealthy:backend="+str(d.get("BackendState")))
elif "coordination server" in h or "network map" in h: print("unhealthy:"+h[:120])
else: print("ok")' 2>/dev/null || echo down; }
busy(){ pgrep -f "$B/bin/tailscale --socket=$B/run/tailscaled.sock nc " >/dev/null || pgrep -f "socks5h?://$HARNESS_SOCKS_ADDR" >/dev/null; }
restart(){
  local now ts pid i; now=$(date +%s)
  if [ $((now - $(get last_restart))) -lt $RESTART_GAP ]; then log "SKIP restart: less than 30 minutes since the last restart"; return; fi
  ts=$(date +%Y%m%d%H%M%S)
  cp -p -r "$B/state" "$B/state.bak-$ts" || { log "ABORT: state backup failed, not restarting"; return; }
  log "BACKUP state -> state.bak-$ts"
  pid=$(tsd_pid)
  if [ -n "$pid" ]; then
    kill -TERM "$pid"; for i in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then log "ABORT: tailscaled pid $pid did not exit within 30s; not killing, not starting a second copy"; return; fi
    log "STOPPED tailscaled pid $pid (SIGTERM)"
  fi
  put last_restart "$now"
  log "START $(bash "$SELF/start-tailscaled.sh" 2>&1 | tr '\n' ' ')"
  for i in $(seq 1 12); do sleep 5; [ "$(health)" = ok ] && { log "RECOVERED after restart ($((i*5))s), pid $(tsd_pid)"; put unhealthy_since 0; return; }; done
  log "AFTER restart still: $(health)"
}
check(){
  local h now since; h=$(health); now=$(date +%s); since=$(get unhealthy_since)
  if [ "$h" = ok ]; then
    [ "$since" != 0 ] && log "OK control plane recovered by itself (unhealthy for $((now-since))s)"
    put unhealthy_since 0; return
  fi
  if [ "$h" = down ] && [ -z "$(tsd_pid)" ]; then log "DOWN tailscaled is not running"; restart; return; fi
  if [ "$since" = 0 ]; then put unhealthy_since "$now"; log "UNHEALTHY start: $h"; return; fi
  local dur=$((now-since))
  [ $dur -le $UNHEALTHY_LIMIT ] && return
  if busy && [ $dur -le $((UNHEALTHY_LIMIT+BUSY_EXTRA)) ]; then log "DEFER unhealthy for ${dur}s but tailnet sessions are active; not restarting yet"; return; fi
  log "ACT unhealthy for ${dur}s ($h); performing the rate-limited restart"
  restart
}
case "${1:-loop}" in
  once) check ;;
  loop)
    exec 9>"$W/.lock"; flock -n 9 || { echo "watchdog already running"; exit 0; }
    log "WATCHDOG start pid $$"
    while :; do check; sleep $INTERVAL; done ;;
  *) echo "usage: $0 loop|once"; exit 2 ;;
esac
