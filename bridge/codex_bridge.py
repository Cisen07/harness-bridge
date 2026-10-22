"""harness-bridge: private Tailscale HTTP job API for native agent CLIs (Codex / Claude Code) on macOS.

- Listens only on the configured tailnet IP; every caller must be listed in access.allowed_sources
  and confirmed by `tailscale whois` (ACL tag or MagicDNS node name).
- Bounded worker pool; one queued/running job per session_id.
- On every terminal state (completed / failed / needs_attention / interrupted) it POSTs
  {job_id, status, backend, host} to the webhook in webhook.conf (best effort, daemon thread).
All host-specific values come from config.toml (see config.example.toml).
"""
import concurrent.futures
import datetime
import hmac
import stat
import http.server
import ipaddress
import json
import os
import pathlib
import signal
import subprocess
import threading
import uuid
import urllib.request
from urllib.parse import urlsplit
from agent_adapters import (BACKENDS, CLAUDE_ENV_MISSING, EFFORTS, ModelError, codex_config_default,
                            codex_effective, codex_models, command_for, normalize_selection, parse_events)
from bridge_config import CONFIG

BASE = pathlib.Path(__file__).resolve().parent
_SERVER = CONFIG['server']
WORKSPACE = pathlib.Path(CONFIG['workspace']['path'])
TAILSCALE = _SERVER['tailscale_bin']
HOST = _SERVER['bind_host']
PORT = int(_SERVER['port'])
HOST_LABEL = _SERVER['host_label']
JOB_TIMEOUT = int(_SERVER['job_timeout_seconds'])
MAX_BODY = int(_SERVER['max_body_bytes'])
WEBHOOK_CONF = pathlib.Path(_SERVER['webhook_conf'])
# Allowed callers: tailnet source IP -> requirement checked against `tailscale whois`.
# 'tag': node must carry this ACL tag; 'node': node's MagicDNS name must match (trailing dot ignored).
# Loaded from config.toml [access].allowed_sources; empty means every request gets 403.
ALLOWED_SOURCES = CONFIG['access']['rules']
LOCK = threading.RLock()
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=int(_SERVER['max_workers']))
JOURNAL = pathlib.Path(_SERVER['journal'])
JOBS = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}
INTERRUPTED_JOBS = []
for job in JOBS.values():
    job.setdefault('model', None)
    job.setdefault('effort', None)
    if job['status'] in ('queued', 'running'):
        job.update(status='interrupted', error='Bridge restarted; task was not automatically retried.')
        INTERRUPTED_JOBS.append(job)


def save():
    with LOCK:
        tmp = JOURNAL.with_suffix('.tmp')
        tmp.write_text(json.dumps(JOBS, ensure_ascii=False, indent=2))
        tmp.chmod(0o600)
        tmp.replace(JOURNAL)


class NoWebhookRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Authorization to a redirect destination.


def validate_webhook_url(url):
    def endpoint(value):
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.query or '%' in value or '\\' in value
                or any(c.isspace() or ord(c) < 32 for c in value)
                or any('..' in segment or ';' in segment for segment in parsed.path.split('/'))):
            raise ValueError('Invalid webhook URL')
        if parsed.scheme == 'http':
            address = ipaddress.ip_address(parsed.hostname)  # no DNS names for plaintext
            if not (address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10')):
                raise ValueError('Plaintext webhook outside trusted network')
        elif parsed.scheme != 'https':
            raise ValueError('Invalid webhook scheme')
        return parsed, (parsed.scheme, parsed.hostname.lower(),
                        parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80))
    target, origin = endpoint(url)
    prefix = _SERVER.get('webhook_url_prefix', '')
    if prefix:
        allowed, allowed_origin = endpoint(prefix)
        base = allowed.path.rstrip('/')
        if origin != allowed_origin or (base and target.path != base and not target.path.startswith(base + '/')):
            raise ValueError('Webhook outside configured origin/path')


def read_bearer_token(path=None):
    """Read the owned 0600 credential without following symlinks or a stat/read race."""
    path = path or os.environ.get('HARNESS_BRIDGE_TOKEN_FILE') or _SERVER['token_file']
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError('Token file must be an owned regular file with mode 0600')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('Token file too large')
            token = raw.strip()
    finally:
        os.close(fd)
    if not token or len(token) > 4096 or any(c < 33 or c > 126 for c in token):
        raise ValueError('Invalid token file content')
    return token


INVALID_NEXT_SLOTS = 0
COUNTER_LOCK = threading.Lock()


