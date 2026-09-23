import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from github_agent_router.config import Config
from github_agent_router.github import GitHubClient
from github_agent_router.router import (
    AUTONOMOUS_UNBLOCK_PROMPT,
    RouteState,
    build_prompt,
    extract_chatgpt_origin,
    extract_verification,
    route,
    should_route_pr,
    watch_event,
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
        serialized=True,
    )
    base.update(kwargs)
    return Config(**base)


def marker(verdict="certified", **overrides):
    value = {
        "verdict": verdict,
        "summary": "observed behavior matches the requested contract",
        "tests": ["python -m unittest: pass"],
        "red_team": ["malformed input rejected without mutation"],
        "requests": [],
    }
    value.update(overrides)
    return "<!-- github-agent-router:verification:" + json.dumps(value, separators=(",", ":")) + " -->"


def signed_comment(state, number=5):
    state.repository = "owner/repo"
    state.number = number
    state.comment_id = 10
    return {
        "id": 10,
        "body": state.comment(key="key-a"),
        "user": {"login": "github-actions[bot]", "type": "Bot"},
    }


def pr_payload(body="PR body"):
    return {
        "action": "synchronize",
        "sender": {"login": "maintainer"},
        "repository": {"full_name": "owner/repo", "default_branch": "main"},
        "pull_request": {
            "number": 5,
            "title": "Implement behavior",
            "body": body,
            "html_url": "https://github.com/owner/repo/pull/5",
            "head": {
                "ref": "chatgpt/implement-behavior",
                "sha": "a" * 40,
                "repo": {"full_name": "owner/repo"},
            },
            "labels": [],
        },
    }


