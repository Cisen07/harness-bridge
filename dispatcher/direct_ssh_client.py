"""SSH-mode dispatcher: run native agent CLIs (Codex / Claude Code) on Linux hosts over Tailscale + OpenSSH.

Runs on the dispatcher box. No daemon is needed on the target host: each job is a detached local
worker process that SSHes in, runs the CLI in a login zsh, parses the output and writes
$HARNESS_HOME/direct-jobs/<job_id>.json (0600). Optional `wake` pushes the terminal state to a webhook.

Configuration (nothing host-specific is hard-coded):
  HARNESS_HOME            base dir (default ~/harness): bin/tailscale, run/tailscaled.sock, ssh/, direct-jobs/
  HARNESS_TARGETS         targets file (default $HARNESS_HOME/targets.json, see targets.example.json)
  HARNESS_SSH_PROXY       "tailscale-nc" (default: userspace tailscaled via `tailscale nc`) or "none"
  HARNESS_WAKE_URL_PREFIX required for wake; exact HTTPS origin and optional path boundary
  HARNESS_WAKE_KEY_ENV    permitted key environment variable (default HARNESS_WAKE_KEY)
Usage: targets | submit (JSON on stdin) | status JOB_ID
"""
import fcntl
import json
import os
import re
import pathlib
import shlex
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit as urlparse
import uuid

BASE = pathlib.Path(os.environ.get('HARNESS_HOME') or os.path.expanduser('~/harness'))
STATE = BASE / 'direct-jobs'
TARGETS_FILE = pathlib.Path(os.environ.get('HARNESS_TARGETS') or BASE / 'targets.json')
SSH_PROXY = os.environ.get('HARNESS_SSH_PROXY', 'tailscale-nc')
TARGET_FIELDS = ('host', 'user', 'workspace', 'entry', 'engine', 'ssh_key', 'switch_helper')


def load_targets(path=TARGETS_FILE):
    """{backend: (host, account, workspace, entry, engine, ssh key path, switch helper or None)}."""
    try:
        raw = json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return {}
    out = {}
    for name, t in raw.items():
        if name.startswith('_'):
            continue  # comments
        missing = [f for f in TARGET_FIELDS[:6] if not t.get(f)]
        if missing or t['engine'] not in ('codex', 'claude') or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,40}', name):
            raise ValueError('Invalid target %r (missing %s)' % (name, ', '.join(missing) or 'valid engine/name'))
        key = pathlib.Path(os.path.expanduser(t['ssh_key']))
        key = key if key.is_absolute() else BASE / key
        out[name] = (t['host'], t['user'], t['workspace'], t['entry'], t['engine'], str(key),
                     t.get('switch_helper') or None)
    return out


TARGETS = load_targets()
MODEL_RE = re.compile(r'^[A-Za-z0-9._\-\[\] +]{1,80}$')
EXPORT_FAILED = 3
# codex model selection: "slug" or "slug:effort", optional effort; omitted = host config.toml default.
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
CODEX_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9.\-]{0,63}$')
_ALL, _NO_ULTRA = EFFORTS, EFFORTS[:-1]
# Example fallback snapshot of `codex debug models`; hidden internal codex-auto-review is excluded.
# Refreshed read-only from the host's ~/.codex/models_cache.json when stale.
CODEX_MODELS = {
    'gpt-6.1-sol': _ALL, 'gpt-6-astra': _ALL, 'gpt-6-sol': _ALL,
    'gpt-6-luna': _NO_ULTRA, 'gpt-5.6-sol': _ALL, 'gpt-5.6-luna': _NO_ULTRA,
}
CODEX_EXCLUDE = {'codex-auto-review'}
MODELS_CACHE_TTL = 6 * 3600


