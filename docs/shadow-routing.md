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
and never fails admission. Shadow runs **after** job enqueue; policy/ledger I/O
is hard-bounded (`HARNESS_SHADOW_IO_TIMEOUT`, default 0.5s). Timeout or I/O
failure discards the suggestion (error on `job.shadow`) and never delays or
fails dispatch.

## Locked policy (example)

Bundled at [`dispatcher/shadow_policy.example.json`](../dispatcher/shadow_policy.example.json):

| Tier id   | weight_pct | engine | model                 | effort |
|-----------|------------|--------|-----------------------|--------|
| astra     | 20         | codex  | gpt-6-astra           | medium |
| sol       | 50         | codex  | gpt-6.1-sol           | medium |
| deepseek  | 30         | claude | deepseek-v4.1-flash   | —      |

- **Hard bindings** (fully JSON-driven under `hard_bindings`): each entry lists
  `identities` / `backends` matchers plus suggested `engine` / `model` /
  `effort` / `preferred_backend`. The OSS example uses fictional `bot_a` →
  BotA / `bot-a-*` backends. Changing alias or backend requires only a policy
  edit — no code change. Unmatched identities such as BotB / LegacyBot are not
  hard-bound and fall through to named / weight sample.
- **Private deploy**: real role→route mappings belong only in the private
  `$HARNESS_HOME/shadow_policy.json` (or `HARNESS_SHADOW_POLICY`); do not put
  production bot names or backends back into the OSS example.
- **deny_targets**: empty minimal set in the example.
- **OpenRouter `SCHEME_8`**: retained in policy as a banned scheme
  (`panel`/`active` = banned); shadow records `OPENROUTER_SCHEME_BANNED` when
  markers match. It is not offered as an active/panel choice.

Override with `HARNESS_SHADOW_POLICY`. Optional bot identity:
request field `bot` (stripped from `.request`) or `HARNESS_SHADOW_BOT`.

## Suggested selection order (record-only)

1. Hard binding (first `hard_bindings` entry whose identities/backends match)
2. Else explicit named model/effort (点名)
3. Else weighted sample among positive `weight_pct` tiers (RNG injectable for tests)

If a hard binding and 点名 disagree → reason `HARD_BINDING_CONFLICT` is
**recorded**; submit still proceeds with the caller’s actual route.

## Ledger

Optional JSONL append (best-effort, same I/O bound as policy load):

- `HARNESS_SHADOW_LEDGER` — absolute path
- default: `$HARNESS_HOME/shadow/ledger.jsonl` (example/private deploy dir, not a
  real box path in this repo)
- `HARNESS_SHADOW_LEDGER_DISABLE=1` skips writing
- `HARNESS_SHADOW_IO_TIMEOUT` — seconds (default `0.5`); applies to attach

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
