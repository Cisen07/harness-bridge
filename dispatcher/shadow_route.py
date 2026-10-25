"""P1 shadow routing: suggest a tier/engine/model/effort and record reasons.

Shadow never changes task selection, never rejects submit, and never requires
decision_* fields. Compare suggested vs actual locally; optional ledger append
is best-effort and must not affect dispatch.

Policy/ledger I/O is bounded: attach runs under a hard timeout so slow or
blocking files never delay or fail admission.
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import threading
import time
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

DEFAULT_POLICY_PATH = pathlib.Path(__file__).resolve().with_name('shadow_policy.example.json')
DEFAULT_IO_TIMEOUT_SEC = 0.5
REASON_NAMED = 'NAMED_TARGET'
REASON_HARD_BINDING = 'HARD_BINDING'
REASON_HARD_BINDING_CONFLICT = 'HARD_BINDING_CONFLICT'
REASON_WEIGHT_SAMPLE = 'WEIGHT_SAMPLE'
REASON_DENY_TARGET = 'DENY_TARGET_HIT'
REASON_OPENROUTER_BANNED = 'OPENROUTER_SCHEME_BANNED'
REASON_ZERO_WEIGHT = 'NO_ELIGIBLE_TIER'
REASON_SHADOW_TIMEOUT = 'SHADOW_IO_TIMEOUT'
REASON_SHADOW_ERROR = 'SHADOW_IO_ERROR'


def default_policy_path() -> pathlib.Path:
    override = os.environ.get('HARNESS_SHADOW_POLICY')
    if override:
        return pathlib.Path(override)
    home = os.environ.get('HARNESS_HOME')
    if home:
        candidate = pathlib.Path(home) / 'shadow_policy.json'
        if candidate.is_file():
            return candidate
    return DEFAULT_POLICY_PATH


def shadow_io_timeout_sec(override: Optional[float] = None) -> float:
    if override is not None:
        return max(0.05, float(override))
    raw = os.environ.get('HARNESS_SHADOW_IO_TIMEOUT')
    if raw is None or raw == '':
        return DEFAULT_IO_TIMEOUT_SEC
    return max(0.05, float(raw))


def run_bounded(fn: Callable[[], Any], timeout_sec: float) -> Any:
    """Run fn in a daemon thread; raise TimeoutError if it does not finish in time."""
    box: dict[str, Any] = {'value': None, 'exc': None, 'done': False}

    def target() -> None:
        try:
            box['value'] = fn()
        except Exception as exc:  # noqa: BLE001 — surfaced to caller after join
            box['exc'] = exc
        finally:
            box['done'] = True

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout_sec)
    if not box['done']:
        raise TimeoutError('shadow I/O timed out after %.3fs' % timeout_sec)
    if box['exc'] is not None:
        raise box['exc']
    return box['value']


def load_policy(path: Optional[pathlib.Path] = None) -> dict:
    path = pathlib.Path(path or default_policy_path())
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('shadow policy must be a JSON object')
    return validate_policy(raw)


def validate_policy(policy: Mapping[str, Any]) -> dict:
    if int(policy.get('schema_version') or 0) != 1:
        raise ValueError('unsupported shadow policy schema_version')
    tiers = policy.get('tiers')
    if not isinstance(tiers, list) or not tiers:
        raise ValueError('shadow policy tiers required')
    total = 0
    ids = set()
    normalized_tiers = []
    for tier in tiers:
        if not isinstance(tier, Mapping):
            raise ValueError('invalid tier')
        tid = tier.get('id')
        weight = tier.get('weight_pct')
        if not isinstance(tid, str) or not tid or tid in ids:
            raise ValueError('tier id must be unique non-empty string')
        if not isinstance(weight, int) or weight < 0 or weight > 100:
            raise ValueError('weight_pct must be int 0..100')
        ids.add(tid)
        total += weight
        normalized_tiers.append({
            'id': tid,
            'weight_pct': weight,
            'engine': tier.get('engine'),
            'model': tier.get('model'),
            'effort': tier.get('effort'),
        })
    if total != 100:
        raise ValueError('tier weight_pct must sum to 100')
    deny = policy.get('deny_targets') or []
    if not isinstance(deny, list):
        raise ValueError('deny_targets must be a list')
    banned = policy.get('banned_schemes') or []
    if not isinstance(banned, list):
        raise ValueError('banned_schemes must be a list')
    hard = policy.get('hard_bindings') or {}
    if not isinstance(hard, Mapping):
        raise ValueError('hard_bindings must be an object')
    normalized_hard = {}
    for key, binding in hard.items():
        if not isinstance(key, str) or not key:
            raise ValueError('hard_bindings keys must be non-empty strings')
        if not isinstance(binding, Mapping):
            raise ValueError('hard_bindings values must be objects')
        normalized_hard[key] = dict(binding)
    return {
        'schema_version': 1,
        'mode': policy.get('mode') or 'shadow',
        'policy_revision': policy.get('policy_revision'),
        'tiers': normalized_tiers,
        'hard_bindings': normalized_hard,
        'deny_targets': list(deny),
        'banned_schemes': list(banned),
    }


def _norm(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    value = value.strip()
    return value or None


def _identity_matches_binding(bot: Optional[str], backend: Optional[str], binding: Mapping[str, Any]) -> bool:
    identities = {str(x).casefold() for x in (binding.get('identities') or [])}
    backends = {str(x) for x in (binding.get('backends') or [])}
    if bot and bot.casefold() in identities:
        return True
    if backend and backend in backends:
        return True
    return False


def _find_hard_binding(
    bot: Optional[str],
    backend: Optional[str],
    hard_bindings: Mapping[str, Any],
) -> tuple[Optional[str], Optional[dict]]:
    """First policy hard_bindings entry whose identities/backends match (JSON-driven)."""
    for binding_id, binding in hard_bindings.items():
        if not isinstance(binding, Mapping):
            continue
        if _identity_matches_binding(bot, backend, binding):
            return binding_id, dict(binding)
    return None, None


def _route_dict(engine=None, model=None, effort=None, backend=None, tier=None) -> dict:
    return {
        'tier': tier,
        'engine': engine,
        'model': model,
        'effort': effort,
        'backend': backend,
    }


def _matches_binding(actual: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    want_engine = binding.get('engine')
    want_model = binding.get('model')
    want_effort = binding.get('effort')
    if want_engine and actual.get('engine') and actual.get('engine') != want_engine:
        return False
    if want_model and actual.get('model') and actual.get('model') != want_model:
        return False
    if want_effort is not None and actual.get('effort') is not None and actual.get('effort') != want_effort:
        return False
    return True


def _named_conflicts_binding(named: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    """True when an explicit named choice disagrees with a hard binding."""
    if not named.get('named'):
        return False
    if named.get('model') and binding.get('model') and named.get('model') != binding.get('model'):
        return True
    if named.get('effort') is not None and binding.get('effort') is not None and named.get('effort') != binding.get('effort'):
        return True
    if named.get('engine') and binding.get('engine') and named.get('engine') != binding.get('engine'):
        return True
    backends = set(binding.get('backends') or [])
    backend = named.get('backend')
    if backend and backends and backend not in backends:
        return True
    return False


def pick_weighted_tier(tiers: Sequence[Mapping[str, Any]], rng: random.Random) -> Optional[dict]:
    eligible = [t for t in tiers if int(t.get('weight_pct') or 0) > 0]
    if not eligible:
        return None
    total = sum(int(t['weight_pct']) for t in eligible)
    draw = rng.randrange(total)
    cursor = 0
    for tier in eligible:
        cursor += int(tier['weight_pct'])
        if draw < cursor:
            return dict(tier)
    return dict(eligible[-1])


def detect_banned_scheme(haystacks: Sequence[Optional[str]], banned_schemes: Sequence[Mapping[str, Any]]) -> Optional[dict]:
    blob = ' '.join(h for h in haystacks if h).casefold()
    if not blob:
        return None
    for scheme in banned_schemes:
        markers = scheme.get('markers') or []
        for marker in markers:
            if str(marker).casefold() in blob:
                return {
                    'id': scheme.get('id'),
                    'kind': scheme.get('kind'),
                    'reason': scheme.get('reason') or REASON_OPENROUTER_BANNED,
                    'panel': scheme.get('panel'),
                    'active': scheme.get('active'),
                }
    return None


def suggest_route(
    *,
    backend: Optional[str] = None,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    bot: Optional[str] = None,
    named_model: Optional[bool] = None,
    named_effort: Optional[bool] = None,
    named_backend: bool = False,
    ai_switch_scheme: Optional[str] = None,
    policy: Optional[Mapping[str, Any]] = None,
    rng: Optional[random.Random] = None,
) -> dict:
    """Compute a shadow suggestion. Never raises for routing conflicts; returns reasons instead.

    Pass named_model/named_effort explicitly when actual model/effort may come from
    host defaults rather than an explicit request (点名). When omitted, presence of
    model/effort is treated as named for convenience in unit tests.
    """
    policy = validate_policy(policy or load_policy())
    rng = rng or random.Random()
    backend = _norm(backend)
    engine = _norm(engine)
    model = _norm(model)
    effort = _norm(effort)
    bot = _norm(bot)
    ai_switch_scheme = _norm(ai_switch_scheme)

    if named_model is None:
        named_model = model is not None
    if named_effort is None:
        named_effort = effort is not None
    named = bool(named_model or named_effort or named_backend)

    actual = _route_dict(engine=engine, model=model, effort=effort, backend=backend)
    reasons: list[str] = []
    notes: list[str] = []
    suggested = None
    selection = None

    binding_id, binding = _find_hard_binding(bot, backend, policy.get('hard_bindings') or {})

    banned = detect_banned_scheme(
        [model, backend, ai_switch_scheme, engine],
        policy.get('banned_schemes') or [],
    )
    if banned:
        reasons.append(banned.get('reason') or REASON_OPENROUTER_BANNED)
        notes.append('scheme %s marked banned from active/panel (shadow only)' % (banned.get('id') or '?'))

    deny_targets = {str(x) for x in (policy.get('deny_targets') or [])}
    if backend and backend in deny_targets:
        reasons.append(REASON_DENY_TARGET)
        notes.append('backend %s is in deny_targets (shadow record only; submit unchanged)' % backend)

    if binding_id and binding:
        suggested = _route_dict(
            engine=binding.get('engine'),
            model=binding.get('model'),
            effort=binding.get('effort'),
            backend=binding.get('preferred_backend') or backend,
            tier='%s-hard-binding' % binding_id,
        )
        selection = 'hard_binding'
        reasons.append(REASON_HARD_BINDING)
        named_view = {
            'named': named,
            'model': model if named_model else None,
            'effort': effort if named_effort else None,
            'engine': engine,
            'backend': backend,
        }
        if _named_conflicts_binding(named_view, binding) or (named and not _matches_binding(actual, binding)):
            reasons.append(REASON_HARD_BINDING_CONFLICT)
            notes.append(
                'named target conflicts with hard binding %s; shadow records only (no reject)' % binding_id
            )
        if named:
            reasons.append(REASON_NAMED)
    elif named:
        suggested = _route_dict(engine=engine, model=model, effort=effort, backend=backend, tier='named')
        selection = 'named'
        reasons.append(REASON_NAMED)
    else:
        tier = pick_weighted_tier(policy['tiers'], rng)
        if tier is None:
            suggested = _route_dict(engine=engine, model=model, effort=effort, backend=backend)
            selection = 'none'
            reasons.append(REASON_ZERO_WEIGHT)
            notes.append('no positive-weight tier available')
        else:
            suggested = _route_dict(
                engine=tier.get('engine'),
                model=tier.get('model'),
                effort=tier.get('effort'),
                backend=backend,
                tier=tier.get('id'),
            )
            selection = 'weight_sample'
            reasons.append(REASON_WEIGHT_SAMPLE)

    match = (
        suggested.get('engine') == actual.get('engine')
        and suggested.get('model') == actual.get('model')
        and suggested.get('effort') == actual.get('effort')
    )
    # Deduplicate reasons while preserving order.
    seen = set()
    ordered_reasons = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered_reasons.append(reason)

    return {
        'mode': 'shadow',
        'policy_revision': policy.get('policy_revision'),
        'selection': selection,
        'suggested': suggested,
        'actual': actual,
        'match': match,
        'reasons': ordered_reasons,
        'notes': notes,
        'bot': bot,
        'named': {
            'model': named_model,
            'effort': named_effort,
            'backend': named_backend,
        },
        'banned_scheme': banned,
        'hard_binding_id': binding_id,
    }


def build_shadow_record(
    suggestion: Mapping[str, Any],
    *,
    job_id: Optional[str] = None,
    backend: Optional[str] = None,
    created_at: Optional[float] = None,
) -> dict:
    return {
        'mode': 'shadow',
        'job_id': job_id,
        'backend': backend,
        'created_at': created_at if created_at is not None else time.time(),
        'policy_revision': suggestion.get('policy_revision'),
        'selection': suggestion.get('selection'),
        'suggested': suggestion.get('suggested'),
        'actual': suggestion.get('actual'),
        'match': suggestion.get('match'),
        'reasons': list(suggestion.get('reasons') or []),
        'notes': list(suggestion.get('notes') or []),
        'bot': suggestion.get('bot'),
        'named': suggestion.get('named'),
        'banned_scheme': suggestion.get('banned_scheme'),
        'hard_binding_id': suggestion.get('hard_binding_id'),
    }


def default_ledger_path() -> pathlib.Path:
    """Default ledger under HARNESS_HOME (example/private deploy dir — not a real box path)."""
    override = os.environ.get('HARNESS_SHADOW_LEDGER')
    if override:
        return pathlib.Path(override)
    home = os.environ.get('HARNESS_HOME') or os.path.expanduser('~/harness')
    return pathlib.Path(home) / 'shadow' / 'ledger.jsonl'


def append_ledger(record: Mapping[str, Any], path: Optional[pathlib.Path] = None) -> Optional[str]:
    """Best-effort JSONL append. Returns path written or None on skip/failure."""
    if os.environ.get('HARNESS_SHADOW_LEDGER_DISABLE') in ('1', 'true', 'yes'):
        return None
    path = pathlib.Path(path or default_ledger_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
    with path.open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return str(path)


def _attach_shadow_body(
    job: MutableMapping[str, Any],
    req: Mapping[str, Any],
    *,
    engine: Optional[str],
    bot: Optional[str],
    named_model: bool,
    named_effort: bool,
    policy: Optional[Mapping[str, Any]],
    rng: Optional[random.Random],
    ledger: bool,
    load_policy_fn: Optional[Callable[[], Mapping[str, Any]]],
) -> Optional[dict]:
    pol = policy if policy is not None else (load_policy_fn() if load_policy_fn else load_policy())
    suggestion = suggest_route(
        backend=job.get('backend') or req.get('backend'),
        engine=engine,
        model=job.get('model') if job.get('model') is not None else req.get('model'),
        effort=job.get('effort') if job.get('effort') is not None else req.get('effort'),
        bot=bot if bot is not None else req.get('bot') or os.environ.get('HARNESS_SHADOW_BOT'),
        named_model=named_model,
        named_effort=named_effort,
        named_backend=False,
        ai_switch_scheme=req.get('ai_switch_scheme') or os.environ.get('HARNESS_SHADOW_AI_SWITCH_SCHEME'),
        policy=pol,
        rng=rng,
    )
    record = build_shadow_record(
        suggestion,
        job_id=job.get('job_id'),
        backend=job.get('backend'),
        created_at=job.get('created_at'),
    )
    job['shadow'] = record
    if ledger:
        try:
            written = append_ledger(record)
            if written:
                job['shadow']['ledger_path'] = written
        except Exception as exc:  # noqa: BLE001 — shadow must not affect submit
            job['shadow']['ledger_error'] = str(exc)[:200]
    return record


def attach_shadow_to_job(
    job: MutableMapping[str, Any],
    req: Mapping[str, Any],
    *,
    engine: Optional[str] = None,
    bot: Optional[str] = None,
    named_model: bool = False,
    named_effort: bool = False,
    policy: Optional[Mapping[str, Any]] = None,
    rng: Optional[random.Random] = None,
    ledger: bool = True,
    load_policy_fn: Optional[Callable[[], Mapping[str, Any]]] = None,
    io_timeout: Optional[float] = None,
) -> Optional[dict]:
    """Compute shadow, mutate job['shadow'], optional ledger.

    Never raises to callers. Policy/ledger I/O runs under a hard timeout so
    blocking special files or slow disks cannot delay callers beyond the bound.
    Work happens on a scratch copy so a timed-out thread cannot overwrite the
    timeout/error record after admission has moved on.
    """
    timeout = shadow_io_timeout_sec(io_timeout)
    started = time.monotonic()
    scratch: dict[str, Any] = dict(job)

    def _run() -> Optional[dict]:
        return _attach_shadow_body(
            scratch, req,
            engine=engine, bot=bot,
            named_model=named_model, named_effort=named_effort,
            policy=policy, rng=rng, ledger=ledger,
            load_policy_fn=load_policy_fn,
        )

    try:
        record = run_bounded(_run, timeout)
        if 'shadow' in scratch:
            job['shadow'] = scratch['shadow']
        return record
    except TimeoutError as exc:
        job['shadow'] = {
            'mode': 'shadow',
            'error': str(exc)[:300],
            'reason': REASON_SHADOW_TIMEOUT,
            'job_id': job.get('job_id'),
            'elapsed_sec': round(time.monotonic() - started, 3),
        }
        return None
    except Exception as exc:  # noqa: BLE001 — shadow must not affect submit
        job['shadow'] = {
            'mode': 'shadow',
            'error': str(exc)[:300],
            'reason': REASON_SHADOW_ERROR,
            'job_id': job.get('job_id'),
            'elapsed_sec': round(time.monotonic() - started, 3),
        }
        return None
