import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_agent_router.config import Config

from github_agent_router.github import GitHubClient, GitHubError, REQUIRED_LABELS
from github_agent_router.jules import JulesClient, JulesError
from github_agent_router.router import (
    RouteState,
    build_prompt,
    check_credentials,
    choose_owner,
    event_message,
    owner_from_labels,
    parse_route_state,
    preferred_home,
    route,
    setup_repo_labels,
    should_route_pr,
)


def cfg(**kwargs):
    base = dict(
        github_token="gh-test-token",
        jules_keys={"a": "key-a", "b": "key-b"},
        home="a",
        overflow="b",
        max_rounds=2,
        auto_review_prs=False,
        dry_run=False,
    )
    base.update(kwargs)
    return Config(**base)


class RouterUnitTests(unittest.TestCase):
    def test_sticky_owner_beats_home(self):
        self.assertEqual(choose_owner({"jules-owner:b"}, cfg(home="a")), "b")
        self.assertEqual(choose_owner({"jules-owner:a"}, cfg(home="b")), "a")

    def test_home_used_for_unclaimed_work(self):
        self.assertEqual(choose_owner(set(), cfg(home="a")), "a")
        self.assertEqual(choose_owner(set(), cfg(home="b")), "b")

    def test_missing_home_key_uses_overflow(self):
        self.assertEqual(choose_owner(set(), cfg(jules_keys={"a": "", "b": "key-b"})), "b")

    def test_no_keys_raises_value_error(self):
        with self.assertRaises(ValueError):
            choose_owner(set(), cfg(jules_keys={"a": "", "b": ""}))

    def test_unknown_home_raises_value_error(self):
        with self.assertRaises(ValueError):
            choose_owner(set(), cfg(home="c"))

    def test_auto_home_uses_repository_visibility(self):
        config = cfg(home="auto", overflow="auto", private_home="a", public_home="b")
        self.assertEqual(preferred_home(config, repository_private=True), "a")
        self.assertEqual(preferred_home(config, repository_private=False), "b")
        self.assertEqual(choose_owner(set(), config, repository_private=True), "a")
        self.assertEqual(choose_owner(set(), config, repository_private=False), "b")

    def test_auto_overflow_uses_the_other_account(self):
        config = cfg(
            home="auto",
            overflow="auto",
            private_home="a",
            public_home="b",
            jules_keys={"a": "", "b": "key-b"},
        )
        self.assertEqual(choose_owner(set(), config, repository_private=True), "b")

    def test_auto_home_requires_visibility(self):
        with self.assertRaisesRegex(ValueError, "repository visibility"):
            choose_owner(set(), cfg(home="auto", overflow="auto"))

    def test_explicit_home_overrides_visibility_for_to_be_public_repo(self):
        config = cfg(home="b", overflow="auto", private_home="a", public_home="b")
        self.assertEqual(preferred_home(config, repository_private=True), "b")

    def test_environment_defaults_to_visibility_routing(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()

        self.assertEqual(config.home, "auto")
        self.assertEqual(config.overflow, "auto")
        self.assertEqual(config.private_home, "a")
        self.assertEqual(config.public_home, "b")

    def test_owner_labels(self):
        self.assertEqual(owner_from_labels({"bug", "jules-owner:a"}), "a")
        self.assertEqual(owner_from_labels({"jules-owner:b"}), "b")
        self.assertIsNone(owner_from_labels({"bug"}))

    def test_marker_round_trip(self):
        state = RouteState("a", "sessions/123", "https://jules.google/x", 2, "IN_PROGRESS")
        comment_body = state.comment()
        self.assertNotIn(r"\`", comment_body)
        self.assertIn("`sessions/123`", comment_body)
        parsed, comment_id = parse_route_state([{"id": 9, "body": comment_body}])
        self.assertEqual(comment_id, 9)
        self.assertEqual(parsed, state)

    def test_pr_requires_label_by_default(self):
        pr = {"labels": [], "head": {"ref": "chatgpt/feature"}}
        self.assertFalse(should_route_pr(pr, cfg()))

    def test_pr_auto_review_can_be_enabled(self):
        pr = {"labels": [], "head": {"ref": "chatgpt/feature"}}
        self.assertTrue(should_route_pr(pr, cfg(auto_review_prs=True)))

    def test_pr_with_jules_run_label(self):
        pr = {"labels": [{"name": "jules:run"}], "head": {"ref": "chatgpt/feature"}}
        self.assertTrue(should_route_pr(pr, cfg(auto_review_prs=False)))

    def test_pr_sticky_owner_allows_continuation(self):
        pr = {"labels": [{"name": "jules-owner:a"}], "head": {"ref": "chatgpt/feature"}}
        self.assertTrue(should_route_pr(pr, cfg(auto_review_prs=False)))

    def test_jules_pr_is_not_re_reviewed(self):
        pr = {"labels": [{"name": "agent:jules"}], "head": {"ref": "jules/fix"}}
        self.assertFalse(should_route_pr(pr, cfg(auto_review_prs=True)))
        pr_branch = {"labels": [], "head": {"ref": "google-jules/fix"}}
        self.assertFalse(should_route_pr(pr_branch, cfg(auto_review_prs=True)))

    def test_build_prompt_issue(self):
        payload = {
            "repository": {"full_name": "owner/repo", "default_branch": "main"},
            "issue": {"number": 42, "title": "Fix bug", "body": "Details here", "labels": []},
        }
        title, prompt, branch, number, labels = build_prompt(payload)
        self.assertEqual(number, 42)
        self.assertEqual(branch, "main")
        self.assertIn("Fix bug", title)
        self.assertIn("Details here", prompt)

    def test_build_prompt_pr(self):
        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 10,
                "title": "Add feature",
                "body": "PR description",
                "head": {"ref": "feat-branch"},
                "labels": [{"name": "jules:run"}],
            },
        }
        title, prompt, branch, number, labels = build_prompt(payload)
        self.assertEqual(number, 10)
        self.assertEqual(branch, "feat-branch")
        self.assertIn("Review PR #10", title)
        self.assertIn("PR description", prompt)
        self.assertIn("jules:run", labels)

    def test_event_message(self):
        self.assertEqual(
            event_message("issue_comment", {"comment": {"body": "hello"}}),
            "hello",
        )
        self.assertIn(
            "abc1234",
            event_message("pull_request", {"pull_request": {"head": {"sha": "abc1234"}}}),
        )
        self.assertIn(
            "requirements",
            event_message("issues", {"issue": {"number": 1}}),
        )


