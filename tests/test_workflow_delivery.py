from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from github_agent_router.workflow import render_workflow


class WorkflowTests(unittest.TestCase):
    def test_renderer_pins_one_shot_runtime_and_filters_comments(self):
        policy = SimpleNamespace(private=True, repository="a/b", allowed_owners=("a",),
                                 home="a", overflow="", max_rounds=2, auto_review_prs=True)
        text = render_workflow(policy, "a" * 40)
        self.assertIn("@" + "a" * 40, text)
        self.assertIn("router_ref: " + "a" * 40, text)
        self.assertIn("jules-cert-sweep", text)
        self.assertIn("startsWith(github.event.comment.body, '/jules')", text)
        self.assertNotIn("verification_timeout:", text)
        self.assertNotIn("JULES_B_API_KEY", text)
        self.assertIn("statuses: write", text)
    def test_mutations_share_non_cancelling_queue(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/reusable-router.yml").read_text()
        self.assertIn("github.event.client_payload.pr_number", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("queue: max", text)
        self.assertIn("run: github-agent-router probe", text)
        self.assertNotIn("run: github-agent-router watch", text)
        self.assertNotIn("sleep", text.split("jobs:")[1])
        self.assertNotIn('"sender": {"login": owner}', text)


if __name__ == "__main__": unittest.main()
