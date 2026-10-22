#!/usr/bin/env bash
# Usage: push_file.sh <local file> <backend-from-targets.json> <remote absolute path>
# Copies a file verbatim to an SSH-mode host with the same SSH settings as direct_ssh_client.py
# (dedicated key, strict known_hosts, optional tailscale nc proxy), writing to a temp file and mv-ing it
# into place, then verifies the remote sha256. Prints no secrets.
set -euo pipefail
D="$(cd "$(dirname "$0")/../dispatcher/bin" && pwd)"; . "$D/_env.sh"
[ $# -eq 3 ] || { echo "usage: $0 <local file> <backend> <remote path>" >&2; exit 2; }
src=$1; target=$2; dst=$3
[ -f "$src" ] || { echo "local file not found: $src" >&2; exit 2; }
case "$dst" in /*) ;; *) echo "remote path must be absolute" >&2; exit 2 ;; esac
TARGETS_FILE="${HARNESS_TARGETS:-$HARNESS_HOME/targets.json}"
read -r host user key < <(python3 - "$TARGETS_FILE" "$target" <<'PY'
import json, sys
t = json.load(open(sys.argv[1])).get(sys.argv[2])
if not isinstance(t, dict):
    sys.exit('unknown backend: ' + sys.argv[2])
print(t['host'], t['user'], t['ssh_key'])
PY
)
[[ "$host" =~ ^[A-Za-z0-9._-]+$ && "$user" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "invalid SSH user or host" >&2; exit 2; }
key=$(harness_path "$key")
# POSIX single-quote escaping for the remote shell
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
rdst=$(q "$dst"); rtmp=$(q "$dst.part.$$"); rdir=$(q "$(dirname -- "$dst")")
SSH=(ssh -T -i "$key"
  -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$HARNESS_HOME/ssh/known_hosts")
[ "${HARNESS_SSH_PROXY:-tailscale-nc}" = tailscale-nc ] && \
  SSH+=(-o "ProxyCommand=$(q "$HARNESS_HOME/bin/tailscale") --socket=$(q "$HARNESS_HOME/run/tailscaled.sock") nc %h %p")
SSH+=(-- "$user@$host")
local_sha=$(sha256sum -- "$src" | cut -d' ' -f1)
"${SSH[@]}" "mkdir -p -- $rdir && cat > $rtmp && mv -f -- $rtmp $rdst" < "$src"
remote_sha=$("${SSH[@]}" "sha256sum -- $rdst" | cut -d' ' -f1)
echo "local  sha256: $local_sha"
echo "remote sha256: $remote_sha"
echo "remote path:   $target:$dst"
if [ "$local_sha" = "$remote_sha" ]; then echo "MATCH"; else echo "MISMATCH" >&2; exit 1; fi