def write(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def job_path(job_id):
    return STATE / (str(uuid.UUID(job_id)) + '.json')


def codex_flags(model=None, effort=None):
    flags = ['-m', model] if model else []
    if effort:
        flags += ['-c', 'model_reasoning_effort="' + effort + '"']
    return flags


def ssh_args(backend, remote):
    host, user, cwd, entry, engine, key, switch = TARGETS[backend]
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', v) for v in (host, user)):
        raise ValueError('Invalid SSH host or user')
    args = ['ssh', '-T', '-i', key,
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(BASE / 'ssh/known_hosts')]
    if SSH_PROXY == 'tailscale-nc':
        proxy = shlex.quote(str(BASE / 'bin/tailscale')) + ' --socket=' + shlex.quote(str(BASE / 'run/tailscaled.sock')) + ' nc %h %p'
        args += ['-o', 'ProxyCommand=' + proxy]
    return args + ['--', user + '@' + host, remote]


def command(backend, session=None, model=None, effort=None):
    host, user, cwd, entry, engine, key, switch = TARGETS[backend]
    if engine == 'codex':
        # `codex exec [-m M] [-c ...] -` / `codex exec resume [-m M] [-c ...] SID -` (per `exec resume --help`).
        args = [entry, 'exec', '--skip-git-repo-check', '--json', '-C', cwd]
        flags = codex_flags(model, effort)
        args += ['resume'] + flags + [session, '-'] if session else flags + ['-']
        model = None  # the switch-helper export below is for claude only
    else:
        args = [entry, '-p', '--verbose', '--output-format', 'stream-json', '--permission-mode', 'acceptEdits']
        if session:
            args += ['--resume', session]
    select = ''
    if model:
        # Per-process scheme: capture export output, check status, then eval; never touches active.zsh.
        select = ('{ out=$(' + shlex.quote(switch) + ' export ' + shlex.quote(model) + ') || { echo '
                  + shlex.quote('model switch helper export failed for model ' + model) + ' >&2; exit '
                  + str(EXPORT_FAILED) + '; }; } && eval "$out" && unset out && ')
    remote = 'zsh -lic ' + shlex.quote('cd -- ' + shlex.quote(cwd) + ' && ' + select + 'exec ' + shlex.join(args))
    return ssh_args(backend, remote)


# Read-only helpers for codex targets (python3 on the host; no config or credential files are read).
_MODELS_PY = r'''
import json, os
d = json.load(open(os.path.expanduser("~/.codex/models_cache.json")))
out = {}
for m in (d.get("models", []) if isinstance(d, dict) else d):
    lv = [x.get("effort") if isinstance(x, dict) else x for x in m.get("supported_reasoning_levels") or []]
    out[m.get("slug")] = dict(efforts=lv, visibility=m.get("visibility"), default_effort=m.get("default_reasoning_level"))
print(json.dumps(out))
'''
_ROLLOUT_PY = r'''
import glob, json, os, sys
fs = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*-" + sys.argv[1] + ".jsonl")))
seen = []
for l in (open(fs[-1], encoding="utf-8") if fs else []):
    if '"turn_context"' not in l:
        continue
    try:
        e = json.loads(l)
    except ValueError:
        continue
    p = e.get("payload") if e.get("type") == "turn_context" else None
    if isinstance(p, dict) and p.get("model"):
        seen.append([p.get("model"), p.get("effort")])
print(json.dumps(dict(found=bool(fs), turns=seen)))
'''


def remote_python(backend, code, *argv):
    remote = 'python3 -c ' + shlex.quote(code) + ''.join(' ' + shlex.quote(a) for a in argv)
    p = subprocess.run(ssh_args(backend, remote), capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    if p.returncode:
        raise RuntimeError('remote read-only query failed (exit %d)' % p.returncode)
    return json.loads(p.stdout.strip().splitlines()[-1])


def codex_models(backend, refresh=False):
    """Allowlist {slug: efforts} from the host's models cache (cached locally), else the static snapshot."""
    cache = STATE / (backend + '-models.json')
    try:
        data = json.loads(cache.read_text())
        if refresh or time.time() - data['fetched_at'] > MODELS_CACHE_TTL:
            raise ValueError('stale')
    except (OSError, ValueError, KeyError, TypeError):
        try:
            data = dict(fetched_at=time.time(), source='host ~/.codex/models_cache.json',
                        models=remote_python(backend, _MODELS_PY))
            write(cache, data)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return {k: list(v) for k, v in CODEX_MODELS.items()}, 'static snapshot (live query failed)'
    models = {k: [e for e in v.get('efforts', []) if e in EFFORTS] for k, v in data['models'].items()
              if isinstance(k, str) and CODEX_SLUG_RE.match(k) and k not in CODEX_EXCLUDE}
    return {k: v for k, v in models.items() if v}, data.get('source', 'cache')


def normalize_codex(backend, model, effort):
    """Return validated (model, effort); (None, None) means the host's config.toml default."""
    if model is not None and (not isinstance(model, str) or not model.strip() or model != model.strip()):
        raise ValueError('Invalid model')
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise ValueError('Invalid effort')
    if model is not None and ':' in model:
        model, _, suffix = model.partition(':')
        if effort is not None and effort.strip().lower() != suffix.lower():
            raise ValueError('model ":effort" suffix conflicts with effort field')
        effort = suffix
    effort = effort.strip().lower() if effort is not None else None
    if effort is not None and effort not in EFFORTS:
        raise ValueError('effort must be one of: ' + ', '.join(EFFORTS))
    if model is not None:
        models, source = codex_models(backend)
        if not CODEX_SLUG_RE.match(model) or model not in models:
            raise ValueError('Unknown Codex model %r; allowed (%s): %s' % (model, source, ', '.join(sorted(models))))
        if effort is not None and effort not in models[model]:
            raise ValueError('%s supports effort: %s' % (model, ', '.join(models[model])))
    return model or None, effort


def session_models_path(backend):
    return STATE / (backend + '-session-models.json')


def load_session_models(backend):
    try:
        return json.loads(session_models_path(backend).read_text())
    except (OSError, ValueError):
        return {}


def parse(engine, text):
    session, result, failed, denials, models, warnings = None, [], False, [], [], []
    terminal = False
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            raise ValueError('Invalid agent event object')
        if engine == 'codex':
            if e.get('type') == 'turn.completed':
                terminal = True
            if e.get('type') == 'item.completed':
                item = e.get('item')
                if not isinstance(item, dict) or (item.get('type') == 'agent_message' and not isinstance(item.get('text'), str)):
                    raise ValueError('Invalid Codex item fields')
            if e.get('type') == 'thread.started':
                if not isinstance(e.get('thread_id'), str) or not e['thread_id']:
                    raise ValueError('Invalid Codex thread ID')
                session = e['thread_id']
            if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'agent_message':
                result.append(e['item'].get('text', ''))
            if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'error':
                # Non-fatal notices, e.g. "session was recorded with model X but is resuming with Y".
                warnings.append(str(e['item'].get('message', ''))[:500])
            if e.get('type') in ('turn.failed', 'error'):
                failed = True
                terminal = True
        else:
            session = e.get('session_id') or session
            if e.get('type') == 'system' and e.get('subtype') == 'init' and e.get('model'):
                models.append(str(e['model']))
            if e.get('type') == 'result' and isinstance(e.get('modelUsage'), dict):
                models += [str(m) for m in e['modelUsage']]
            if e.get('type') == 'result':
                if (not isinstance(e.get('session_id'), str) or not e['session_id']
                        or not isinstance(e.get('result', ''), str)
                        or not isinstance(e.get('permission_denials', []), list)):
                    raise ValueError('Invalid Claude result fields')
                terminal = True
                result = [e.get('result', '')]
                failed = bool(e.get('is_error')) or e.get('subtype', 'success') != 'success'
                denials = e.get('permission_denials', [])
    if not terminal:
        raise ValueError('Missing agent terminal event')
    out = dict(session_id=session, result='\n'.join(result)[-48000:], failed=failed, permission_denials=denials,
               observed_models=list(dict.fromkeys(models)))
    if warnings:
        out['warnings'] = warnings
    return out


def record_codex_session(job):
    """Observed model/effort from the host rollout's last turn_context; remember the session's selection."""
    try:
        info = remote_python(job['backend'], _ROLLOUT_PY, str(uuid.UUID(job['session_id'])))
        if info.get('turns'):
            model, effort = info['turns'][-1]
            job['observed_models'] = [model]
            job['observed_effort'] = effort
            job['observed_source'] = 'host rollout turn_context (last turn)'
        else:
            job['observed_note'] = 'no turn_context found in host rollout'
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as e:
        job['observed_note'] = 'observed model lookup failed: ' + str(e)[:200]
    try:
        with open(STATE / (job['backend'] + '.sessions.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            table = load_session_models(job['backend'])
            table[job['session_id']] = dict(model=job.get('model'), effort=job.get('effort'),
                                            model_source=job.get('model_source'),
                                            observed_model=(job.get('observed_models') or [None])[0],
                                            observed_effort=job.get('observed_effort'),
                                            job_id=job['job_id'], updated_at=time.time())
            write(session_models_path(job['backend']), table)
    except OSError as e:
        job['session_state_note'] = 'could not save session model state: ' + str(e)[:200]


WAKE_URL_PREFIX = os.environ.get('HARNESS_WAKE_URL_PREFIX', '')
WAKE_KEY_ENV = os.environ.get('HARNESS_WAKE_KEY_ENV', 'HARNESS_WAKE_KEY')
ENV_NAME_RE = re.compile(r'^[A-Z_][A-Z0-9_]{0,63}$')


def validate_wake(wake):
    """Optional opt-in push on terminal state: {"url": routine webhook URL, "key_env": env var holding its key}."""
    if not isinstance(wake, dict) or set(wake) - {'url', 'key_env'}:
        raise ValueError('wake must be {"url", "key_env"}')
    url, key_env = wake.get('url'), wake.get('key_env')
    def endpoint(value):
        if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError('Invalid wake URL configuration')
        parsed = urlparse(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.query
                or any('..' in part or '%' in part or ';' in part for part in parsed.path.split('/'))
                or '%' in parsed.netloc or '\\' in value):
            raise ValueError('wake URL must be HTTPS without userinfo, query or fragment')
        return parsed, (parsed.scheme, parsed.hostname.lower(), parsed.port if parsed.port is not None else 443)

    target, origin = endpoint(url)
    allowed, allowed_origin = endpoint(WAKE_URL_PREFIX)
    base_path = allowed.path.rstrip('/')
    if origin != allowed_origin or (base_path and target.path != base_path
                                   and not target.path.startswith(base_path + '/')):
        raise ValueError('wake.url does not match the configured HTTPS origin/path')
    if (not isinstance(key_env, str) or not ENV_NAME_RE.fullmatch(key_env)
            or key_env != WAKE_KEY_ENV):
        raise ValueError('wake.key_env must match HARNESS_WAKE_KEY_ENV')
    return dict(url=url, key_env=key_env)


class NoWebhookRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_wake(job, wake):
    """POST job_id/status only (never the result text); failures are recorded, never fatal. Key never stored."""
    try:
        wake = validate_wake(wake)
    except ValueError:
        return dict(ok=False, error='Invalid wake configuration')
    key = os.environ.get(wake['key_env'])
    if not key:
        return dict(ok=False, error='wake key env not set in worker environment')
    body = json.dumps(dict(job_id=job['job_id'], backend=job['backend'], status=job['status'],
                           exit_code=job.get('exit_code'), model=job.get('model'), effort=job.get('effort'),
                           finished_at=time.strftime('%Y-%m-%d %H:%M:%S %Z'))).encode()
    req = urllib.request.Request(wake['url'], data=body, method='POST',
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    for attempt in range(3):
        try:
            with urllib.request.build_opener(NoWebhookRedirect()).open(req, timeout=20) as resp:
                return dict(ok=200 <= resp.status < 300, http=resp.status, attempts=attempt + 1)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return dict(ok=False, http=e.code, attempts=attempt + 1)
            last = 'http %d' % e.code
        except (OSError, ValueError) as e:
            last = type(e).__name__
        time.sleep(5)
    return dict(ok=False, error=last, attempts=3)


def worker(job_id):
    path = job_path(job_id)
    job = json.loads(path.read_text())
    job.update(status='running', worker_pid=os.getpid())
    write(path, job)
    request = {}
    try:
        request = json.loads((STATE / (job_id + '.request')).read_text())
        engine = TARGETS[job['backend']][4]
        if engine == 'codex':
            args = command(job['backend'], request.get('session_id'), job.get('model'), job.get('effort'))
        else:
            args = command(job['backend'], request.get('session_id'), request.get('model'))
        p = subprocess.run(args, input=request['prompt'], capture_output=True, text=True, timeout=3600)
        r = parse(engine, p.stdout)
        previous = request.get('session_id')
        if previous and r['session_id'] and previous != r['session_id']:
            raise RuntimeError('Remote agent returned a different session ID')
        job.update(status='completed' if p.returncode == 0 and not r['failed'] else 'failed',
                   exit_code=p.returncode, session_id=r['session_id'] or previous, result=r['result'])
        if r['observed_models']:
            job['observed_models'] = r['observed_models']
        if r.get('warnings'):
            job['warnings'] = r['warnings']
        if engine == 'codex' and job['session_id']:
            record_codex_session(job)
        if r['permission_denials']:
            job.update(status='needs_attention', permission_denials=r['permission_denials'])
        if p.returncode and not r['result']:
            job['error'] = 'Remote SSH or native CLI failed; inspect the private diagnostic file.'
            if request.get('model') and p.returncode == EXPORT_FAILED:
                lines = [l for l in p.stderr.splitlines() if '\t' in l or 'export failed' in l]
                job['error'] = 'model switch helper export failed; no fallback to default. Candidates:\n' + '\n'.join(lines)[-3000:]
            diagnostic = STATE / (job_id + '.stderr')
            diagnostic.write_text(p.stderr)
            diagnostic.chmod(0o600)
    except Exception as e:
        # Detached workers must persist a terminal state even on malformed protocol data.
        job.update(status='failed', error='Agent execution or output parsing failed (' + type(e).__name__ + ')')
    write(path, job)
    if request.get('wake'):
        job['wake'] = dict(send_wake(job, request['wake']), at=time.time())
        write(path, job)


def main():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if not TARGETS:
        raise SystemExit('No targets configured: create %s (see targets.example.json)' % TARGETS_FILE)
    if sys.argv[1] == 'targets':
        info = {k: dict(host=v[0], account=v[1], workspace=v[2], engine=v[4], transport='direct-tailscale-ssh',
                        supports_model=v[6] is not None or v[4] == 'codex')
                for k, v in TARGETS.items()}
        for k, v in TARGETS.items():
            if v[4] == 'codex':
                models, source = codex_models(k)
                info[k].update(supports_effort=True, models=models, models_source=source,
                               selector='model "slug" or "slug:effort"; optional effort in ' + '|'.join(EFFORTS)
                               + '; omitted = host config.toml default; resume without model reuses the session model')
        print(json.dumps(info, ensure_ascii=False))
    elif sys.argv[1] == 'submit':
        req = json.load(sys.stdin)
        if set(req) - {'backend', 'prompt', 'session_id', 'model', 'effort', 'wake'}:
            raise ValueError('Unknown request field')
        backend = req['backend']
        if backend not in TARGETS or not isinstance(req['prompt'], str) or not req['prompt'].strip():
            raise ValueError('Invalid backend or prompt')
        if req.get('session_id'):
            req['session_id'] = str(uuid.UUID(req['session_id']))
        if req.get('wake') is not None:
            req['wake'] = validate_wake(req['wake'])
        model = req.get('model')
        extra = {}
        if TARGETS[backend][4] == 'codex':
            effort = req.get('effort')
            model = None if model == '' else model
            effort = None if effort == '' else effort
            model, effort = normalize_codex(backend, model, effort)
            req.pop('model', None)
            req.pop('effort', None)
            if model is not None or effort is not None:
                extra = dict(effort=effort, model_source='request')
                if req.get('session_id') and model is None:
                    prev = load_session_models(backend).get(req['session_id']) or {}
                    model = prev.get('model') if prev.get('model_source') in ('request', 'session') else None
                    if model is None:
                        extra['model_note'] = 'effort-only override; model is the host config.toml default'
            elif req.get('session_id'):
                prev = load_session_models(backend).get(req['session_id'])
                if prev and prev.get('model_source') in ('request', 'session') and (prev.get('model') or prev.get('effort')):
                    model = prev.get('model')
                    extra = dict(effort=prev.get('effort'), model_source='session')
                elif prev:
                    extra = dict(effort=None, model_source='default')
                else:
                    extra = dict(effort=None, model_source='default',
                                 model_note='session not in client state; resumed with host config.toml default')
            else:
                extra = dict(effort=None, model_source='default')
            if model is not None:
                normalize_codex(backend, model, extra.get('effort'))
            extra['codex_flags'] = codex_flags(model, extra.get('effort'))
        elif req.get('effort') not in (None, ''):
            raise ValueError('effort is only supported for codex backends')
        else:
            req.pop('effort', None)
            if model is None or model == '':
                req.pop('model', None)
                model = None
            else:
                if TARGETS[backend][6] is None:
                    raise ValueError('Backend does not support model selection')
                if not isinstance(model, str) or not MODEL_RE.match(model) or model != model.strip():
                    raise ValueError('Invalid model')
        job_id = str(uuid.uuid4())
        job = dict(job_id=job_id, backend=backend, workspace=TARGETS[backend][2],
                   transport='direct-tailscale-ssh', status='queued', created_at=time.time(), model=model, **extra)
        write(STATE / (job_id + '.request'), req)
        write(job_path(job_id), job)
        subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), '_worker', job_id],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        print(json.dumps(job))
    elif sys.argv[1] == 'status':
        print(job_path(sys.argv[2]).read_text())
    elif sys.argv[1] == '_worker':
        worker(str(uuid.UUID(sys.argv[2])))
    else:
        raise ValueError('Use targets, submit (JSON on stdin; optional wake={url,key_env}), or status JOB_ID')


if __name__ == '__main__':
    main()
