"""Regression-first checks; all external services are mocked."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from github_agent_router.config import Config
from github_agent_router.github import GitHubClient
from github_agent_router.jules import JulesClient
from github_agent_router.router import RouteState, parse_route_state, route, setup_repo_labels

CONFIG = Config(github_token="synthetic", jules_keys={"a": "A", "b": "B"}, home="a", overflow=None, serialized=True)
PAYLOAD = {"action": "edited", "repository": {"full_name": "example/project", "private": True},
           "sender": {"login": "maintainer"},
           "issue": {"number": 7, "title": "Task", "body": "Test", "labels": ["jules:run"]}}


def comment(author="github-actions[bot]"):
    state = RouteState("a", "sessions/existing", state="IN_PROGRESS")
    # After the fix, fixtures use the same context-bound format as production.
    if hasattr(state, "repository"):
        state.repository, state.number, state.comment_id = "example/project", 7, 10
        body = state.comment(key="A")
    else:
        body = state.comment()
    return {"id": 10, "body": body,
            "user": {"login": author, "type": "Bot" if author.endswith("[bot]") else "User"}}


class SafetyRegressions(unittest.TestCase):
    def setUp(self):
        p = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        p.start(); self.addCleanup(p.stop)

    def run_route(self, config, payload, event="issues", session_state="IN_PROGRESS", authorized=True):
        gh = Mock(spec=GitHubClient)
        gh.comments.return_value = [comment()]
        if hasattr(GitHubClient, "can_write"):
            gh.can_write.return_value = authorized
        jules = Mock(spec=JulesClient)
        jules.get_session.return_value = {"state": session_state}
        jules.find_source.return_value = "sources/example/project"
        jules.create_session.return_value = {"name": "sessions/new", "state": "IN_PROGRESS"}
        with patch("github_agent_router.router.GitHubClient", return_value=gh), \
             patch("github_agent_router.router.JulesClient", return_value=jules):
            result = route(config, event, deepcopy(payload))
        return gh, jules, result

    def test_dry_run_all_existing_session_states_do_not_write(self):
        for state in ("IN_PROGRESS", "COMPLETED", "AWAITING_USER_FEEDBACK", "STATE_UNSPECIFIED"):
            with self.subTest(state=state):
                gh, jules, _ = self.run_route(replace(CONFIG, dry_run=True), PAYLOAD, session_state=state)
                for method in ("add_comment", "update_comment", "upsert_router_comment", "add_labels", "remove_label", "setup_labels"):
                    getattr(gh, method).assert_not_called()
                jules.send_message.assert_not_called()
                jules.create_session.assert_not_called()

    def test_dry_run_comment_does_not_write(self):
        payload = deepcopy(PAYLOAD)
        payload["comment"] = {"id": 4, "body": "/jules keep logs", "user": {"login": "maintainer"}}
        gh, jules, _ = self.run_route(replace(CONFIG, dry_run=True), payload, "issue_comment")
        gh.remove_label.assert_not_called(); jules.send_message.assert_not_called()

    def test_untrusted_actor_does_not_send(self):
        payload = deepcopy(PAYLOAD)
        payload["sender"] = {"login": "outsider"}
        payload["comment"] = {"id": 4, "body": "/jules keep logs", "user": {"login": "outsider"}}
        gh, jules, _ = self.run_route(CONFIG, payload, "issue_comment", authorized=False)
        jules.send_message.assert_not_called(); gh.remove_label.assert_not_called()

    def test_user_authored_marker_is_not_state(self):
        state, _ = parse_route_state([comment("outsider")])
        self.assertIsNone(state)

    def test_unknown_state_does_not_send_or_create(self):
        gh, jules, _ = self.run_route(CONFIG, PAYLOAD, session_state="STATE_UNSPECIFIED")
        jules.create_session.assert_not_called(); jules.send_message.assert_not_called()

    def test_label_setup_dry_run_does_not_write(self):
        with patch("github_agent_router.router.GitHubClient") as cls:
            setup_repo_labels(replace(CONFIG, dry_run=True), "example/project")
            cls.return_value.setup_labels.assert_not_called()

    def test_comment_pagination_reads_second_page(self):
        gh = GitHubClient("synthetic", "example/project")
        first = [{"id": n} for n in range(100)]
        with patch.object(gh, "_request", side_effect=[first, [comment()]]) as req:
            rows = gh.comments(7)
        self.assertEqual(len(rows), 101)
        self.assertEqual(req.call_count, 2)

    def test_conflicting_owner_labels_rejected(self):
        from github_agent_router.router import owner_from_labels
        with self.assertRaises(ValueError):
            owner_from_labels({"jules-owner:a", "jules-owner:b"})


if __name__ == "__main__":
    unittest.main()
