"""Native agent CLI adapters for the Mac HTTP bridge (Codex CLI and Claude Code CLI).

Model selection (optional, per job):
  * model:  model slug, optionally with ':effort' suffix, e.g. 'gpt-6-astra:medium'
  * effort: reasoning effort, e.g. 'medium' (mac-codex only)
Omitting both keeps the plain command line (the CLI's local config default).
All paths come from bridge_config (config.toml); nothing host-specific is hard-coded.
"""
import glob
import json
import os
import re
import shlex

from bridge_config import CONFIG

_WS = CONFIG['workspace']['path']
_CODEX = CONFIG['codex']
_CLAUDE = CONFIG['claude']

BACKENDS = {
    'mac-codex': dict(engine='codex', workspace=_WS, entry=_CODEX['bin'],
                      account=os.environ.get('USER', ''), host=CONFIG['server']['host_label']),
    'mac-claudecode': dict(engine='claude', workspace=_WS, entry=_CLAUDE['bin'],
                           account=os.environ.get('USER', ''), host=CONFIG['server']['host_label']),
}
CODEX_HOME = _CODEX['home']
CLAUDE_ENV_MISSING = 'claude env script missing'
CLAUDE_SWITCH_FAILED_EXIT = 79

EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_FULL = EFFORTS
_NO_ULTRA = EFFORTS[:-1]
# Fallback allowlist used only when ~/.codex/models_cache.json is unreadable.
# It is an example snapshot of `codex debug models`; the live cache always takes precedence.
CODEX_MODELS = {
    'gpt-6.1-sol': _FULL, 'gpt-6-astra': _FULL, 'gpt-6-sol': _FULL,
    'gpt-6-luna': _NO_ULTRA, 'gpt-5.6-sol': _FULL, 'gpt-5.6-luna': _NO_ULTRA,
}
CODEX_SLUG_RE = re.compile(r'[a-z0-9][a-z0-9.\-]{0,63}')
CLAUDE_MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,79}')
SWITCH_MODEL_RE = re.compile(r'[A-Za-z0-9._\-\[\] +]{1,80}')


class ModelError(ValueError):
    """Client-facing model/effort validation error (safe to echo back)."""


def codex_models():
    """Static allowlist, refreshed from the local Codex models cache when readable."""
    models = dict(CODEX_MODELS)
    try:
        with open(os.path.join(CODEX_HOME, 'models_cache.json'), encoding='utf-8') as f:
            data = json.load(f)
        items = data.get('models', data) if isinstance(data, dict) else data
        for item in items if isinstance(items, list) else []:
            slug = item.get('slug') if isinstance(item, dict) else None
            levels = item.get('supported_reasoning_levels') if isinstance(item, dict) else None
            if not isinstance(slug, str) or not CODEX_SLUG_RE.fullmatch(slug) or not isinstance(levels, list):
                continue
            efforts = tuple(e for e in ((x.get('effort') if isinstance(x, dict) else x) for x in levels)
                            if e in EFFORTS)
            if efforts:
                models[slug] = efforts
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return models


def normalize_selection(backend, model=None, effort=None):
    """Return (model, effort) validated for backend; (None, None) means local default."""
    if backend not in BACKENDS:
        raise ModelError('Unsupported backend; only mac-codex and mac-claudecode are allowed')
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ModelError('model must be nonempty text')
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise ModelError('effort must be nonempty text')
    if BACKENDS[backend]['engine'] == 'claude':
        if effort is not None:
            raise ModelError('effort is not supported for mac-claudecode')
        if model is None:
            return None, None
        if _CLAUDE['model_mode'] == 'switch':
            if not SWITCH_MODEL_RE.fullmatch(model):
                raise ModelError('model must be a valid switch-helper selector (1..80 characters)')
            return model, None
        if not CLAUDE_MODEL_RE.fullmatch(model):
            raise ModelError('Invalid Claude model name (1..80 of A-Z a-z 0-9 . _ - [ ])')
        return model, None
    if model is not None and ':' in model:
        model, _, suffix = model.partition(':')
        if effort is not None and effort != suffix:
            raise ModelError('model ":effort" suffix conflicts with effort field')
        effort = suffix
    model = model.strip() if model is not None else None
    effort = effort.strip().lower() if effort is not None else None
    if effort is not None and effort not in EFFORTS:
        raise ModelError('effort must be one of: ' + ', '.join(EFFORTS))
    models = codex_models()
    if model is not None and (not CODEX_SLUG_RE.fullmatch(model) or model not in models):
        raise ModelError('Unknown Codex model; allowed: ' + ', '.join(sorted(models)))
    if effort is not None and model is not None and effort not in models[model]:
        raise ModelError(f'{model} supports effort: ' + ', '.join(models[model]))
    return model, effort


def _claude_script(model):
    """Shell preamble for the Claude login shell. Every interpolated value is shell-quoted."""
    parts = []
    env_script = _CLAUDE.get('env_script') or ''
    if env_script:
        s = shlex.quote(env_script)
        parts.append(f"[ -r {s} ] || {{ echo {shlex.quote(CLAUDE_ENV_MISSING)} >&2; exit 78; }}; "
                     f"source {s} || exit 78; ")
    prepend = [p for p in (_CLAUDE.get('path_prepend') or []) if p]
    if prepend:
        parts.append('export PATH=' + shlex.quote(':'.join(prepend)) + ':"$PATH"; ')
    parts.append('export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1; ')
    if model is not None and _CLAUDE['model_mode'] == 'switch':
        helper = shlex.quote(_CLAUDE['switch_helper'])
        parts.append(f'SWITCH_OUT=$({helper} export {shlex.quote(model)}) || exit {CLAUDE_SWITCH_FAILED_EXIT}; '
                     f'eval "$SWITCH_OUT" || exit {CLAUDE_SWITCH_FAILED_EXIT}; unset SWITCH_OUT; ')
    parts.append('cd ' + shlex.quote(_WS) + ' && exec ' + shlex.quote(_CLAUDE['bin']) + ' "$@"')
    return ''.join(parts)


