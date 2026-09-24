"""Controller branch tests use fake operations, never inject live failures."""
import argparse
import tempfile
import unittest
from unittest.mock import patch
from conventional import Conventional

class RulesTest(unittest.TestCase):
    def run_rules(self, failure=None, windows=(True,)):
        with tempfile.TemporaryDirectory() as directory:
            controller = Conventional(argparse.Namespace(runtime=directory, trial='unit', manifest='unused'))
            calls = []
            def operation(action, environment=None, release='v2', extra=()):
                calls.append((action, environment, release))
                return (action, environment, release) != failure
            with patch.object(controller, 'op', side_effect=operation), patch.object(controller, 'decision'), \
                 patch.object(controller, 'window', side_effect=windows):
                result = controller.run()
            return result, calls

    def test_staging_failure_blocks_candidate_production(self):
        result, calls = self.run_rules(('probe', 'staging', 'v2'))
        self.assertEqual(result, 1)
        self.assertNotIn(('deploy', 'production', 'v2'), calls)

    def test_failed_baseline_blocks_candidate(self):
        result, calls = self.run_rules(('probe', 'production', 'v1'))
        self.assertEqual(result, 1)
        self.assertFalse(any(action == 'deploy' for action, _, _ in calls))

    def test_healthy_candidate_does_not_recover(self):
        result, calls = self.run_rules()
        self.assertEqual(result, 0)
        self.assertFalse(any(action in ('restart', 'rollback') for action, _, _ in calls))

    def test_successful_restart_retains_candidate(self):
        result, calls = self.run_rules(windows=(False, True))
        self.assertEqual(result, 0)
        self.assertEqual(sum(action == 'restart' for action, _, _ in calls), 1)
        self.assertFalse(any(action == 'rollback' for action, _, _ in calls))

    def test_rollback_is_restoration_not_candidate_success(self):
        result, calls = self.run_rules(windows=(False, False, True))
        self.assertEqual(result, 2)
        self.assertEqual(sum(action == 'restart' for action, _, _ in calls), 1)
        self.assertEqual(sum(action == 'rollback' for action, _, _ in calls), 1)

    def test_exhausted_recovery_stops(self):
        result, calls = self.run_rules(windows=(False, False, False))
        self.assertEqual(result, 1)
        self.assertEqual(sum(action == 'restart' for action, _, _ in calls), 1)
        self.assertEqual(sum(action == 'rollback' for action, _, _ in calls), 1)

if __name__ == '__main__':
    unittest.main()
