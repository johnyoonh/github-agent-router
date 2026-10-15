import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tighten_ruleset import plan, apply


class RulesetTests(unittest.TestCase):
    def setUp(self):
        self.current = {'id': 7, 'source': 'a/b', 'source_type': 'Repository',
            'target': 'branch', 'enforcement': 'active', 'rules': [{'type': 'pull_request'}],
            'bypass_actors': [{'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'always'},
                              {'actor_type': 'Integration', 'actor_id': 9, 'bypass_mode': 'always'}]}
    def test_only_selected_actor_narrowed_and_rules_not_sent(self):
        proposed = plan(self.current, 'a/b', 7, 5)
        self.assertEqual(set(proposed['body']), {'bypass_actors'})
        self.assertEqual(proposed['body']['bypass_actors'][0]['bypass_mode'], 'pull_request')
        self.assertEqual(proposed['body']['bypass_actors'][1], self.current['bypass_actors'][1])
        self.assertEqual(self.current['bypass_actors'][0]['bypass_mode'], 'always')
    def test_unknown_actor_or_wrong_scope_rejected(self):
        with self.assertRaises(ValueError): plan(self.current, 'other/repo', 7, 5)
        with self.assertRaises(ValueError): plan(self.current, 'a/b', 7, 6)
    def test_drift_blocks_before_write(self):
        proposed = plan(self.current, 'a/b', 7, 5)
        def request(method, path):
            self.assertEqual(method, 'GET')
            return {**self.current, 'name': 'changed'}
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / 'backup.json'
            with self.assertRaises(ValueError): apply(request, 'path', self.current, proposed, backup)
            self.assertFalse(backup.exists())
    def test_apply_has_backup_and_verified_partial_write(self):
        state = copy.deepcopy(self.current)
        proposed = plan(state, 'a/b', 7, 5)
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / 'backup.json'
            def request(method, path, body=None):
                if method == 'PUT':
                    self.assertTrue(backup.exists())
                    state.update(body)
                return copy.deepcopy(state)
            self.assertEqual(apply(request, 'path', self.current, proposed, backup)['status'], 'applied')
            self.assertEqual(state['rules'], self.current['rules'])


if __name__ == '__main__': unittest.main()
