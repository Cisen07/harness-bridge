#!/usr/bin/env bash
# Start the tailscaled watchdog detached (setsid nohup, like start-tailscaled.sh); no-op if it is already running.
SELF="$(cd "$(dirname "$0")" && pwd)"; . "$SELF/../bin/_env.sh"
W="$HARNESS_HOME/watchdog"; mkdir -p "$W"
if pgrep -f "$SELF/tailscaled_watchdog.sh loop" >/dev/null; then echo "watchdog already running"; exit 0; fi
setsid nohup "$SELF/tailscaled_watchdog.sh" loop >>"$W/watchdog.out" 2>&1 < /dev/null &
echo "started watchdog pid $!"
