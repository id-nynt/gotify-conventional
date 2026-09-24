#!/usr/bin/env python3
"""Controller-neutral single operations. Linux/WSL only; private state is never evidence."""
import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from boundary import rendezvous

HERE = Path(__file__).resolve().parent
PORTS = {'conventional': {'staging': 8101, 'production': 8100},
         'bdi': {'staging': 8201, 'production': 8200}}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)

class Operations:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.runtime).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.trial = self.root / 'trials' / args.approach / args.trial
        self.public = self.trial / 'evidence'
        self.state_file = self.trial / 'state.json'
        self.active = self.root / 'active-trial.json'
        self.unresolved = self.root / 'unresolved-operation.json'
        self.owner = {'approach': args.approach, 'trial': args.trial}
        self.state = None
        self.receipt = {'operation_id': uuid.uuid4().hex, 'started_at': now(),
                        **self.owner, 'operation': args.operation, 'environment': args.environment,
                        'release': args.release, 'requests': [], 'commands': []}

    def command(self, argv, env=None):
        started = time.monotonic()
        try:
            p = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            write_json(self.unresolved, {**self.owner, 'operation_id': self.receipt['operation_id'], 'argv': argv, 'at': now()})
            raise RuntimeError('execution_unresolved_host_reservation_retained')
        record = {'argv': argv, 'exit_code': p.returncode,
                  'duration_ms': round((time.monotonic() - started) * 1000)}
        self.receipt['commands'].append(record)
        # Commands here never print credentials, raw API bodies or complete inspect output.
        with (self.public / 'commands.log').open('a') as f:
            f.write(json.dumps(record) + '\n' + p.stdout + p.stderr + '\n')
        if p.returncode:
            raise RuntimeError('operation_command_failed')
        return p.stdout.strip()

    def save(self):
        write_json(self.state_file, self.state)

    def prepare(self):
        if self.active.exists():
            raise RuntimeError('host_reserved_by_an_existing_trial')
        manifest = json.loads(Path(self.args.manifest).read_text())
        if not re.fullmatch('[0-9a-f]{40}', manifest['source_sha']):
            raise ValueError('invalid_source_sha')
        for release in ('v1', 'v2'):
            if not re.fullmatch(r'(sha256:[0-9a-f]{64}|ghcr\.io/id-nynt/gotify-(conventional|bdi)-experiment@sha256:[0-9a-f]{64})', manifest['images'][release]):
                raise ValueError('immutable_experiment_image_required')
        self.trial.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.public.mkdir()
        for image in manifest['images'].values():
            if '@sha256:' in image:
                self.command(['docker', 'pull', image])
        self.state = {'manifest': manifest, 'environments': {}, 'compose_sha256':
                      hashlib.sha256((HERE / 'compose.yaml').read_bytes()).hexdigest()}
        for environment in ('staging', 'production'):
            directory = self.trial / environment
            directory.mkdir(mode=0o700)
            (directory / 'data').mkdir()
            password = secrets.token_urlsafe(24)
            credential = directory / 'admin-password'
            credential.write_text(password)
            credential.chmod(0o600)
            self.state['environments'][environment] = {'password': password, 'baseline_verified': False}
        self.save()
        write_json(self.active, self.owner)
        self.receipt['manifest'] = manifest

    def context(self):
        stage = self.args.environment
        if stage not in PORTS[self.args.approach]:
            raise ValueError('environment_required')
        return self.state['environments'][stage]

    def compose(self, action, release):
        self.context()
        stage = self.args.environment
        directory = self.trial / stage
        env = os.environ | {'GOTIFY_IMAGE': self.state['manifest']['images'][release],
            'GOTIFY_HOST_PORT': str(PORTS[self.args.approach][stage]),
            'GOTIFY_DATA_DIR': str(directory / 'data'),
            'GOTIFY_ADMIN_PASSWORD_FILE': str(directory / 'admin-password'),
            'GOTIFY_RELEASE_SHA': self.state['manifest']['source_sha'], 'GOTIFY_TRIAL_ID': self.args.trial}
        env['GOTIFY_EXECUTION_ID'] = self.context().get('execution_id', 'unassigned')
        project = 'gotify-' + self.args.approach + '-' + stage
        return self.command(['docker', 'compose', '-f', str(HERE / 'compose.yaml'), '-p', project] + action, env)

    def deploy(self, baseline=False):
        state = self.context()
        release = self.args.release
        if baseline:
            if state.get('release') or release != 'v1':
                raise RuntimeError('reset_requires_new_trial_and_v1')
        elif not state['baseline_verified']:
            raise RuntimeError('verified_baseline_required')
        if hashlib.sha256((HERE / 'compose.yaml').read_bytes()).hexdigest() != self.state['compose_sha256']:
            raise RuntimeError('deployment_configuration_changed')
        state['execution_id'] = self.args.execution_id or self.receipt['operation_id']
        self.save()
        self.compose(['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120'], release)
        state['release'] = release
        self.save()
        self.identity()
        if not baseline and self.args.operation == 'deploy' and self.args.environment == 'production' and release == 'v2':
            rendezvous(self.root, self.owner, self.receipt)
            self.receipt['deployment_ready_at_unix'] = time.time()
            write_json(self.public / 'production-boundary.json', {**self.owner,
                'execution_id': state['execution_id'], 'release': release,
                'ready_at_unix': self.receipt['deployment_ready_at_unix']})

    def identity(self):
        stage = self.args.environment
        cid = self.compose(['ps', '-a', '-q', 'gotify'], self.args.release)
        if not cid:
            raise RuntimeError('container_missing')
        image = self.command(['docker', 'inspect', '--format', '{{.Image}}', cid])
        running = self.command(['docker', 'inspect', '--format', '{{.State.Running}}', cid]) == 'true'
        trial = self.command(['docker', 'inspect', '--format', '{{index .Config.Labels "experiment.trial_id"}}', cid])
        restart = self.command(['docker', 'inspect', '--format', '{{.HostConfig.RestartPolicy.Name}}', cid])
        execution = self.command(['docker', 'inspect', '--format', '{{index .Config.Labels "experiment.execution_id"}}', cid])
        reference = self.state['manifest']['images'][self.args.release]
        expected = self.command(['docker', 'image', 'inspect', '--format', '{{.Id}}', reference])
        self.receipt['identity'] = {'container': cid, 'actual_image': image, 'expected_image': expected,
                                    'running': running, 'trial': trial, 'restart_policy': restart, 'execution_id': execution}
        expected_execution = self.args.execution_id or self.context().get('execution_id')
        if image != expected or trial != self.args.trial or restart != 'no' or execution != expected_execution:
            raise RuntimeError('runtime_identity_mismatch')
        return running

    def request(self, path, body=None, token=None):
        state = self.context()
        port = PORTS[self.args.approach][self.args.environment]
        auth = 'Basic ' + base64.b64encode(('experiment:' + state['password']).encode()).decode()
        headers = {'Content-Type': 'application/json', **({'X-Gotify-Key': token} if token else {'Authorization': auth})}
        req = urllib.request.Request('http://127.0.0.1:' + str(port) + path, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        started = time.monotonic()
        record = {'at': now(), 'path': path, 'method': req.get_method()}
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=8) as response:
                record['status'] = response.status
                return json.load(response)
        except Exception as error:
            record['error_type'] = type(error).__name__
            raise
        finally:
            record['duration_ms'] = round((time.monotonic() - started) * 1000)
            self.receipt['requests'].append(record)

    def probe(self, dashboard):
        state = self.context()
        self.receipt['observation'] = {'at': now(), 'health': 'unknown', 'functional': False,
                                      'data_status': 'unavailable', 'persistence': False}
        observation = self.receipt['observation']
        if not self.identity():
            observation.update(health='unhealthy', data_status='fresh', reason='container_stopped')
            raise RuntimeError('container_stopped')
        try:
            health = self.request('/health')
            version = self.request('/version')
            if health != {'health': 'green', 'database': 'green'}:
                raise RuntimeError('health_check_failed')
            if version.get('commit') != self.state['manifest']['source_sha'] or version.get('version') != 'experiment-' + self.args.release:
                raise RuntimeError('release_identity_mismatch')
            if 'app_token' not in state:
                state['app_token'] = self.request('/application', {'name': 'study-probe'})['token']
                self.save()
            payload = self.args.trial + '-' + self.args.environment + '-' + self.receipt['operation_id']
            sent = self.request('/message', {'title': 'Gotify experiment probe', 'message': payload}, state['app_token'])
            messages = self.request('/message?limit=100')['messages']
            if not any(m['id'] == sent['id'] and m['message'] == payload for m in messages):
                raise RuntimeError('message_content_mismatch')
            if 'sentinel' in state:
                # Fetch older pages when needed; do not mistake high traffic for lost data.
                sentinel = state['sentinel']
                cursor = None
                while not any(m['id'] == sentinel['id'] and m['message'] == sentinel['message'] for m in messages):
                    if not messages or len(messages) < 100:
                        raise RuntimeError('persistence_sentinel_missing')
                    next_cursor = min(m['id'] for m in messages)
                    if cursor == next_cursor:
                        raise RuntimeError('message_pagination_stalled')
                    cursor = next_cursor
                    messages = self.request('/message?limit=100&since=' + str(cursor))['messages']
            else:
                state['sentinel'] = {'id': sent['id'], 'message': payload}
                self.save()
            if dashboard:
                screenshot = self.public / (self.receipt['operation_id'] + '.png')
                self.command(['node', str(HERE / 'dashboard.mjs'),
                    'http://127.0.0.1:' + str(PORTS[self.args.approach][self.args.environment]), payload, str(screenshot)],
                    os.environ | {'GOTIFY_PROBE_USER': 'experiment', 'GOTIFY_PROBE_PASSWORD': state['password']})
                observation['dashboard'] = True
            if not self.identity():
                raise RuntimeError('container_stopped_during_probe')
            observation.update(health='healthy', data_status='fresh', functional=True, persistence=True,
                               message_id=sent['id'], message=payload, sentinel_id=state['sentinel']['id'])
            if self.args.release == 'v1' and dashboard:
                state['baseline_verified'] = True
                self.save()
        except Exception as error:
            known_failure = isinstance(error, (RuntimeError, OSError))
            observation.update(health='unhealthy' if known_failure else 'unknown',
                               data_status='fresh' if known_failure else 'unavailable', reason=type(error).__name__)
            raise

    def diagnose(self):
        running = self.identity()
        database = self.trial / self.args.environment / 'data/gotify.db'
        database_ready = False
        if database.is_file():
            try:
                with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=4) as connection:
                    database_ready = connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            except sqlite3.Error:
                pass
        identity = self.receipt['identity']
        return {'deployment_execution_id': identity['execution_id'], 'container_id': identity['container'],
                'dependency_ready': database_ready, 'dependency_kind': 'sqlite',
                'app_state': 'running' if running else 'stopped'}

    def execute(self):
        operation = self.args.operation
        if self.unresolved.exists() and operation != 'evidence':
            raise RuntimeError('unresolved_execution_requires_reconciliation')
        if operation == 'prepare':
            self.prepare()
            return
        if not self.active.exists() or json.loads(self.active.read_text()) != self.owner:
            raise RuntimeError('trial_does_not_own_host')
        self.state = json.loads(self.state_file.read_text())
        if operation == 'reset':
            self.deploy(baseline=True)
        elif operation == 'deploy':
            self.deploy()
        elif operation == 'rollback':
            if self.args.release != 'v1':
                raise ValueError('rollback_requires_v1')
            self.deploy()
        elif operation in ('probe', 'observe'):
            self.probe(dashboard=operation == 'probe')
        elif operation in ('diagnose', 'restart'):
            before = self.diagnose()
            repair = {'action': operation, 'expected_execution_id': before['deployment_execution_id'],
                      'before': before, 'status': 'observed'}
            self.receipt['repair_evidence'] = repair
            if operation == 'restart':
                repair['status'] = 'failed'
                self.compose(['restart', 'gotify'], self.args.release)
                repair.update(status='executed', after=self.diagnose())
        elif operation == 'evidence':
            self.receipt['files'] = sorted(p.name for p in self.public.iterdir())
        elif operation == 'finish':
            if not self.args.outcome:
                raise ValueError('explicit_outcome_required')
            self.receipt['outcome'] = self.args.outcome
            self.active.unlink()

    def run(self):
        lock = (self.root / 'operation.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = 0
        try:
            if self.public.exists():
                with (self.public / 'intents.jsonl').open('a') as f:
                    f.write(json.dumps(self.receipt) + '\n')
            self.execute()
            self.receipt['status'] = 'PASS'
        except Exception as error:
            code = 1
            self.receipt.update(status='FAIL', error_type=type(error).__name__, error=str(error))
        self.receipt['finished_at'] = now()
        if 'observation' in self.receipt:
            requests = self.receipt['requests']
            durations = sorted(r['duration_ms'] for r in requests)
            failures = sum('error_type' in r or r.get('status', 0) >= 400 for r in requests)
            self.receipt['observation'].update(
                request_count=len(requests), request_failures=failures,
                error_rate=failures / len(requests) if requests else 0,
                latency_p95_ms=durations[math.ceil(len(durations) * .95)-1] if durations else 0,
                availability=1 if self.receipt['observation']['health'] == 'healthy' else 0)
        if self.public.exists():
            # Unique operation IDs preserve failures and repeated observations.
            with (self.public / (self.receipt['operation_id'] + '.json')).open('x') as f:
                json.dump(self.receipt, f, indent=2)
        print(json.dumps(self.receipt))
        return code

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['prepare', 'reset', 'deploy', 'probe', 'observe', 'diagnose', 'restart', 'rollback', 'evidence', 'finish'])
    p.add_argument('--approach', required=True, choices=PORTS)
    p.add_argument('--trial', required=True)
    p.add_argument('--environment', choices=['staging', 'production'])
    p.add_argument('--release', choices=['v1', 'v2'], default='v2')
    p.add_argument('--runtime', default=str(Path.home() / 'gotify-study-runtime'))
    p.add_argument('--manifest')
    p.add_argument('--execution-id', help='Expected deployment identity, or new identity for deploy/reset/rollback')
    p.add_argument('--outcome', choices=['achieved', 'restored', 'failed', 'incomplete'])
    args = p.parse_args()
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', args.trial):
        p.error('trial must be a safe identifier')
    if args.operation == 'prepare' and not args.manifest:
        p.error('prepare requires --manifest')
    return Operations(args).run()

if __name__ == '__main__':
    sys.exit(main())
