#!/usr/bin/env python3
"""Print a short summary of one job JSON and optionally append a ledger line.
usage: _job_summary.py <job.json> <kind:direct|mac> <host-label> <bot> [ledger_path|-]"""
import json, sys, time, datetime, pathlib

TERMINAL = {'completed', 'failed', 'interrupted', 'needs_attention'}


def find_tokens(obj, depth=0):
    """Look for token usage anywhere in the job (usage / tokens / *_tokens keys)."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    out = {}
    for k, v in obj.items():
        lk = str(k).lower()
        if lk in ('usage', 'token_usage', 'tokens', 'modelusage') and isinstance(v, (dict, int)):
            return v
        if lk.endswith('_tokens') and isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, dict):
            r = find_tokens(v, depth + 1)
            if r:
                return r
    return out or None


def main():
    path, kind, host, bot = sys.argv[1:5]
    ledger = sys.argv[5] if len(sys.argv) > 5 else '-'
    j = json.loads(pathlib.Path(path).read_text())
    st = j.get('status')
    model = j.get('effective_model') or (j.get('observed_models') or [None])[0] or j.get('model')
    effort = j.get('effective_effort') or j.get('observed_effort') or j.get('effort')
    tok = find_tokens(j)
    print('status:', st)
    print('job_id:', j.get('job_id'))
    print('backend:', str(j.get('backend')) + (' (' + host + ')' if host else ''))
    print('model/effort:', (model or 'default') + ' / ' + (effort or 'default'))
    print('exit_code:', j.get('exit_code'))
    if j.get('permission_denials'):
        ds = j['permission_denials']
        print('permission_denials: %d' % len(ds))
        for d in ds[:5]:
            if isinstance(d, dict):
                cmd = json.dumps(d.get('tool_input', ''), ensure_ascii=False)
                print('  -', d.get('tool_name'), cmd[:120] + ('…' if len(cmd) > 120 else ''))
            else:
                print('  -', str(d)[:120])
    if st == 'failed' and j.get('exit_code') == 0 and not j.get('error'):
        print('note: exit_code=0 but status failed: usually an error/turn.failed event in the CLI output; the result may still be complete')
    if j.get('error'):
        print('error:', str(j['error'])[:300])
    if j.get('warnings'):
        print('warnings:', len(j['warnings']), '(see full JSON)')
    if tok:
        print('tokens:', json.dumps(tok, ensure_ascii=False)[:300])
    res = str(j.get('result') or '')
    print('result (first 600 of %d chars):' % len(res))
    print(res[:600] + ('…' if len(res) > 600 else ''))
    print('full:', path)
    if ledger != '-' and st in TERMINAL:
        rec = dict(time=datetime.datetime.now().astimezone().isoformat(timespec='seconds'), bot=bot,
                   kind=kind, host=host, job_id=j.get('job_id'), backend=j.get('backend'), model=model,
                   effort=effort, status=st, exit_code=j.get('exit_code'), result_chars=len(res), tokens=tok)
        p = pathlib.Path(ledger)
        seen = p.exists() and any(('"job_id": "%s"' % j.get('job_id')) in l for l in p.read_text().splitlines())
        if not seen:  # one line per job, even if wait is re-run
            with p.open('a') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
