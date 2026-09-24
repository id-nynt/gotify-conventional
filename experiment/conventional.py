#!/usr/bin/env python3
"""Explicit conventional release and recovery rules. No BDI/Jason dependency."""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent

class Conventional:
    def __init__(self, args):
        self.args = args
        self.policy = json.loads((HERE / 'policy.json').read_text())
        self.evidence = Path(args.runtime).expanduser() / 'trials/conventional' / args.trial / 'evidence'
        self.prepared = False
        self.last_receipt = {}

    def decision(self, action, **details):
        record = {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'controller': 'conventional',
                  'decision': action, **details}
        print(json.dumps(record), flush=True)
        if self.evidence.exists():
            with (self.evidence / 'decisions.jsonl').open('a') as f:
                f.write(json.dumps(record) + '\n')

    def op(self, action, environment=None, release='v2', extra=()):
        self.decision(action, environment=environment, release=release)
        argv = [sys.executable, str(HERE / 'operations.py'), action,
                '--approach', 'conventional', '--trial', self.args.trial,
                '--runtime', self.args.runtime, '--release', release, *extra]
        if environment: argv.extend(['--environment', environment])
        result = subprocess.run(argv, timeout=240, text=True, capture_output=True)
        print(result.stdout, end='', flush=True)
        if result.stderr: print(result.stderr, end='', file=sys.stderr, flush=True)
        try: self.last_receipt = json.loads(result.stdout)
        except ValueError: self.last_receipt = {}
        return result.returncode == 0

    def window(self, release, outer_deadline, environment='production'):
        deadline = min(outer_deadline, time.monotonic() + self.policy['observation_timeout_seconds'])
        healthy = 0
        for attempt in range(self.policy['observation_attempts']):
            if time.monotonic() >= deadline: return False
            passed = self.op('probe', environment, release)
            observation = self.last_receipt.get('observation', {})
            # With automatic restart disabled, a positively identified stopped
            # container needs a recovery action; waiting cannot repair it.
            if observation.get('data_status') == 'fresh' and self.last_receipt.get('identity', {}).get('running') is False:
                return False
            passed = passed and observation.get('data_status') == 'fresh' and observation.get('health') == 'healthy' \
                and observation.get('error_rate', 1) <= self.policy['error_rate_high_gt'] \
                and observation.get('latency_p95_ms', float('inf')) <= self.policy['latency_p95_ms_high_gt']
            if time.monotonic() > deadline: return False
            healthy = healthy + 1 if passed else 0
            if healthy >= self.policy['healthy_observations']:
                return True
            if attempt + 1 < self.policy['observation_attempts']:
                time.sleep(min(self.policy['observation_interval_seconds'], max(0, deadline-time.monotonic())))
        return False

    def run(self):
        try:
            code = self.release()
        except Exception as error:
            # Unexpected/uncertain execution must retain the reservation for review.
            self.decision('stop', reason='controller_exception_reservation_retained', error_type=type(error).__name__)
            return 1
        if self.prepared:
            outcome = {0: 'achieved', 2: 'restored'}.get(code, 'failed')
            self.decision('terminal', outcome=outcome)
            if not self.op('evidence'):
                self.decision('stop', reason='evidence_collection_failed_reservation_retained')
                return 1
            if not self.op('finish', extra=['--outcome', outcome]): return 1
        return code

    def release(self):
        if not self.op('prepare', extra=['--manifest', self.args.manifest]): return 1
        self.prepared = True
        for environment in ('staging', 'production'):
            if not self.op('reset', environment, 'v1') or not self.op('probe', environment, 'v1'):
                return 1
        if not self.op('deploy', 'staging') or not self.window('v2', time.monotonic() + self.policy['observation_timeout_seconds'], 'staging'):
            self.decision('stop', reason='staging_gate_failed')
            return 1
        deployed = self.op('deploy', 'production')
        deadline = time.monotonic() + self.policy['recovery_timeout_seconds']
        if deployed and self.window('v2', deadline): return 0
        for attempt in range(self.policy['restart_attempts']):
            if time.monotonic() >= deadline: break
            if not self.op('diagnose', 'production'): break
            diagnosis = self.last_receipt.get('repair_evidence', {}).get('before', {})
            if diagnosis.get('app_state') != 'stopped' or not diagnosis.get('dependency_ready'): break
            if self.op('restart', 'production') and self.window('v2', deadline): return 0
        for attempt in range(self.policy['rollback_attempts']):
            if time.monotonic() >= deadline: break
            if self.op('rollback', 'production', 'v1') and self.window('v1', deadline):
                return 2  # Restored service is distinct from a successful candidate.
        self.decision('stop', reason='recovery_exhausted')
        return 1

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trial', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--runtime', default=str(Path.home() / 'gotify-study-runtime'))
    return Conventional(p.parse_args()).run()

if __name__ == '__main__':
    sys.exit(main())
