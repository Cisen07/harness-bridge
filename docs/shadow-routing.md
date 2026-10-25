# P1 shadow routing

## Semantics

On SSH-mode `submit`, the dispatcher may attach a `shadow` object to the job
record. It answers: *given the locked policy, what route would we suggest?* and
compares that to the **actual** `backend` / engine / `model` / `effort` used for
this job.

Shadow **does not**:

- change task selection or rewrite request fields before launch
- reject submit (including `HARD_BINDING_CONFLICT`)
- require or emit `decision_*` fields on the Mac job API
- read secret values, TF config, or a second live config source

Failure while computing or ledgering shadow is recorded on the job when possible
and never fails admission.

## Locked policy (example)

Bundled at [`dispatcher/shadow_policy.example.json`](../dispatcher/shadow_policy.example.json):

| Tier id   | weight_pct | engine | model                 | effort |
|-----------|------------|--------|-----------------------|--------|
| astra     | 20         | codex  | gpt-6-astra           | medium |
| sol       | 50         | codex  | gpt-6.1-sol           | medium |
| deepseek  | 30         | claude | deepseek-v4.1-flash   | —      |

- **Ada hard binding** (Ada identity or `tw-tl-*` backends only): Codex
  `gpt-6-astra` / `medium`. Non-Ada identities are not hard-bound.
- **deny_targets**: empty minimal set in the example.
- **OpenRouter `SCHEME_8`**: retained in policy as a banned scheme
  (`panel`/`active` = banned); shadow records `OPENROUTER_SCHEME_BANNED` when
  markers match. It is not offered as an active/panel choice.

Override with `HARNESS_SHADOW_POLICY`. Optional bot identity:
request field `bot` (stripped from `.request`) or `HARNESS_SHADOW_BOT`.

## Suggested selection order (record-only)

1. Ada hard binding (if Ada identity / Ada backend)
2. Else explicit named model/effort (点名)
3. Else weighted sample among positive `weight_pct` tiers (RNG injectable for tests)

If Ada and 点名 disagree → reason `HARD_BINDING_CONFLICT` is **recorded**; submit
still proceeds with the caller’s actual route.

## Ledger

Optional JSONL append (best-effort):

- `HARNESS_SHADOW_LEDGER` — absolute path
- default: `$HARNESS_HOME/shadow/ledger.jsonl` (example/private deploy dir, not a
  real box path in this repo)
- `HARNESS_SHADOW_LEDGER_DISABLE=1` skips writing

## Boundary vs P2 (enforcement)

| | P1 shadow (this doc) | P2+ |
|---|---|---|
| Behavior change | none | may reject / force route |
| `HARD_BINDING_CONFLICT` | record only | reject / escalate |
| `decision_*` on Mac API | not used | required when new mode enabled |
| Mac bridge unknown fields | unchanged | coordinated protocol bump |
| UI / public write panel | out of scope | later phase |
| Config truth | single example/policy file via env | box published snapshot (no dual source) |

Module: [`dispatcher/shadow_route.py`](../dispatcher/shadow_route.py).
Wiring: [`dispatcher/direct_ssh_client.py`](../dispatcher/direct_ssh_client.py) `submit`.
