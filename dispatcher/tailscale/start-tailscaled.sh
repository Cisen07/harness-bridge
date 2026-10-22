#!/usr/bin/env bash
# Reproducible userspace tailscaled launcher for the dispatcher box (no root, no TUN). Does NOT log in.
# First login (once, interactive or with an auth key you supply out of band):
#   $HARNESS_HOME/bin/tailscale --socket=$HARNESS_HOME/run/tailscaled.sock up --advertise-tags=tag:<dispatcher-tag>
# Exposes a SOCKS5 proxy on $HARNESS_SOCKS_ADDR (default 127.0.0.1:10555) used by mac_wait.sh / curl.
set -euo pipefail
umask 077
D="$(cd "$(dirname "$0")/../bin" && pwd)"; . "$D/_env.sh"
B="$HARNESS_HOME"
PORT="${HARNESS_SOCKS_ADDR##*:}"
VER="${TS_VERSION:-1.104.2}"
F="tailscale_${VER}_amd64.tgz"
mkdir -p "$B/bin" "$B/state" "$B/run" "$B/dl"; chmod 700 "$B" "$B/state" "$B/run"
if [ ! -x "$B/bin/tailscaled" ] || [ ! -x "$B/bin/tailscale" ]; then
  cd "$B/dl"
  curl -fsSLO "https://pkgs.tailscale.com/stable/$F"
  curl -fsSL "https://pkgs.tailscale.com/stable/$F.sha256" -o "$F.sha256"
  [ "$(tr -d '[:space:]' < "$F.sha256")" = "$(sha256sum "$F" | cut -d' ' -f1)" ] || { echo "SHA256 MISMATCH" >&2; exit 1; }
  tar xzf "$F"; install -m 755 "tailscale_${VER}_amd64/tailscale" "tailscale_${VER}_amd64/tailscaled" "$B/bin/"
fi
if pgrep -f "$B/bin/tailscaled" >/dev/null; then echo "tailscaled already running"; exit 0; fi
if (ss -ltn 2>/dev/null || true) | grep -q ":$PORT\b"; then echo "port $PORT in use; aborting" >&2; exit 1; fi
setsid nohup "$B/bin/tailscaled" \
  --tun=userspace-networking \
  --socket="$B/run/tailscaled.sock" \
  --statedir="$B/state" \
  --state="$B/state/tailscaled.state" \
  --socks5-server="$HARNESS_SOCKS_ADDR" \
  >>"$B/run/tailscaled.log" 2>&1 < /dev/null &
echo "started pid $!"
