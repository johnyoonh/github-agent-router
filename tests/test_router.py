import unittest

from github_agent_router.config import Config
from github_agent_router.router import (
    RouteState,
    choose_owner,
    owner_from_labels,
    parse_route_state,
    should_route_pr,
)


def cfg(**kwargs):
    base = dict(
        github_token="x",
        jules_keys={"a": "A", "b": "B"},
        home="a",
        overflow="b",
        max_rounds=2,
        auto_review_prs=False,
        dry_run=False,
    )
    base.update(kwargs)
    return Config(**base)


class RouterTests(unittest.TestCase):
    def test_sticky_owner_beats_home(self):
        self.assertEqual(choose_owner({"jules-owner:b"}, cfg(home="a")), "b")

    def test_home_used_for_unclaimed_work(self):
        self.assertEqual(choose_owner(set(), cfg(home="a")), "a")

    def test_missing_home_key_uses_overflow(self):
        self.assertEqual(choose_owner(set(), cfg(jules_keys={"a": "", "b": "B"})), "b")

    def test_owner_labels(self):
        self.assertEqual(owner_from_labels({"bug", "jules-owner:a"}), "a")
        self.assertIsNone(owner_from_labels({"bug"}))

    def test_marker_round_trip(self):
        state = RouteState("a", "sessions/123", "https://jules.google/x", 2, "IN_PROGRESS")
        parsed, comment_id = parse_route_state([{"id": 9, "body": state.comment()}])
        self.assertEqual(comment_id, 9)
        self.assertEqual(parsed, state)

    def test_pr_requires_label_by_default(self):
        pr = {"labels": [], "head": {"ref": "chatgpt/feature"}}
        self.assertFalse(should_route_pr(pr, cfg()))

    def test_pr_auto_review_can_be_enabled(self):
        pr = {"labels": [], "head": {"ref": "chatgpt/feature"}}
        self.assertTrue(should_route_pr(pr, cfg(auto_review_prs=True)))

    def test_jules_pr_is_not_re_reviewed(self):
        pr = {"labels": [{"name": "agent:jules"}], "head": {"ref": "jules/fix"}}
        self.assertFalse(should_route_pr(pr, cfg(auto_review_prs=True)))


if __name__ == "__main__":
    unittest.main()
