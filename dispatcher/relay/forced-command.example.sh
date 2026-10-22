#!/usr/bin/env bash
# Install at an absolute private path; set the shebang to an absolute Bash >= 5.1.
# Protocol: SSH command = "GET /health" (etc.); stdin = auth header + newline + body.
# This is an alternative to relay_via_host.sh's inline remote-command protocol.
set -euo pipefail
if (( BASH_VERSINFO[0] < 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] < 1) )); then
  echo 'relay requires Bash 5.1 or newer' >&2
  exit 2
fi
TARGET='100.x.y.z:18542'  # replace with the single trusted bridge endpoint
command_re='^(GET|POST) (/health|/backends|/jobs|/jobs/[a-f0-9-]{36})$'
[[ ${SSH_ORIGINAL_COMMAND:-} =~ $command_re ]] || exit 2
method=${BASH_REMATCH[1]}; path=${BASH_REMATCH[2]}
if [[ $method = POST ]]; then
  [[ $path = /jobs ]] || exit 2
else
  [[ $path != /jobs ]] || exit 2
fi
IFS= read -r auth
[[ $auth = 'Authorization: Bearer '* && ${#auth} -le 4120 && $auth != *$'\r'* ]] || exit 2
args=(-sS --connect-timeout 10 --max-time 60 -X "$method" -H @/dev/fd/3)
[[ $method != POST ]] || args+=(-H 'Content-Type: application/json' --data-binary @-)
# Process substitution supplies an anonymous pipe; no credential temp file or argv.
# Keep the shell alive until curl finishes, so /dev/fd/3 remains available.
curl "${args[@]}" -- "http://$TARGET$path" 3< <(printf '%s\n' "$auth")
