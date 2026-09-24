import argparse
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from operations import Operations

class OperationSafetyTest(unittest.TestCase):
    def operation(self, directory, action='observe'):
        args = argparse.Namespace(runtime=directory, approach='conventional', trial='unit',
                                  operation=action, environment='production', release='v2', execution_id=None)
        op = Operations(args)
        op.state = {'manifest': {'source_sha': 'a'*40}, 'environments': {'production': {
            'password': 'test-only', 'app_token': 'test-only', 'baseline_verified': False,
            'sentinel': {'id': 1, 'message': 'baseline'}}}}
        return op

    def test_stop_during_successful_requests_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            op = self.operation(directory)
            def request(path, body=None, token=None):
                if path == '/health': return {'health': 'green', 'database': 'green'}
                if path == '/version': return {'commit': 'a'*40, 'version': 'experiment-v2'}
                if body: self.payload = body['message']; return {'id': 2}
                return {'messages': [{'id': 2, 'message': self.payload}, {'id': 1, 'message': 'baseline'}]}
            with patch.object(op, 'identity', side_effect=[True, False]), patch.object(op, 'request', side_effect=request):
                with self.assertRaisesRegex(RuntimeError, 'stopped_during_probe'):
                    op.probe(False)
            self.assertEqual(op.receipt['observation']['health'], 'unhealthy')

    def test_unknown_command_completion_blocks_later_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            op = self.operation(directory, 'restart')
            with patch('operations.subprocess.run', side_effect=subprocess.TimeoutExpired(['docker'], 180)):
                with self.assertRaisesRegex(RuntimeError, 'execution_unresolved'):
                    op.command(['docker', 'compose', 'up'])
            self.assertTrue(op.unresolved.exists())
            with self.assertRaisesRegex(RuntimeError, 'requires_reconciliation'):
                op.execute()

    def test_deployment_requires_verified_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            op = self.operation(directory, 'deploy')
            with patch.object(op, 'compose') as compose:
                with self.assertRaisesRegex(RuntimeError, 'verified_baseline'):
                    op.deploy()
                compose.assert_not_called()

if __name__ == '__main__':
    unittest.main()
