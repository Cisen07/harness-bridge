#!/usr/bin/env bash
# scrub_check.sh — fail if the tree contains values that look like private topology or secrets.
# Generic patterns only (this file is public). Put your real values (tailnet IPs, tailnet name, node names,
# usernames, internal hostnames), one per line, in .scrub-denylist.local (git-ignored) for an exact-match pass.
set -uo pipefail
cd "$(dirname "$0")/.."
EXCLUDE=(--exclude-dir=.git --exclude=scrub_check.sh --exclude=SCRUB-REPORT.md --exclude=.scrub-denylist.local)
fail=0
check() { # name, extended regex
  local hits; hits=$(grep -rInE "${EXCLUDE[@]}" -- "$2" . 2>/dev/null | grep -v '100\.64\.0\.0/10' || true)
  if [ -n "$hits" ]; then echo "FAIL [$1]"; echo "$hits" | sed 's/^/  /'; fail=1; else echo "ok   [$1]"; fi; }
check "tailnet (CGNAT) IPv4"     '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'
check "tailnet MagicDNS name"     'tail[0-9a-f]{6}\.ts\.net'
check "macOS home path"           '/Users/[A-Za-z0-9_]'
check "Linux home path"           '/home/[A-Za-z0-9_]'
check "root home path"            '(^|[^A-Za-z0-9_])/root/'
check "private key block"         'BEGIN [A-Z ]*PRIVATE KEY'
check "literal Bearer token"      'Bearer [A-Za-z0-9_.-]{16,}'
check "platform key prefix"       '\b(crsr|gho|ghp|ghs|github_pat|tskey|xox[abp])[-_][A-Za-z0-9_-]{8,}'
check "webhook URL with id"       'https?://[A-Za-z0-9.-]+/(automations/)?webhooks?/[A-Za-z0-9_-]{8,}'
check "real launchd label"        '<string>com\.[a-z0-9-]+\.[a-z0-9.-]+</string>'
if [ -f .scrub-denylist.local ]; then
  while IFS= read -r v; do
    [ -z "$v" ] || [ "${v#\#}" != "$v" ] && continue
    if grep -rInF "${EXCLUDE[@]}" -- "$v" . >/dev/null 2>&1; then echo "FAIL [denylist entry #$(grep -nxF -- "$v" .scrub-denylist.local | cut -d: -f1)]"; fail=1; fi
  done < .scrub-denylist.local
  echo "ok   [local denylist checked]"
fi
exit $fail
