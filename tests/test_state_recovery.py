"""Stateful service doubles exercise restart, concurrency, and uncertain writes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from github_agent_router.config import Config
from github_agent_router.github import GitHubClient, GitHubError
from github_agent_router.jules import JulesClient
from github_agent_router.router import RouteState, route, parse_route_state

CONFIG = Config("synthetic", {"a": "A", "b": "B"}, home="a", overflow=None, serialized=True)
EVENT = {"action": "edited", "sender": {"login": "maintainer"},
         "repository": {"full_name": "example/project", "private": True, "default_branch": "main"},
         "issue": {"number": 7, "title": "Task", "body": "Test", "labels": ["jules:run"]}}
BOT = {"login": "github-actions[bot]", "type": "Bot"}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.gh = Mock(spec=GitHubClient)
        self.gh.can_write.side_effect = lambda name: name == "maintainer"
        self.gh.comments.side_effect = lambda number: deepcopy(self.rows)
        self.gh.add_comment.side_effect = self.add
        self.gh.update_comment.side_effect = self.update
        self.jules = Mock(spec=JulesClient)
        self.jules.find_source.return_value = "sources/example/project"
        self.jules.get_session.return_value = {"state": "IN_PROGRESS"}
        self.jules.create_session.return_value = {"name": "sessions/created", "state": "IN_PROGRESS"}
        self.jules.list_sessions.return_value = []
        for p in (patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")),
                  patch("github_agent_router.router.GitHubClient", return_value=self.gh),
                  patch("github_agent_router.router.JulesClient", return_value=self.jules)):
            p.start(); self.addCleanup(p.stop)

    def add(self, number, body):
        row = {"id": len(self.rows) + 1, "body": body, "user": BOT}
        self.rows.append(row)
        return deepcopy(row)

    def update(self, number, body):
        for row in self.rows:
            if row["id"] == number:
                row["body"] = body
                return deepcopy(row)
        raise AssertionError("canonical comment not found")

    def load(self):
        return parse_route_state(self.rows, repository="example/project", number=7, keys=CONFIG.jules_keys)[0]

    def existing(self, **kwargs):
        data = {"owner": "a", "session": "sessions/existing", "repository": "example/project", "number": 7, "state": "IN_PROGRESS", "comment_id": len(self.rows) + 1}
        data.update(kwargs)
        state = RouteState(**data)
        self.add(7, state.comment(key="A"))
        return state

    def test_initial_and_duplicate_event_create_once(self):
        route(CONFIG, "issues", EVENT)
        route(CONFIG, "issues", EVENT)
        self.jules.create_session.assert_called_once()
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.load().session, "sessions/created")
        self.assertEqual(self.load().pending, "")

    def test_two_concurrent_events_create_once(self):
        start = Barrier(2)
        def call():
            start.wait(timeout=3)
            return route(CONFIG, "issues", deepcopy(EVENT))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(call) for _ in range(2)]
            for result in results:
                result.result(timeout=5)
        self.jules.create_session.assert_called_once()
        self.assertEqual(len(self.rows), 1)

    def test_label_write_failure_preserves_external_result(self):
        self.gh.add_labels.side_effect = [GitHubError("failure", 500), None]
        with self.assertRaises(GitHubError):
            route(CONFIG, "issues", EVENT)
        self.assertEqual(self.load().session, "sessions/created")
        route(CONFIG, "issues", EVENT)
        self.jules.create_session.assert_called_once()

    def test_waiting_label_failure_is_repaired_on_duplicate(self):
        self.existing()
        self.jules.get_session.return_value = {"state": "AWAITING_USER_FEEDBACK"}
        def fail_once(number, labels):
            if labels == ["jules:needs-user"]:
                raise GitHubError("lost label write", 500)
        self.gh.add_labels.side_effect = fail_once
        with self.assertRaises(GitHubError):
            route(CONFIG, "issues", EVENT)
        self.gh.add_labels.side_effect = None
        self.gh.add_labels.reset_mock()
        route(CONFIG, "issues", EVENT)
        self.gh.add_labels.assert_any_call(7, ["jules:needs-user"])
        self.jules.send_message.assert_not_called()

    def test_creation_timeout_recovers_by_operation_not_another_post(self):
        def created_then_timeout(**kwargs):
            self.jules.list_sessions.return_value = [{"name": "sessions/recovered", "state": "IN_PROGRESS", "prompt": kwargs["prompt"], "sourceContext": {"source": kwargs["source"]}}]
            raise TimeoutError("synthetic lost response")
        self.jules.create_session.side_effect = created_then_timeout
        with self.assertRaises(TimeoutError):
            route(CONFIG, "issues", EVENT)
        self.assertEqual(self.load().pending, "create")
        self.assertIn("recovered", route(CONFIG, "issues", EVENT))
        route(CONFIG, "issues", EVENT)
        self.jules.create_session.assert_called_once()
        self.assertEqual(self.load().session, "sessions/recovered")

    def test_no_match_after_timeout_blocks_instead_of_retrying(self):
        self.jules.create_session.side_effect = TimeoutError("synthetic")
        with self.assertRaises(TimeoutError):
            route(CONFIG, "issues", EVENT)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            route(CONFIG, "issues", EVENT)
        self.jules.create_session.assert_called_once()

    def test_reservation_write_failure_never_posts_to_jules(self):
        self.gh.add_comment.side_effect = GitHubError("failure", 500)
        with self.assertRaises(GitHubError):
            route(CONFIG, "issues", EVENT)
        self.jules.create_session.assert_not_called()

    def test_result_persistence_failure_leaves_recoverable_reservation(self):
        def create(**kwargs):
            self.jules.list_sessions.return_value = [{"name": "sessions/created", "prompt": kwargs["prompt"], "sourceContext": {"source": kwargs["source"]}}]
            return {"name": "sessions/created"}
        self.jules.create_session.side_effect = create
        def fail_after_creation(number, body):
            if self.jules.create_session.called:
                raise GitHubError("failure", 500)
            return self.update(number, body)
        self.gh.update_comment.side_effect = fail_after_creation
        with self.assertRaises(GitHubError):
            route(CONFIG, "issues", EVENT)
        self.gh.update_comment.side_effect = self.update
        route(CONFIG, "issues", EVENT)
        self.jules.create_session.assert_called_once()

    def test_send_timeout_is_not_replayed(self):
        self.existing()
        self.jules.send_message.side_effect = TimeoutError("synthetic")
        with self.assertRaises(TimeoutError):
            route(CONFIG, "issues", EVENT)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            route(CONFIG, "issues", EVENT)
        self.jules.send_message.assert_called_once()

    def test_signed_foreign_context_blocks(self):
        self.existing(number=8)
        with self.assertRaisesRegex(ValueError, "different repository or issue"):
            route(CONFIG, "issues", EVENT)
        self.jules.get_session.assert_not_called()
        self.jules.create_session.assert_not_called()

    def test_unsigned_legacy_bot_state_blocks_instead_of_restarting(self):
        self.add(7, RouteState("a", "sessions/legacy").comment())
        with self.assertRaisesRegex(ValueError, "legacy"):
            route(CONFIG, "issues", EVENT)
        self.jules.create_session.assert_not_called()

    def test_outsider_cannot_replay_signed_old_state(self):
        old = RouteState("a", "sessions/old", repository="example/project", number=7, comment_id=1)
        self.existing()
        self.rows.append({"id": 99, "user": {"login": "outsider", "type": "User"}, "body": old.comment(key="A")})
        route(CONFIG, "issues", EVENT)
        self.jules.get_session.assert_called_once_with("sessions/existing")
        self.assertEqual(self.gh.update_comment.call_args.args[0], 1)

    def test_authorized_writer_quote_cannot_replay_old_state(self):
        old = RouteState("a", "sessions/old", repository="example/project", number=7, comment_id=1)
        self.existing()
        self.rows.append({"id": 99, "user": {"login": "maintainer", "type": "User"}, "body": old.comment(key="A")})
        route(CONFIG, "issues", EVENT)
        self.jules.get_session.assert_called_once_with("sessions/existing")
        self.assertEqual(self.gh.update_comment.call_args.args[0], 1)

    def test_unknown_state_preserves_round(self):
        self.existing(round=2)
        self.jules.get_session.return_value = {"state": "STATE_UNSPECIFIED"}
        route(CONFIG, "issues", EVENT)
        self.jules.send_message.assert_not_called()
        self.jules.create_session.assert_not_called()
        self.assertEqual(self.load().round, 2)

    def test_unserialized_execution_is_read_only(self):
        with self.assertRaisesRegex(ValueError, "SERIALIZED"):
            route(replace(CONFIG, serialized=False), "issues", EVENT)
        self.gh.add_comment.assert_not_called()
        self.jules.create_session.assert_not_called()

    def test_policy_conflict_does_not_transfer_owner(self):
        self.existing()
        with self.assertRaisesRegex(ValueError, "not permitted"):
            route(replace(CONFIG, allowed_owners=("b",)), "issues", EVENT)
        self.jules.create_session.assert_not_called()

    def test_all_event_mutations_require_write_permission(self):
        event = deepcopy(EVENT); event["sender"]["login"] = "outsider"
        self.assertIn("without repository write", route(CONFIG, "issues", event))
        self.gh.add_comment.assert_not_called()
        self.jules.create_session.assert_not_called()

    def test_fork_branch_is_not_mistaken_for_target_branch(self):
        event = deepcopy(EVENT)
        event["pull_request"] = event.pop("issue")
        event["pull_request"]["head"] = {"ref": "main", "repo": {"full_name": "outsider/fork"}}
        self.assertIn("fork", route(CONFIG, "pull_request", event))
        self.jules.create_session.assert_not_called()

    def test_owner_label_without_verified_state_does_not_duplicate(self):
        event = deepcopy(EVENT); event["issue"]["labels"].append("jules-owner:a")
        with self.assertRaisesRegex(ValueError, "without verified session"):
            route(CONFIG, "issues", event)
        self.jules.create_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