def token_slots(rule):
    if 'token_files' in rule:
        return rule['token_files']
    if _SERVER.get('require_per_source_tokens', True):
        raise ValueError('per-source token_files required')
    return [None]


def validate_startup_token():
    if _SERVER.get('require_token', True):
        try:
            seen_paths, seen_tokens = set(), []
            rules = list(ALLOWED_SOURCES.values())
            for rule in rules:
                for index, path in enumerate(token_slots(rule)):
                    try:
                        resolved = os.path.abspath(path or os.environ.get('HARNESS_BRIDGE_TOKEN_FILE') or _SERVER['token_file'])
                        if resolved in seen_paths:
                            raise ValueError('reused token path')
                        token = read_bearer_token(path)
                        if any(hmac.compare_digest(token, previous) for previous in seen_tokens):
                            raise ValueError('duplicate token content')
                        seen_paths.add(resolved)
                        seen_tokens.append(token)
                    except FileNotFoundError:
                        if index == 0:
                            raise
        except (OSError, ValueError, KeyError):
            raise RuntimeError(
                'Cannot start: Bearer token file must exist, be owned by this user, '
                'be a regular non-symlink file with mode 0600, and contain a valid token; '
                'check HARNESS_BRIDGE_TOKEN_FILE or server.token_file'
            ) from None


def bearer_authorized(headers, address=None):
    if not _SERVER.get('require_token', True):
        return True
    global INVALID_NEXT_SLOTS
    rule = ALLOWED_SOURCES.get(address, {})
    try:
        slots = token_slots(rule)
    except ValueError:
        return False
    supplied = headers.get_all('Authorization', [])
    try:
        candidate = supplied[0].encode('utf-8') if len(supplied) == 1 else b''
    except (UnicodeError, AttributeError):
        candidate = b''
    matched = False
    invalid = False
    for index, path in enumerate(slots):
        token = b''
        try:
            token = read_bearer_token(path)
        except FileNotFoundError:
            invalid |= index == 0
        except (OSError, ValueError, KeyError):
            invalid |= index == 0
            if index != 0:
                with COUNTER_LOCK:
                    INVALID_NEXT_SLOTS += 1
        # Always compare every slot, including absent/invalid slots.
        equal = hmac.compare_digest(candidate, b'Bearer ' + token)
        matched |= equal and bool(token)
    return matched and not invalid


def post_terminal_webhook(payload):
    """Best effort only. Never log config, URLs, headers, or exception messages."""
    try:
        config = WEBHOOK_CONF
        try:
            content = config.read_text()
        except FileNotFoundError:
            return
        values = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or key.strip() not in ('WEBHOOK_URL', 'WEBHOOK_AUTH'):
                raise ValueError('Invalid webhook config')
            values[key.strip()] = value.strip()
        url = values.get('WEBHOOK_URL', '')
        auth = values.get('WEBHOOK_AUTH', '')
        if not url or not auth:
            return
        validate_webhook_url(url)
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'), method='POST',
            headers={'Authorization': auth, 'Content-Type': 'application/json'})
        if request.type not in ('http', 'https'):
            raise ValueError('Invalid webhook scheme')
        opener = urllib.request.build_opener(NoWebhookRedirect())
        with opener.open(request, timeout=5) as response:
            if not 200 <= response.status < 300:
                raise ValueError('Unexpected webhook status')
    except Exception as error:
        print(f'Terminal webhook failed ({type(error).__name__})', flush=True)


def notify_terminal(job):
    """Snapshot terminal metadata; network/config I/O runs outside job workers."""
    try:
        if job.get('status') in ('queued', 'running'):
            return
        payload = {'job_id': job['job_id'], 'status': job['status'],
                   'backend': job.get('backend', 'mac-codex'), 'host': HOST_LABEL}
        threading.Thread(target=post_terminal_webhook, args=(payload,),
                         name='terminal-webhook', daemon=True).start()
    except Exception as error:
        print(f'Terminal webhook dispatch failed ({type(error).__name__})', flush=True)


def authorized(address):
    try:
        ipaddress.ip_address(address)
        rule = ALLOWED_SOURCES.get(address)
        if not rule:
            return False
        p = subprocess.run([TAILSCALE, 'whois', '--json', address], capture_output=True,
                           text=True, timeout=5, check=True)
        node = json.loads(p.stdout)['Node']
        if 'tag' in rule:
            return rule['tag'] in (node.get('Tags') or [])
        if 'node' in rule:
            return (node.get('Name') or '').rstrip('.') == rule['node'].rstrip('.')
        return False
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
        return False


