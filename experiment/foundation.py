#!/usr/bin/env python3
"""Local foundation rehearsal, not a measured trial or CI/CD controller."""
import base64
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO.parent

def main():
    trial = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    evidence = STUDY / 'results' / 'foundation' / trial
    evidence.mkdir(parents=True, exist_ok=False)
    runtime = Path.home() / 'gotify-study-runtime' / 'foundation' / trial
    runtime.mkdir(parents=True, mode=0o700)
    password = secrets.token_urlsafe(24)
    password_file = runtime / 'admin-password'
    password_file.write_text(password)
    password_file.chmod(0o600)
    manifest = json.loads((REPO / 'experiment/build/images.json').read_text())
    env = os.environ.copy()
    env.update(GOTIFY_ADMIN_PASSWORD_FILE=str(password_file), GOTIFY_RELEASE_SHA=manifest['source_sha'], GOTIFY_TRIAL_ID=trial)
    basic = 'Basic ' + base64.b64encode(('experiment:' + password).encode()).decode()
    sequence = []

    def event(kind, **fields):
        with (evidence / 'events.jsonl').open('a') as f:
            f.write(json.dumps({'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'kind': kind, **fields}) + '\n')

    def run(args, **kwargs):
        event('command', argv=args)
        p = subprocess.run(args, text=True, capture_output=True, timeout=180, **kwargs)
        with (evidence / 'commands.log').open('a') as f:
            f.write('$ ' + ' '.join(args) + '\n' + p.stdout + p.stderr + '\n')
        if p.returncode:
            raise RuntimeError('Command failed: ' + args[0])
        return p.stdout.strip()

    def request(port, path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token: headers['X-Gotify-Key'] = token
        else: headers['Authorization'] = basic
        req = urllib.request.Request('http://127.0.0.1:' + str(port) + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=8) as response:
                result = json.load(response)
                event('http', port=port, path=path, status=response.status,
                      duration_ms=round((time.monotonic() - started) * 1000))
                return result
        except Exception as exc:
            event('http', port=port, path=path, error_type=type(exc).__name__,
                  duration_ms=round((time.monotonic() - started) * 1000))
            raise

    def compose(stage, release, action):
        port = 8101 if stage == 'staging' else 8100
        data = runtime / stage
        data.mkdir(exist_ok=True)
        current = env | {'GOTIFY_HOST_PORT': str(port), 'GOTIFY_DATA_DIR': str(data), 'GOTIFY_IMAGE': manifest['images'][release]}
        args = ['docker', 'compose', '-f', str(REPO / 'experiment/compose.yaml'), '-p', 'gotify-conventional-' + stage]
        run(args + action, env=current)
        cid = run(args + ['ps', '-q', 'gotify'], env=current)
        actual = run(['docker', 'inspect', '--format', '{{.Image}}', cid])
        if actual != manifest['images'][release]: raise RuntimeError('Image identity mismatch')
        event('deployed', stage=stage, release=release, image=actual, container=cid, data_dir=str(data))

    sentinels = {}
    tokens = {}
    def probe(stage, release, label):
        port = 8101 if stage == 'staging' else 8100
        health = request(port, '/health')
        version = request(port, '/version')
        if health != {'health': 'green', 'database': 'green'}: raise RuntimeError('Unhealthy')
        if version['commit'] != manifest['source_sha'] or version['version'] != 'experiment-' + release:
            raise RuntimeError('Version mismatch')
        if stage not in tokens:
            tokens[stage] = request(port, '/application', {'name': 'foundation-probe'})['token']
        payload = trial + '-' + stage + '-' + label
        msg = request(port, '/message', {'title': 'Experiment foundation', 'message': payload}, tokens[stage])
        messages = request(port, '/message?limit=100')['messages']
        if not any(m['id'] == msg['id'] and m['message'] == payload for m in messages):
            raise RuntimeError('Readback mismatch')
        if stage in sentinels and not any(m['id'] == sentinels[stage]['id'] and m['message'] == sentinels[stage]['message'] for m in messages):
            raise RuntimeError('Lost baseline sentinel')
        sentinels.setdefault(stage, {'id': msg['id'], 'message': payload})
        run(['node', str(REPO / 'experiment/dashboard.mjs'), 'http://127.0.0.1:' + str(port), payload,
             str(evidence / (stage + '-' + label + '.png'))],
             env=env | {'GOTIFY_PROBE_USER': 'experiment', 'GOTIFY_PROBE_PASSWORD': password})
        record = {'stage': stage, 'release': release, 'label': label, 'message_id': msg['id'],
                  'sentinel_id': sentinels[stage]['id'], 'api_ok': True, 'dashboard_ok': True, 'persistence_ok': True}
        sequence.append(record)
        event('verification', **record)

    result = {'trial_id': trial, 'mode': 'foundation-rehearsal', 'manifest': manifest, 'runtime': str(runtime)}
    try:
        for port in (8100, 8101, 8200, 8201):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
        event('ports_free', ports=[8100, 8101, 8200, 8201])
        for stage in ('staging', 'production'):
            compose(stage, 'v1', ['up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '120'])
            probe(stage, 'v1', 'baseline')
        for stage in ('staging', 'production'):
            compose(stage, 'v2', ['up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '120'])
            probe(stage, 'v2', 'candidate')
        compose('production', 'v2', ['restart', 'gotify'])
        compose('production', 'v2', ['up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '120'])
        probe('production', 'v2', 'restarted')
        for stage in ('staging', 'production'):
            compose(stage, 'v1', ['up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '120'])
            probe(stage, 'v1', 'restored')
        result['status'] = 'PASS'
    except Exception as exc:
        result['status'] = 'FAIL'
        result['error_type'] = type(exc).__name__
        event('failure', error_type=type(exc).__name__)
    result['checks'] = sequence
    (evidence / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({'status': result['status'], 'evidence': str(evidence)}))
    return 0 if result['status'] == 'PASS' else 1

if __name__ == '__main__':
    sys.exit(main())