class RouteFlowTests(unittest.TestCase):
    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_unlabeled_issue_ignored(self, mock_jules, mock_gh):
        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1, "title": "Test", "labels": []},
        }
        result = route(cfg(), "issues", payload)
        self.assertIn("ignored issue: jules:run absent", result)
        mock_jules.return_value.create_session.assert_not_called()

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_labeled_issue_creates_session(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_gh.comments.return_value = []
        mock_jules = mock_jules_cls.return_value
        mock_jules.find_source.return_value = "sources/123"
        mock_jules.create_session.return_value = {
            "name": "sessions/s1",
            "url": "https://jules.google/s1",
            "state": "IN_PROGRESS",
        }

        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1, "title": "Test", "labels": [{"name": "jules:run"}]},
        }
        result = route(cfg(), "issues", payload)
        self.assertIn("created sessions/s1 on Jules a", result)
        mock_gh.setup_labels.assert_called_once_with()
        mock_gh.add_labels.assert_called_with(1, ["jules-owner:a"])
        mock_gh.upsert_router_comment.assert_called()

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_auto_home_uses_public_repository_for_jules_b(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_gh.comments.return_value = []
        mock_gh.is_private.return_value = False
        mock_jules = mock_jules_cls.return_value
        mock_jules.find_source.return_value = "sources/public"
        mock_jules.create_session.return_value = {
            "name": "sessions/public-b",
            "url": "https://jules.google/public-b",
            "state": "IN_PROGRESS",
        }

        payload = {
            "repository": {"full_name": "owner/public-repo"},
            "issue": {"number": 2, "title": "Public", "labels": [{"name": "jules:run"}]},
        }
        result = route(cfg(home="auto", overflow="auto"), "issues", payload)

        self.assertIn("created sessions/public-b on Jules b", result)
        mock_jules_cls.assert_called_once_with("key-b")
        mock_gh.is_private.assert_called_once_with()

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_overflow_on_429(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_gh.comments.return_value = []

        def client_side_effect(api_key):
            client = MagicMock()
            if api_key == "key-a":
                client.find_source.side_effect = JulesError(429, "rate limited")
            else:
                client.find_source.return_value = "sources/b123"
                client.create_session.return_value = {
                    "name": "sessions/overflow-b",
                    "url": "https://jules.google/overflow-b",
                    "state": "IN_PROGRESS",
                }
            return client

        mock_jules_cls.side_effect = client_side_effect

        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1, "title": "Test", "labels": [{"name": "jules:run"}]},
        }
        result = route(cfg(home="a", overflow="b"), "issues", payload)
        self.assertIn("created sessions/overflow-b on Jules b", result)
        mock_gh.add_labels.assert_called_with(1, ["jules-owner:b"])

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_auto_overflow_on_429_uses_other_account(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_gh.comments.return_value = []
        mock_gh.is_private.return_value = True

        def client_side_effect(api_key):
            client = MagicMock()
            if api_key == "key-a":
                client.find_source.side_effect = JulesError(429, "rate limited")
            else:
                client.find_source.return_value = "sources/b123"
                client.create_session.return_value = {
                    "name": "sessions/auto-overflow-b",
                    "url": "https://jules.google/auto-overflow-b",
                    "state": "IN_PROGRESS",
                }
            return client

        mock_jules_cls.side_effect = client_side_effect

        payload = {
            "repository": {"full_name": "owner/private-repo"},
            "issue": {"number": 3, "title": "Private", "labels": [{"name": "jules:run"}]},
        }
        result = route(cfg(home="auto", overflow="auto"), "issues", payload)

        self.assertIn("created sessions/auto-overflow-b on Jules b", result)

    def test_persist_records_session_before_owner_label(self):
        from github_agent_router.router import persist

        events = []
        gh = MagicMock()
        gh.setup_labels.side_effect = lambda: events.append("setup_labels")
        gh.upsert_router_comment.side_effect = lambda *args: events.append("comment")
        gh.add_labels.side_effect = lambda *args: events.append("owner_label")
        gh.remove_label.side_effect = lambda *args: events.append("remove_other")

        persist(gh, 4, RouteState("a", "sessions/4"))

        self.assertEqual(events, ["setup_labels", "comment", "owner_label", "remove_other"])

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_active_session_continuation(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        existing_state = RouteState("a", "sessions/s1", "https://jules.google/s1", 1, "IN_PROGRESS")
        mock_gh.comments.return_value = [{"id": 10, "body": existing_state.comment()}]

        mock_jules = mock_jules_cls.return_value
        mock_jules.get_session.return_value = {"state": "IN_PROGRESS", "url": "https://jules.google/s1"}

        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1, "title": "Test", "labels": [{"name": "jules:run"}]},
        }
        result = route(cfg(home="auto", overflow="auto"), "issues", payload)
        self.assertIn("continued active sticky session sessions/s1", result)
        mock_jules.send_message.assert_called()
        mock_gh.is_private.assert_not_called()

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_awaiting_user_feedback(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        existing_state = RouteState("a", "sessions/s1", "https://jules.google/s1", 1, "IN_PROGRESS")
        mock_gh.comments.return_value = [{"id": 10, "body": existing_state.comment()}]

        mock_jules = mock_jules_cls.return_value
        mock_jules.get_session.return_value = {"state": "AWAITING_USER_FEEDBACK", "url": "https://jules.google/s1"}

        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1, "title": "Test", "labels": [{"name": "jules:run"}]},
        }
        result = route(cfg(), "issues", payload)
        self.assertIn("awaits user feedback", result)
        mock_gh.add_labels.assert_any_call(1, ["jules:needs-user"])

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_terminal_creates_next_round(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        existing_state = RouteState("a", "sessions/s1", "https://jules.google/s1", 1, "COMPLETED")
        mock_gh.comments.return_value = [{"id": 10, "body": existing_state.comment()}]

        mock_jules = mock_jules_cls.return_value
        mock_jules.get_session.return_value = {"state": "COMPLETED", "url": "https://jules.google/s1"}
        mock_jules.find_source.return_value = "sources/123"
        mock_jules.create_session.return_value = {
            "name": "sessions/s2",
            "url": "https://jules.google/s2",
            "state": "IN_PROGRESS",
        }

        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 5,
                "title": "PR 5",
                "head": {"ref": "fix-1"},
                "labels": [{"name": "jules:run"}],
            },
        }
        result = route(cfg(max_rounds=2), "pull_request", payload)
        self.assertIn("created sticky follow-up sessions/s2", result)

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_route_terminal_max_rounds_adds_needs_user(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        existing_state = RouteState("a", "sessions/s2", "https://jules.google/s2", 2, "COMPLETED")
        mock_gh.comments.return_value = [{"id": 10, "body": existing_state.comment()}]

        mock_jules = mock_jules_cls.return_value
        mock_jules.get_session.return_value = {"state": "COMPLETED", "url": "https://jules.google/s2"}

        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 5,
                "title": "PR 5",
                "head": {"ref": "fix-1"},
                "labels": [{"name": "jules:run"}],
            },
        }
        result = route(cfg(max_rounds=2), "pull_request", payload)
        self.assertIn("stopped at max Jules rounds (2)", result)
        mock_gh.add_labels.assert_any_call(5, ["jules:needs-user"])


    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_issue_comment_routes_jules_prompt(self, mock_jules_cls, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        existing_state = RouteState("a", "sessions/s1", "https://jules.google/s1", 1, "IN_PROGRESS")
        mock_gh.comments.return_value = [{"id": 10, "body": existing_state.comment()}]
        mock_jules = mock_jules_cls.return_value

        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1},
            "comment": {"body": "/jules: please retain user logs"},
        }
        result = route(cfg(), "issue_comment", payload)
        self.assertIn("sent message to sessions/s1", result)
        mock_jules.send_message.assert_called_with("sessions/s1", "please retain user logs")
        mock_gh.remove_label.assert_called_with(1, "jules:needs-user")

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_issue_comment_empty_or_ignored(self, mock_jules_cls, mock_gh_cls):
        payload_non_jules = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1},
            "comment": {"body": "LGTM"},
        }
        self.assertEqual(
            route(cfg(), "issue_comment", payload_non_jules),
            "ignored issue comment without /jules",
        )

        payload_empty = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1},
            "comment": {"body": "/jules"},
        }
        mock_gh = mock_gh_cls.return_value
        existing_state = RouteState("a", "sessions/s1", "https://jules.google/s1", 1, "IN_PROGRESS")
        mock_gh.comments.return_value = [{"id": 10, "body": existing_state.comment()}]
        self.assertEqual(
            route(cfg(), "issue_comment", payload_empty),
            "ignored empty /jules command",
        )

    def test_dry_run_mode(self):
        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 7, "title": "Test", "labels": [{"name": "jules:run"}]},
        }
        with patch("github_agent_router.router.GitHubClient") as mock_gh:
            mock_gh.return_value.comments.return_value = []
            result = route(cfg(dry_run=True), "issues", payload)
            self.assertIn("dry-run: would route #7 to Jules a", result)