def run_agent(cmd, prompt):
    """Keep the CLI and shell descendants in a killable session on timeout."""
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, cwd=WORKSPACE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Do not wait forever on a pipe inherited by an escaped descendant.
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def effective_selection(backend, parsed, model, effort):
    """Best-effort report of the model/effort the agent actually ran with."""
    if backend == 'mac-claudecode':
        return {'model': ','.join(parsed.get('models') or []) or model, 'effort': None}
    seen = codex_effective(parsed.get('session_id'))
    if seen:
        return seen
    default = codex_config_default()
    return {'model': model or default['model'], 'effort': effort or default['effort']}


def run_job(job_id, prompt, session_id, backend='mac-codex', model=None, effort=None):
    with LOCK:
        job = JOBS[job_id]
        job['status'] = 'running'
        save()
    try:
        cmd = command_for(backend, session_id, model, effort)
        result = run_agent(cmd, prompt)
        parsed = parse_events(BACKENDS[backend]['engine'], result.stdout)
        actual_session = parsed['session_id']
        if session_id and actual_session and actual_session != session_id:
            raise RuntimeError('Agent returned a different session; refusing to treat it as a resume.')
        with LOCK:
            success = result.returncode == 0 and not parsed['failed']
            status = 'completed' if success else 'failed'
            if parsed['permission_denials']:
                status = 'needs_attention'
            job.update(status=status,
                       session_id=actual_session or session_id,
                       result=parsed['result'], exit_code=result.returncode)
            try:
                actual = effective_selection(backend, dict(parsed, session_id=actual_session or session_id),
                                             model, effort)
                job.update(effective_model=actual.get('model'), effective_effort=actual.get('effort'))
            except Exception:  # Reporting only; never fail a finished job because of it.
                pass
            if parsed.get('warnings'):
                job['warnings'] = parsed['warnings']
            if parsed['permission_denials']:
                job['permission_denials'] = parsed['permission_denials']
            if not success:
                if (backend == 'mac-claudecode' and result.returncode == 78 and
                        CLAUDE_ENV_MISSING in getattr(result, 'stderr', '').splitlines()):
                    job.update(result=CLAUDE_ENV_MISSING, error=CLAUDE_ENV_MISSING)
                else:
                    job['error'] = 'Agent or transport failed; inspect the result and host diagnostics before retrying.'
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as error:
        with LOCK:
            job.update(status='failed', error=str(error))
    finally:
        with LOCK:
            save()
            notification = dict(job)
        notify_terminal(notification)