class CertificationContractTests(unittest.TestCase):
    def test_chatgpt_branch_routes_without_manual_label(self):
        pr = pr_payload()["pull_request"]
        self.assertTrue(should_route_pr(pr, cfg()))
        _, prompt, _, _, _ = build_prompt(pr_payload())
        self.assertIn("adversarial verification gate", prompt)
        self.assertIn("head " + "a" * 40, prompt)
        self.assertIn("github-agent-router:verification", prompt)

    def test_extract_certified_verdict_requires_test_and_red_team_evidence(self):
        activities = [{"agentMessaged": {"agentMessage": "done\n" + marker()}}]
        result = extract_verification(activities)
        self.assertEqual(result["verdict"], "certified")
        with self.assertRaisesRegex(ValueError, "behavioral test and red-team"):
            extract_verification([{"agentMessaged": {"agentMessage": marker(red_team=[])}}])

    def test_needs_evidence_requires_bounded_request(self):
        with self.assertRaisesRegex(ValueError, "requires an explicit request"):
            extract_verification([{
                "agentMessaged": {
                    "agentMessage": marker(
                        "needs_evidence",
                        tests=[],
                        red_team=[],
                        requests=[],
                    )
                }
            }])

    def test_chatgpt_origin_accepts_only_canonical_conversation(self):
        body = '<!-- chatgpt-opencli:origin:{"conversation":"https://chatgpt.com/c/abc","project":"repo","agents_sha":"1234"} -->'
        self.assertEqual(extract_chatgpt_origin(body)["conversation"], "https://chatgpt.com/c/abc")
        bad = '<!-- chatgpt-opencli:origin:{"conversation":"https://evil.example/c/abc"} -->'
        self.assertNotIn("conversation", extract_chatgpt_origin(bad))

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_completed_certified_pr_sets_certification_without_handoff(self, jules_cls, gh_cls):
        gh = gh_cls.return_value
        gh.can_write.return_value = True
        state = RouteState("a", "sessions/s1", round=1, state="IN_PROGRESS")
        gh.comments.return_value = [signed_comment(state)]
        jules = jules_cls.return_value
        jules.get_session.return_value = {"state": "COMPLETED", "url": "https://jules.google/s1"}
        jules.list_activities.return_value = [{"agentMessaged": {"agentMessage": marker()}}]

        result = route(cfg(), "pull_request", pr_payload())

        self.assertIn("certified by Jules", result)
        gh.ensure_handoff_issue.assert_not_called()
        gh.add_labels.assert_any_call(5, ["agent:chatgpt", "jules:run"])
        gh.add_labels.assert_any_call(5, ["jules:certified"])

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_needs_evidence_creates_deduplicated_handoff_state(self, jules_cls, gh_cls):
        gh = gh_cls.return_value
        gh.can_write.return_value = True
        gh.ensure_handoff_issue.return_value = 88
        state = RouteState("a", "sessions/s1", round=1, state="IN_PROGRESS")
        gh.comments.return_value = [signed_comment(state)]
        jules = jules_cls.return_value
        jules.get_session.return_value = {"state": "COMPLETED", "url": "https://jules.google/s1"}
        jules.list_activities.return_value = [{
            "agentMessaged": {
                "agentMessage": marker(
                    "needs_evidence",
                    summary="need one local daemon trace",
                    tests=[],
                    red_team=[],
                    requests=["run bounded daemon health check and return exit/status only"],
                )
            }
        }]

        result = route(cfg(), "pull_request", pr_payload())

        self.assertEqual(result, "needs local evidence; handoff issue #88")
        gh.ensure_handoff_issue.assert_called_once()
        gh.add_labels.assert_any_call(5, ["agent:blocked"])
        gh.add_labels.assert_any_call(5, ["chatgpt:handoff"])

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_waiting_feedback_gets_one_autonomous_unblock_nudge(self, jules_cls, gh_cls):
        gh = gh_cls.return_value
        gh.can_write.return_value = True
        state = RouteState("a", "sessions/s1", round=1, state="IN_PROGRESS")
        gh.comments.return_value = [signed_comment(state)]
        jules = jules_cls.return_value
        jules.get_session.return_value = {"state": "AWAITING_USER_FEEDBACK"}

        result = route(cfg(), "pull_request", pr_payload())

        self.assertIn("nudged sessions/s1", result)
        jules.send_message.assert_called_once_with("sessions/s1", AUTONOMOUS_UNBLOCK_PROMPT)
        gh.ensure_handoff_issue.assert_not_called()

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_second_wait_surfaces_material_user_block(self, jules_cls, gh_cls):
        gh = gh_cls.return_value
        gh.can_write.return_value = True
        gh.ensure_handoff_issue.return_value = 91
        state = RouteState(
            "a", "sessions/s1", round=1, state="IN_PROGRESS", feedback_nudges=1
        )
        gh.comments.return_value = [signed_comment(state)]
        jules = jules_cls.return_value
        jules.get_session.return_value = {"state": "AWAITING_USER_FEEDBACK"}
        jules.list_activities.return_value = []

        result = route(cfg(), "pull_request", pr_payload())

        self.assertEqual(result, "needs user; surfaced in handoff issue #91")
        gh.add_labels.assert_any_call(5, ["jules:needs-user"])

    @patch("github_agent_router.router.GitHubClient")
    @patch("github_agent_router.router.JulesClient")
    def test_terminal_needs_evidence_comment_starts_new_sticky_round(self, jules_cls, gh_cls):
        gh = gh_cls.return_value
        gh.can_write.return_value = True
        gh.add_comment.return_value = {"id": 11}
        state = RouteState(
            "a",
            "sessions/s1",
            round=1,
            state="COMPLETED",
            branch="chatgpt/implement-behavior",
            verification="needs_evidence",
            handoff_issue=88,
        )
        gh.comments.return_value = [signed_comment(state)]
        jules = jules_cls.return_value
        jules.get_session.return_value = {"state": "COMPLETED"}
        jules.find_source.return_value = "sources/owner/repo"
        jules.create_session.return_value = {
            "name": "sessions/s2",
            "state": "IN_PROGRESS",
            "url": "https://jules.google/s2",
        }
        payload = {
            "action": "created",
            "sender": {"login": "maintainer"},
            "repository": {"full_name": "owner/repo", "default_branch": "main"},
            "issue": {
                "number": 5,
                "title": "Implement behavior",
                "body": "PR body",
                "labels": [
                    {"name": "jules:run"},
                    {"name": "jules-owner:a"},
                    {"name": "chatgpt:handoff"},
                ],
            },
            "comment": {
                "body": "/jules Local daemon trace is clean; exit 0 and expected socket is listening.",
                "user": {"login": "maintainer"},
            },
        }

        result = route(cfg(), "issue_comment", payload)

        self.assertIn("created sticky follow-up sessions/s2", result)
        jules.send_message.assert_not_called()
        kwargs = jules.create_session.call_args.kwargs
        self.assertEqual(kwargs["branch"], "chatgpt/implement-behavior")
        self.assertIn("Local daemon trace is clean", kwargs["prompt"])
        self.assertIn("required github-agent-router verification marker", kwargs["prompt"])

    def test_handoff_issue_reuses_existing_marker(self):
        gh = GitHubClient("token", "owner/repo")
        marker_text = "<!-- github-agent-router:handoff:v1 owner/repo#5 -->"
        gh.ensure_label = MagicMock()
        gh.paginated = MagicMock(return_value=[{"number": 17, "body": marker_text}])
        gh._request = MagicMock(return_value={})
        gh.add_labels = MagicMock()

        number = gh.ensure_handoff_issue(marker_text, "title", marker_text + "\nbody")

        self.assertEqual(number, 17)
        gh._request.assert_called_once_with(
            "PATCH",
            "/repos/owner/repo/issues/17",
            {"title": "title", "body": marker_text + "\nbody"},
        )

    @patch("github_agent_router.router.time.sleep")
    @patch("github_agent_router.router.route")
    def test_watch_accepts_jules_comment_on_pr(self, routed, sleep):
        payload = {
            "action": "created",
            "sender": {"login": "maintainer"},
            "repository": {"full_name": "owner/repo", "default_branch": "main"},
            "issue": {
                "number": 5,
                "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/5"},
                "labels": [{"name": "jules-owner:a"}],
            },
            "comment": {
                "body": "/jules local evidence attached",
                "user": {"login": "maintainer"},
            },
        }
        routed.return_value = "certified by Jules: evidence accepted"

        result = watch_event(cfg(), "issue_comment", payload, timeout=1, interval=0.1)

        self.assertIn("certified by Jules", result)
        routed.assert_called_once()
        sleep.assert_not_called()

    @patch("github_agent_router.router.time.sleep")
    @patch("github_agent_router.router.route")
    def test_watch_waits_until_certified(self, routed, sleep):
        routed.side_effect = [
            "pending Jules verification: IN_PROGRESS",
            "certified by Jules: all checks pass",
        ]

        result = watch_event(cfg(), "pull_request", pr_payload(), timeout=5, interval=0.1)

        self.assertIn("certified by Jules", result)
        self.assertEqual(routed.call_count, 2)
        sleep.assert_called_once()


class SatisfactionMatrixTests(unittest.TestCase):
    def test_machine_readable_e2e_matrix_has_evidence_for_every_scenario(self):
        path = Path(__file__).with_name("e2e_satisfaction.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        ids = set()
        for scenario in payload["scenarios"]:
            self.assertTrue(scenario["id"])
            self.assertNotIn(scenario["id"], ids)
            ids.add(scenario["id"])
            self.assertGreaterEqual(len(scenario["expected"]), 2)
            self.assertGreaterEqual(len(scenario["required_evidence"]), 1)
            self.assertIsInstance(scenario["live_required"], bool)


if __name__ == "__main__":
    unittest.main()
