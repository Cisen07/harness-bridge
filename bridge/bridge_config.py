"""Configuration loader shared by codex_bridge.py and agent_adapters.py.

Resolution order for the config file:
  1. $HARNESS_BRIDGE_CONFIG (absolute path)
  2. <bridge dir>/config.toml
  3. <bridge dir>/config.json
Nothing host-specific is hard-coded in the bridge; every value below has a neutral default
or must be provided by the operator. See config.example.toml.
"""
import ipaddress
import json
import os
import pathlib

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        _toml = None

BRIDGE_DIR = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path(os.path.expanduser('~'))

DEFAULTS = {
    'server': {
        'bind_host': '127.0.0.1',      # set to this machine's tailnet IP
        'allow_loopback_for_tests': False,
        'port': 18542,
        'host_label': 'mac',           # sent as "host" in the terminal webhook payload
        'max_workers': 4,
        'require_token': True,
        'require_per_source_tokens': True,
        'token_file': '',
        'webhook_url_prefix': '',
        'job_timeout_seconds': 3600,
        'max_body_bytes': 16000,
        'tailscale_bin': '/usr/local/bin/tailscale',
        'journal': '',                 # default: <bridge dir>/jobs.json
        'webhook_conf': '',            # default: <bridge dir>/webhook.conf
    },
    'workspace': {
        'path': str(HOME / 'src'),
    },
    'codex': {
        'bin': str(HOME / '.local/bin/codex'),
        'home': '',                    # default: $CODEX_HOME or ~/.codex
        'sandbox': 'danger-full-access',
        'approval_policy': 'never',
    },
    'claude': {
        'bin': 'claude',
        'shell': '/bin/zsh',
        'path_prepend': [],            # extra PATH entries, e.g. an nvm node bin dir
        'env_script': '',              # optional script sourced before claude (gateway/env setup)
        'permission_mode': 'dontAsk',
        # 'flag'   -> model passed as `claude --model <m>`
        # 'switch' -> model selected per process by `<switch_helper> export <m>` (prints shell exports)
        'model_mode': 'flag',
        'switch_helper': '',
    },
    'access': {
        # Each entry: {ip = "<tailnet ip>", tag = "tag:<name>"} or {ip = "...", node = "<magicdns name>"}
        'allowed_sources': [],
    },
}


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def config_path():
    env = os.environ.get('HARNESS_BRIDGE_CONFIG')
    if env:
        return pathlib.Path(env)
    for name in ('config.toml', 'config.json'):
        candidate = BRIDGE_DIR / name
        if candidate.exists():
            return candidate
    return None


def load():
    path = config_path()
    data = {}
    if path is not None:
        raw = path.read_bytes()
        if path.suffix == '.toml':
            if _toml is None:
                raise RuntimeError('TOML config needs Python 3.11+ (or tomli); use config.json instead')
            data = _toml.loads(raw.decode('utf-8'))
        else:
            data = json.loads(raw.decode('utf-8'))
    cfg = _merge(DEFAULTS, data)
    server = cfg['server']
    server['journal'] = server['journal'] or str(BRIDGE_DIR / 'jobs.json')
    server['webhook_conf'] = server['webhook_conf'] or str(BRIDGE_DIR / 'webhook.conf')
    server['token_file'] = server['token_file'] or str(BRIDGE_DIR / 'bridge.token')
    if not isinstance(server['require_per_source_tokens'], bool):
        raise ValueError('server.require_per_source_tokens must be boolean')
    if not isinstance(server['require_token'], bool):
        raise ValueError('server.require_token must be boolean')
    host = ipaddress.ip_address(server['bind_host'])
    allow_loopback = server.get('allow_loopback_for_tests', False)
    if not isinstance(allow_loopback, bool):
        raise ValueError('server.allow_loopback_for_tests must be boolean')
    if not (host in ipaddress.ip_network('100.64.0.0/10') or
            (str(host) == '127.0.0.1' and allow_loopback)):
        raise ValueError('bind_host must be a tailnet IPv4 address (loopback requires test opt-in)')
    codex = cfg['codex']
    codex['home'] = codex['home'] or os.environ.get('CODEX_HOME', str(HOME / '.codex'))
    rules = {}
    used_token_paths = set()
    for entry in cfg['access']['allowed_sources']:
        if not isinstance(entry, dict) or 'ip' not in entry:
            raise ValueError('access.allowed_sources entries need an "ip"')
        rule = {k: entry[k] for k in ('tag', 'node') if entry.get(k)}
        if len(rule) != 1:
            raise ValueError('each allowed source needs exactly one of "tag" or "node"')
        if 'token_files' in entry:
            slots = entry['token_files']
            if (not isinstance(slots, list) or not 1 <= len(slots) <= 2
                    or any(not isinstance(p, str) or not pathlib.Path(p).is_absolute() for p in slots)):
                raise ValueError('token_files needs one or two absolute paths')
            normalized = [os.path.normpath(p) for p in slots]
            if len(set(normalized)) != len(normalized) or any(p in used_token_paths for p in normalized):
                raise ValueError('token_files paths must be distinct across sources and slots')
            used_token_paths.update(normalized)
            rule['token_files'] = slots
        elif server['require_per_source_tokens']:
            raise ValueError('each allowed source requires token_files')
        if str(entry['ip']) in rules:
            raise ValueError('duplicate source IP')
        rules[str(entry['ip'])] = rule
    cfg['access']['rules'] = rules
    return cfg


CONFIG = load()
