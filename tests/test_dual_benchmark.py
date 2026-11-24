"""Independent quality and accounting guards for the simulated benchmark."""
import tempfile
import unittest
from pathlib import Path
from tools.dual_efficiency_benchmark import check_artifacts, _metrics, run_case
import dual_ai


class BenchmarkChecks(unittest.TestCase):
    def test_false_success_and_missing_or_changed_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertTrue(check_artifacts(root, 'publish_prep', ['prepare', 'verify']))
            (root / 'VERSION.v1').write_text('1.0\n')
            (root / 'VERSION').write_text('2.0\n')
            (root / 'RELEASE.md').write_text(
                'v1.0 preserved. v2.0 prepared. Not published. User runs authenticated commands.\n')
            self.assertEqual(check_artifacts(root, 'publish_prep', ['prepare', 'verify']), [])
            (root / 'VERSION.v1').write_text('2.0\n')
            self.assertTrue(check_artifacts(root, 'publish_prep', ['prepare', 'verify']))
            self.assertTrue(check_artifacts(root, 'publish_prep', ['prepare', 'prepare', 'verify']))

    def test_partial_cost_is_not_a_total_and_cache_uses_measured_calls(self):
        result = _metrics({'stats': {'worker': {
            'calls': 2, 'input_tokens': 1100, 'cache_read_input_tokens': 80,
            'cache_measured_input': 100, 'cache_measured_read': 80,
            'cache_measured_calls': 1, 'cost': 0.1, 'cost_known_calls': 1}}})
        self.assertEqual(result['cache_hit_rate'], 0.8)
        self.assertIsNone(result['cost_usd'])
        self.assertEqual(result['known_cost_subtotal_usd'], 0.1)
        self.assertEqual(result['cost_coverage'], 0.5)

    def test_long_history_counts_actions_per_round(self):
        case = run_case(dual_ai, 'test', 'long_history', 'dual')
        self.assertEqual(len(case['rounds']), 2)
        for row in case['rounds']:
            self.assertTrue(row['ok'], row)
            self.assertEqual(row['actions'], ['prepare', 'verify'])
            self.assertEqual(row['repeated_side_effects'], 0)