def resolve_session_selection(session, backend, model, effort):
    """Sticky per-session model: a resume without model/effort reuses the session's last selection.

    Codex resume otherwise falls back to config.toml's default, silently switching models.
    An explicit model/effort on resume is allowed; the change is recorded in model_switched_from.
    Caller holds LOCK.
    """
    previous = None
    if session:
        history = [x for x in JOBS.values() if x.get('session_id') == session and x.get('backend', 'mac-codex') == backend]
        if history:
            previous = max(history, key=lambda x: x.get('created_at', ''))
    if model is None and effort is None:
        if previous and (previous.get('model') or previous.get('effort')):
            return {'model': previous.get('model'), 'effort': previous.get('effort'), 'model_source': 'session'}
        return {'model': None, 'effort': None, 'model_source': 'default'}
    out = {'model': model, 'effort': effort, 'model_source': 'request'}
    if previous is not None:
        before = (previous.get('model'), previous.get('effort'))
        if model is None:  # effort-only override keeps the session's model
            out['model'] = before[0]
        if (out['model'], out['effort']) != before:
            out['model_switched_from'] = {'model': before[0], 'effort': before[1]}
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *_):
        pass  # Do not log prompts, credentials, or request headers.

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not authorized(self.client_address[0]) or not bearer_authorized(self.headers, self.client_address[0]):
            return self.reply(403, {'error': 'Requires an allowed Tailscale source and valid API credential'})
        if self.path == '/health':
            return self.reply(200, {'ok': True, 'backend': 'Mac Codex / Claude Code CLI',
                                    'backends': list(BACKENDS),
                                    'mode': {'mac-codex': '{}; approval_policy={}; local Codex config'.format(
                                                 CONFIG['codex']['sandbox'], CONFIG['codex']['approval_policy']),
                                             'mac-claudecode': CONFIG['claude']['permission_mode'] + '; local Claude config'},
                                    'host': HOST_LABEL,
                                    'workspace': str(WORKSPACE)})
        if self.path == '/backends':
            info = {k: dict({field: b[field] for field in ['host', 'account', 'engine', 'workspace']},
                            supports_model=True, supports_effort=(b['engine'] == 'codex'))
                    for k, b in BACKENDS.items()}
            info['mac-codex'].update(models={k: list(v) for k, v in sorted(codex_models().items())},
                                     default=codex_config_default(),
                                     selector='model "slug" or "slug:effort"; optional effort in ' + '|'.join(EFFORTS))
            info['mac-claudecode'].update(selector=('model selected via switch helper' if CONFIG['claude']['model_mode'] == 'switch'
                                                    else 'model passed to claude --model'))
            return self.reply(200, info)
        if self.path.startswith('/jobs/'):
            with LOCK:
                job = JOBS.get(self.path.removeprefix('/jobs/'))
                return self.reply(200 if job else 404,
                                  dict(job, model=job.get('model'), effort=job.get('effort')) if job
                                  else {'error': 'Unknown job'})
        return self.reply(404, {'error': 'Unknown route'})

    def do_POST(self):
        if not authorized(self.client_address[0]) or not bearer_authorized(self.headers, self.client_address[0]):
            return self.reply(403, {'error': 'Requires an allowed Tailscale source and valid API credential'})
        if self.path != '/jobs':
            return self.reply(404, {'error': 'Unknown route'})
        if ('Origin' in self.headers or 'Sec-Fetch-Site' in self.headers
                or self.headers.get_all('Host', []) != [f'{HOST}:{PORT}']):
            return self.reply(403, {'error': 'Invalid request origin or host'})
        if (len(self.headers.get_all('Content-Type', [])) != 1
                or self.headers.get_content_type() != 'application/json'):
            return self.reply(415, {'error': 'Requires application/json'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                raise ValueError(f'Body must be 1..{MAX_BODY} bytes')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or set(data) - {'prompt', 'session_id', 'backend', 'model', 'effort'}:
                raise ValueError('Only prompt, optional session_id, backend, model and effort are accepted')
            backend = data.get('backend', 'mac-codex')
            if not isinstance(backend, str) or backend not in ('mac-codex', 'mac-claudecode'):
                return self.reply(400, {'error': 'Unsupported backend; only mac-codex and mac-claudecode are allowed'})
            try:
                model, effort = normalize_selection(backend, data.get('model'), data.get('effort'))
            except ModelError as error:
                return self.reply(400, {'error': str(error)})
            prompt = data['prompt']
            session = data.get('session_id')
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError('prompt must be nonempty text')
            if session is not None and (not isinstance(session, str) or not session.strip()):
                raise ValueError('session_id must be nonempty text')
            with LOCK:
                if session and not any(x.get('session_id') == session and
                                       x.get('workspace') == BACKENDS[backend]['workspace'] and
                                       x.get('backend', 'mac-codex') == backend
                                       for x in JOBS.values()):
                    raise ValueError('Unknown session_id')
                if session and any(x.get('session_id') == session and
                                   x['status'] in ('queued', 'running') for x in JOBS.values()):
                    return self.reply(409, {'error': 'This session_id already has a queued or running job; concurrent resumes are not allowed'})
                job_id = str(uuid.uuid4())
                record = {'job_id': job_id, 'status': 'queued', 'session_id': session,
                          'workspace': BACKENDS[backend]['workspace'], 'backend': backend,
                          'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
                record.update(resolve_session_selection(session, backend, model, effort))
                JOBS[job_id] = record
                save()
                POOL.submit(run_job, job_id, prompt, session, backend, record['model'], record['effort'])
            return self.reply(202, {'job_id': job_id, 'status': 'queued'})
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {'error': 'Invalid JSON request, prompt, or session_id'})


def codex_mode_summary():
    return 'codex sandbox={}; approval_policy={}'.format(
        CONFIG['codex']['sandbox'], CONFIG['codex']['approval_policy'])


if __name__ == '__main__':
    os.umask(0o077)
    validate_startup_token()
    if not WORKSPACE.is_dir():
        raise RuntimeError(f'Configured workspace is missing: {WORKSPACE}')
    if INTERRUPTED_JOBS:
        save()
        for job in INTERRUPTED_JOBS:
            notify_terminal(job)
    print(f'harness-bridge listening on {HOST}:{PORT} ({len(ALLOWED_SOURCES)} allowed sources); workspace={WORKSPACE}; {codex_mode_summary()}', flush=True)
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()