def command_for(backend, session_id=None, model=None, effort=None):
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError('Unsupported backend; only mac-codex and mac-claudecode are allowed')
    model, effort = normalize_selection(backend, model, effort)
    if backend == 'mac-claudecode':
        if _CLAUDE['model_mode'] == 'switch' and model is not None and not _CLAUDE.get('switch_helper'):
            raise ValueError('claude.model_mode = "switch" needs claude.switch_helper')
        args = [_CLAUDE['shell'], '-lc', _claude_script(model), 'claude-shell',
                '-p', '--output-format', 'json', '--permission-mode', _CLAUDE['permission_mode']]
        if model is not None and _CLAUDE['model_mode'] != 'switch':
            args += ['--model', model]
        if session_id:
            args += ['--resume', session_id]
        return args
    b = BACKENDS[backend]
    args = [b['entry'], 'exec', '--skip-git-repo-check', '--json',
            '--sandbox', _CODEX['sandbox'], '-c', f'approval_policy="{_CODEX["approval_policy"]}"',
            '-C', b['workspace']]
    if model is not None:
        args += ['-m', model]
    if effort is not None:
        args += ['-c', f'model_reasoning_effort="{effort}"']
    args += ['resume', session_id, '-'] if session_id else ['-']
    return args


def codex_config_default():
    """Top-level model / model_reasoning_effort from config.toml (no secrets read out)."""
    out = {'model': None, 'effort': None}
    try:
        with open(os.path.join(CODEX_HOME, 'config.toml'), encoding='utf-8') as f:
            for line in f:
                if line.lstrip().startswith('['):
                    break
                m = re.match(r'\s*(model|model_reasoning_effort)\s*=\s*"([^"]*)"', line)
                if m:
                    out['model' if m.group(1) == 'model' else 'effort'] = m.group(2)
    except OSError:
        pass
    return out


def codex_effective(thread_id):
    """Last turn_context model/effort recorded by Codex for the thread, if found."""
    if not isinstance(thread_id, str) or not re.fullmatch(r'[0-9a-fA-F\-]{8,64}', thread_id):
        return None
    files = sorted(glob.glob(os.path.join(CODEX_HOME, 'sessions', '*', '*', '*', f'rollout-*{thread_id}.jsonl')))
    if not files:
        return None
    found = None
    try:
        with open(files[-1], encoding='utf-8') as f:
            for line in f:
                if '"turn_context"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get('payload', event) if isinstance(event, dict) else {}
                if isinstance(payload, dict) and payload.get('model'):
                    found = {'model': payload.get('model'), 'effort': payload.get('effort')}
    except OSError:
        return None
    return found


def parse_events(engine, stdout):
    if engine == 'claude':
        try:
            # Startup scripts may emit text or JSON before the final result.
            decoder = json.JSONDecoder()
            event = None
            offset = 0
            while (start := stdout.find('{', offset)) != -1:
                try:
                    candidate, end = decoder.raw_decode(stdout, start)
                except ValueError:
                    offset = start + 1
                    continue
                offset = end
                if isinstance(candidate, dict) and candidate.get('type') == 'result':
                    event = candidate
            if event is None:
                raise ValueError('Expected a Claude result object')
            session = event.get('session_id')
            result = event.get('result', '')
            denials = event.get('permission_denials', [])
            if (event.get('type') != 'result' or not isinstance(session, str) or not session
                    or not isinstance(result, str) or not isinstance(denials, list)):
                raise ValueError('Invalid Claude result fields')
            usage = event.get('modelUsage')
            models = sorted(usage) if isinstance(usage, dict) else []
            return dict(session_id=session, result=result[-48000:],
                        failed=bool(event.get('is_error')) or event.get('subtype', 'success') != 'success',
                        permission_denials=denials, models=models, warnings=[])
        except (ValueError, TypeError):
            return dict(session_id=None, result='', failed=True, permission_denials=[], models=[], warnings=[])
    session = None
    messages = []
    warnings = []
    failed = False
    terminal = False
    denials = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            failed = True
            continue
        if event.get('type') == 'turn.completed':
            terminal = True
        if event.get('type') == 'thread.started':
            if isinstance(event.get('thread_id'), str) and event['thread_id']:
                session = event['thread_id']
            else:
                failed = True
        item = event.get('item') if isinstance(event.get('item'), dict) else {}
        if event.get('type') == 'item.completed' and not item:
            failed = True
        if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
            if isinstance(item.get('text'), str):
                messages.append(item['text'])
            else:
                failed = True
        if event.get('type') == 'item.completed' and item.get('type') == 'error':
            # Non-fatal notices, e.g. "session was recorded with model X but is resuming with Y".
            warnings.append(str(item.get('message', ''))[:500])
        if event.get('type') in ('error', 'turn.failed'):
            failed = True
    return dict(session_id=session, result='\n'.join(messages)[-48000:],
                failed=failed or not terminal, permission_denials=denials, models=[], warnings=warnings)
