import json
from pathlib import Path
import tempfile
import unittest
from boundary import rendezvous


class BoundarySafety(unittest.TestCase):
    def test_unarmed_deployment_does_not_wait(self):
        with tempfile.TemporaryDirectory() as root:
            rendezvous(root, {'approach': 'bdi', 'trial': 'test'}, {})
            self.assertFalse((Path(root) / 'scenarios').exists())

    def test_wrong_ack_cannot_release_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            owner = {'approach': 'bdi', 'trial': 'test'}
            directory = Path(root) / 'scenarios/bdi/test'
            directory.mkdir(parents=True)
            (directory / 'armed.json').write_text(json.dumps({**owner, 'nonce': 'one'}))
            (directory / 'ack.json').write_text(json.dumps({'nonce': 'another', 'operation_id': 'op', 'status': 'PASS'}))
            with self.assertRaisesRegex(RuntimeError, 'acknowledgment_mismatch'):
                rendezvous(root, owner, {'operation_id': 'op', 'identity': {}}, timeout=1)

    def test_missing_harness_ack_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            owner = {'approach': 'bdi', 'trial': 'test'}
            directory = Path(root) / 'scenarios/bdi/test'
            directory.mkdir(parents=True)
            (directory / 'armed.json').write_text(json.dumps({**owner, 'nonce': 'one'}))
            with self.assertRaisesRegex(RuntimeError, 'boundary_timeout'):
                rendezvous(root, owner, {'operation_id': 'op', 'identity': {}}, timeout=0)
