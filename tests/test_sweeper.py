import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from github_agent_router.sweeper import sweep, Checkpoint, lease


class API:
    repository = 'a/b'
    def __init__(self, timeout=False):
        self.writes = []
        self.timeout = timeout
    def _request(self, method, path, payload=None):
        if method == 'GET':
            return [{'number': 7, 'labels': [{'name': 'jules-owner:a'}],
                     'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}]
        self.writes.append(copy.deepcopy(payload))
        if self.timeout:
            raise TimeoutError('response lost')
        return {}


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkpoint = Checkpoint(Path(self.tmp.name) / 'state.json')
        self.state = {'owner': 'a', 'session': 'sessions/1', 'head_sha': 'a' * 40,
                      'base_sha': 'b' * 40, 'next_probe_at': 0}
        self.api = API()
        self.status = 'COMPLETED'
        self.calls = 0
    def factory(self, key):
        case = self
        class Jules:
            def get_session(self, session):
                case.calls += 1
                return {'state': case.status}
        return Jules()
    def run_sweep(self, **kwargs):
        with patch('github_agent_router.sweeper.read_state', return_value=self.state):
            return sweep(self.api, {'a': 'key'}, self.factory, self.checkpoint,
                         now=1000, **kwargs)
    def test_pending_has_backoff_no_dispatch(self):
        self.status = 'IN_PROGRESS'
        self.assertEqual(self.run_sweep()[0]['status'], 'pending')
        self.assertGreater(self.checkpoint['a/b#7']['due'], 1000)
        self.assertEqual(self.api.writes, [])
    def test_terminal_is_reserved_before_dispatch(self):
        self.assertEqual(self.run_sweep()[0]['status'], 'dispatched')
        stored = Checkpoint(self.checkpoint.path)
        self.assertIn('reserved_fingerprint', stored['a/b#7'])
        self.assertEqual(self.run_sweep()[0]['status'], 'dispatch_reserved_or_uncertain')
        self.assertEqual(len(self.api.writes), 1)
    def test_uncertain_post_is_not_retried(self):
        self.api.timeout = True
        with self.assertRaises(TimeoutError): self.run_sweep()
        self.api.timeout = False
        self.assertEqual(self.run_sweep()[0]['status'], 'dispatch_reserved_or_uncertain')
        self.assertEqual(len(self.api.writes), 1)
    def test_dry_run_does_not_write_checkpoint_or_remote(self):
        self.assertEqual(self.run_sweep(dry_run=True)[0]['status'], 'would_dispatch')
        self.assertEqual(dict(self.checkpoint), {})
        self.assertFalse(self.checkpoint.path.exists())
        self.assertEqual(self.api.writes, [])
    def test_pending_mutation_needs_reconciliation(self):
        self.state['pending'] = 'send'
        self.assertEqual(self.run_sweep()[0]['status'], 'reconciliation_required')
        self.assertEqual(self.calls, 0)
    def test_lease_excludes_second_process(self):
        with lease(self.checkpoint.path):
            with self.assertRaises(RuntimeError):
                with lease(self.checkpoint.path): pass


if __name__ == '__main__': unittest.main()
