"""Optional external scenario rendezvous; contains no fault or recovery policy."""
import json
import time
from pathlib import Path


def rendezvous(root, owner, receipt, timeout=60):
    directory = Path(root) / 'scenarios' / owner['approach'] / owner['trial']
    arm = directory / 'armed.json'
    if not arm.exists():
        return
    registration = json.loads(arm.read_text())
    if any(registration.get(k) != v for k, v in owner.items()):
        raise RuntimeError('scenario_registration_mismatch')
    event = {**owner, 'boundary': 'production-v2-before-first-observation',
             'nonce': registration['nonce'], 'operation_id': receipt['operation_id'],
             'identity': receipt['identity'], 'ready_at_unix': time.time()}
    temporary = directory / 'ready.tmp'
    temporary.write_text(json.dumps(event, indent=2))
    temporary.replace(directory / 'ready.json')
    receipt['scenario_boundary'] = event
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        acknowledgment = directory / 'ack.json'
        if acknowledgment.exists():
            ack = json.loads(acknowledgment.read_text())
            if ack.get('nonce') != event['nonce'] or ack.get('operation_id') != event['operation_id']:
                raise RuntimeError('scenario_acknowledgment_mismatch')
            if ack.get('status') != 'PASS':
                raise RuntimeError('external_scenario_failed')
            receipt['scenario_boundary']['acknowledged_at_unix'] = time.time()
            return
        time.sleep(.1)
    raise RuntimeError('external_scenario_boundary_timeout')
