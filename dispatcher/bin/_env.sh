# Shared defaults for the dispatcher shell tools. Sourced, not executed.
HARNESS_HOME="${HARNESS_HOME:-$HOME/harness}"
HARNESS_HOSTS_FILE="${HARNESS_HOSTS_FILE:-$HARNESS_HOME/hosts.env}"
# shellcheck disable=SC1090
[ -r "$HARNESS_HOSTS_FILE" ] && . "$HARNESS_HOSTS_FILE"
HARNESS_SOCKS_ADDR="${HARNESS_SOCKS_ADDR:-127.0.0.1:10555}"
HARNESS_LEDGER="${HARNESS_LEDGER:-$HARNESS_HOME/harness-ledger.jsonl}"
harness_path() { case "$1" in /*) printf '%s' "$1";; *) printf '%s/%s' "$HARNESS_HOME" "$1";; esac; }