class SetupAndCheckTests(unittest.TestCase):
    @patch("github_agent_router.github.GitHubClient._request")
    def test_repository_visibility_is_read_from_github(self, mock_request):
        mock_request.return_value = {"private": True}

        self.assertTrue(GitHubClient("token", "owner/repo").is_private())
        mock_request.assert_called_once_with("GET", "/repos/owner/repo")

    @patch("github_agent_router.github.GitHubClient._request")
    def test_repository_visibility_requires_boolean_response(self, mock_request):
        for response in ({}, {"private": "false"}):
            mock_request.return_value = response
            with self.assertRaisesRegex(GitHubError, "boolean private"):
                GitHubClient("token", "owner/repo").is_private()

    @patch("github_agent_router.github.GitHubClient._request")
    def test_setup_labels(self, mock_request):
        gh = GitHubClient("token", "owner/repo")
        labels = gh.setup_labels()
        self.assertEqual(len(labels), len(REQUIRED_LABELS))
        self.assertIn("jules:run", labels)
        self.assertIn("jules-owner:a", labels)
        self.assertIn("jules-owner:b", labels)
        self.assertIn("jules:needs-user", labels)
        self.assertIn("agent:jules", labels)

    @patch("github_agent_router.router.JulesClient")
    def test_check_credentials(self, mock_jules_cls):
        mock_jules = mock_jules_cls.return_value
        mock_jules.list_sources.return_value = [
            {"githubRepo": {"owner": "owner", "repo": "repo"}}
        ]
        code = check_credentials(cfg())
        self.assertEqual(code, 0)

    def test_check_credentials_fails_without_github_token(self):
        self.assertEqual(
            check_credentials(cfg(github_token="", jules_keys={"a": "", "b": ""})),
            1,
        )

    def test_generate_workflow_content(self):
        from github_agent_router.router import generate_workflow_content
        content = generate_workflow_content(home="b", overflow="a", max_rounds=3, auto_review_prs=True)
        self.assertIn("home: b", content)
        self.assertIn('overflow: "a"', content)
        self.assertIn("max_rounds: 3", content)
        self.assertIn("auto_review_prs: true", content)

    def test_generate_workflow_includes_visibility_owner_inputs(self):
        from github_agent_router.router import generate_workflow_content

        content = generate_workflow_content()

        self.assertIn("private_home: a", content)
        self.assertIn("public_home: b", content)

    def test_generate_workflow_serializes_repository_runs(self):
        from github_agent_router.router import generate_workflow_content

        content = generate_workflow_content()

        self.assertIn("concurrency:", content)
        self.assertIn("cancel-in-progress: false", content)

    def test_generate_workflow_defaults_to_visibility_routing(self):
        from github_agent_router.router import generate_workflow_content

        content = generate_workflow_content()

        self.assertIn("home: auto", content)
        self.assertIn('overflow: "auto"', content)

    @patch("github_agent_router.github.GitHubClient._request")
    def test_provision_repo(self, mock_request):
        from github_agent_router.router import provision_repo
        mock_request.return_value = {}
        provision_repo(cfg(), "target/repo", home="a", overflow="b")
        # Ensure requests were made for setup labels and put_file
        self.assertTrue(mock_request.called)

    @patch("github_agent_router.router.git_remote_slug")
    def test_scan_workspace_repos_assigns_home_policy(self, mock_slug):
        import tempfile
        from github_agent_router.router import scan_workspace_repos

        with tempfile.TemporaryDirectory() as tmp:
            repos_dir = os.path.join(tmp, "repos")
            chrome_dir = os.path.join(tmp, "chrome")
            for r in (repos_dir, chrome_dir):
                repo_git = os.path.join(r, "my-project", ".git")
                os.makedirs(repo_git)

            mock_slug.side_effect = lambda path: "johnyoonh/" + os.path.basename(os.path.dirname(path))

            results = scan_workspace_repos([repos_dir, chrome_dir], owner_filter="johnyoonh")
            self.assertEqual(len(results), 2)
            repos_item = next(x for x in results if x["root"] == "repos")
            chrome_item = next(x for x in results if x["root"] == "chrome")
            self.assertEqual(repos_item["home"], "a")
            self.assertEqual(repos_item["overflow"], "b")
            self.assertEqual(chrome_item["home"], "b")
            self.assertEqual(chrome_item["overflow"], "a")


if __name__ == "__main__":
    unittest.main()

