#!/usr/bin/env bash
# relay_via_host.sh — fallback channel: box -> relay host (direct Tailscale SSH, dedicated restricted key)
#                     -> curl on the relay host -> Mac bridge ($RELAY_TARGET_HOSTPORT).
# Use only when the box cannot reach the bridge directly (direct + DERP failed) while the bridge itself is up.
# Configure RELAY_SSH_TARGET / RELAY_SSH_KEY / RELAY_TARGET_HOSTPORT in $HARNESS_HOME/hosts.env.
#
# Usage:
#   relay_via_host.sh GET /health
#   relay_via_host.sh GET /jobs/<job_id>
#   relay_via_host.sh POST /jobs < body.json        # request body only via stdin
# Optional environment:
#   RELAY_MAX_TIME   total curl timeout on the relay host in seconds (default 60, 1-600)
#   RELAY_DRYRUN=1   validate locally and print the remote command, without connecting
# Exit codes:
#   0  request finished (HTTP 4xx/5xx bodies are still printed as-is)
#   2  usage error / bad configuration
#   3  method not allowed (GET, POST only)
#   4  invalid path (allowlist regex, contains .., too long)
#   5  SSH to the relay host failed (ssh exit 255)
#   6  local overall timeout (the request may have reached the bridge: GET before re-POSTing)
#   7  curl failed on the relay host (bridge unreachable, timeout; curl exit code on stderr)
# Reads no token or key material; it only references the key file path.
#
# Relay-host hardening (recommended): put the key in authorized_keys with
#   restrict,command="<a wrapper that only allows curl to $RELAY_TARGET_HOSTPORT>" ssh-ed25519 AAAA... relay
set -uo pipefail

D="$(cd "$(dirname "$0")/../bin" && pwd)"; . "$D/_env.sh"
B="$HARNESS_HOME"

die() { local code=$1; shift; echo "relay_via_host: $*" >&2; exit "$code"; }
[ -n "${RELAY_SSH_TARGET:-}" ] && [ -n "${RELAY_SSH_KEY:-}" ] && [ -n "${RELAY_TARGET_HOSTPORT:-}" ] \
  || die 2 "set RELAY_SSH_TARGET, RELAY_SSH_KEY and RELAY_TARGET_HOSTPORT in $HARNESS_HOSTS_FILE"
TARGET=$RELAY_TARGET_HOSTPORT
RELAY=$RELAY_SSH_TARGET
KEY=$(harness_path "$RELAY_SSH_KEY")

[ $# -eq 2 ] || die 2 "usage: $0 <GET|POST> <path>   (POST body on stdin)"
M=$1; P=$2

case "$M" in GET|POST) ;; *) die 3 "method not allowed: GET or POST only" ;; esac

PATH_RE='^/[A-Za-z0-9._/?=&-]*$'
[ "${#P}" -le 512 ] || die 4 "path too long (>512)"
[[ "$P" =~ $PATH_RE ]] || die 4 "path has disallowed characters (only A-Z a-z 0-9 . _ / ? = & -, must start with /)"
[[ "$P" == *..* ]] && die 4 "path must not contain .."

MAXT=${RELAY_MAX_TIME:-60}
[[ "$MAXT" =~ ^[0-9]+$ ]] && [ "$MAXT" -ge 1 ] && [ "$MAXT" -le 600 ] || die 2 "RELAY_MAX_TIME must be an integer 1-600"

# POSIX single-quote escaping for the relay host's login shell (safe for sh/bash/zsh; more portable than printf %q)
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

URL="http://$TARGET$P"
REMOTE="curl -sS --connect-timeout 10 -m $(q "$MAXT") -X $(q "$M")"
if [ "$M" = POST ]; then
  REMOTE="$REMOTE -H $(q 'Content-Type: application/json') --data-binary @-"
fi
REMOTE="$REMOTE -- $(q "$URL")"

if [ "${RELAY_DRYRUN:-0}" = 1 ]; then
  echo "DRYRUN remote: $REMOTE"; exit 0
fi

SSH=(ssh -T -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$B/ssh/known_hosts"
  -o "ProxyCommand=$B/bin/tailscale --socket=$B/run/tailscaled.sock nc %h %p")
# GET does not read stdin (-n) so the caller's stdin is not swallowed; POST streams stdin to curl's @- on the relay
[ "$M" = GET ] && SSH+=(-n)

LOCAL_MAX=$((MAXT + 30))
timeout "$LOCAL_MAX" "${SSH[@]}" "$RELAY" "$REMOTE"
rc=$?
case $rc in
  0)   exit 0 ;;
  124) die 6 "local timeout (${LOCAL_MAX}s); the request may already have run on the bridge: GET before re-POSTing" ;;
  255) die 5 "SSH to the relay host failed (tailscaled/relay unreachable or key rejected)" ;;
  *)   die 7 "curl failed on the relay host, curl exit $rc (7=cannot reach bridge, 28=timeout)" ;;
esac
