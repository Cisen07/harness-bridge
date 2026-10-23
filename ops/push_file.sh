#!/usr/bin/env bash
# Usage: push_file.sh <local file> <backend-from-targets.json> <remote absolute path>
# Copies a file verbatim to an SSH-mode host with the same SSH settings as direct_ssh_client.py
# (dedicated key, strict known_hosts, optional tailscale nc proxy), writing to a temp file,
# verifying the remote temp sha256 BEFORE mv, then mv-ing into place. Prints no secrets.
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
# Write to remote temp only; verify BEFORE mv so a bad payload never replaces the target.
"${SSH[@]}" "mkdir -p -- $rdir && cat > $rtmp" < "$src"
remote_tmp_sha=$("${SSH[@]}" "sha256sum -- $rtmp" | cut -d' ' -f1)
echo "local  sha256: $local_sha"
echo "remote temp sha256: $remote_tmp_sha"
if [ "$local_sha" != "$remote_tmp_sha" ]; then
  "${SSH[@]}" "rm -f -- $rtmp" || true
  echo "MISMATCH (temp; target untouched)" >&2
  exit 1
fi
"${SSH[@]}" "mv -f -- $rtmp $rdst"
remote_sha=$("${SSH[@]}" "sha256sum -- $rdst" | cut -d' ' -f1)
echo "remote path:   $target:$dst"
echo "remote final sha256: $remote_sha"
if [ "$local_sha" = "$remote_sha" ]; then echo "MATCH"; else echo "MISMATCH (after mv)" >&2; exit 1; fi
