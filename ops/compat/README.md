# ops/compat — call-surface compatibility

## push_file aliases

Some deployments keep a short three-argument call surface:

```bash
ops/compat/push_file.sh <local-file> <alias> <absolute-remote-path>
```

The compat entry only resolves `<alias>` → backend name, then exec's
[`ops/push_file.sh`](../push_file.sh) (verify-before-mv transport). It does not
duplicate SSH or hashing logic.

1. Copy `aliases.example.json` to `$HARNESS_HOME/ops/compat/aliases.json` (mode 0600).
2. Point each alias at a backend key that exists in private `targets.json`.
3. Optional: set `HARNESS_PUSH_ALIASES` to an alternate JSON path.

Exit codes follow the transport script (2 = usage/config, 1 = transfer/verify failure).
