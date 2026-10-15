from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from github_agent_router.delivery import (canonical, DOMAIN, evaluate, validate_receipt,
    validate_exception, merge_native, check_state, APPROVAL_PREFIX)

NOW = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)


def fixture():
    pr = {"repository": "a/b", "pr": 7, "head_sha": "a" * 40, "base_sha": "b" * 40,
          "merge_tree_sha": "c" * 40, "open": True, "draft": False, "same_repository": True,
          "mergeable": True, "protection_known": True, "reviews_satisfied": True,
          "protected": True, "strict_up_to_date": True, "bypass_capable": False,
          "required_checks": [{"name": "gate", "state": "success"}]}
    cert = {k: pr[k] for k in ("repository", "pr", "head_sha", "base_sha", "merge_tree_sha")}
    cert["verdict"] = "certified"
    receipt = {**cert, "schema_version": 1, "status": "passed", "repair_shas": [],
               "checks": [{"argv": ["test"], "returncode": 0}], "completed_at": NOW.isoformat()}
    receipt.pop("verdict")
    receipt["signature"] = hmac.new(b"key", DOMAIN + canonical(receipt), hashlib.sha256).hexdigest()
    return pr, cert, receipt


class DecisionTests(unittest.TestCase):
    def decide(self, pr, cert, receipt, **kwargs):
        return evaluate(pr, cert, receipt, evidence_key="key", now=NOW, **kwargs)

    def test_ready(self):
        self.assertEqual(self.decide(*fixture())["status"], "ready")

    def test_never_trust_label_without_certificate(self):
        pr, cert, receipt = fixture()
        self.assertEqual(self.decide(pr, {}, receipt)["status"], "blocked")

    def test_head_base_and_tree_must_match(self):
        for key in ("head_sha", "base_sha", "merge_tree_sha"):
            pr, cert, receipt = fixture()
            cert[key] = "f" * 40
            self.assertEqual(self.decide(pr, cert, receipt)["status"], "blocked")

    def test_pending_missing_failed_and_unknown_do_not_pass(self):
        for state in ("pending", "missing", "failure", "cancelled", "skipped", "neutral"):
            pr, cert, receipt = fixture()
            pr["required_checks"][0]["state"] = state
            self.assertEqual(self.decide(pr, cert, receipt)["status"], "blocked")

    def test_admin_identity_requires_operator_not_silent_bypass(self):
        pr, cert, receipt = fixture()
        pr["bypass_capable"] = True
        self.assertEqual(self.decide(pr, cert, receipt)["status"], "human_required")

    def test_uncertain_protection_and_reviews_block(self):
        for key in ("protection_known", "reviews_satisfied", "mergeable", "strict_up_to_date"):
            pr, cert, receipt = fixture()
            pr[key] = False
            self.assertEqual(self.decide(pr, cert, receipt)["status"], "blocked")

    def test_unsigned_or_tampered_receipt_blocks(self):
        for mutation in (lambda x: x.pop("signature"), lambda x: x.update(status="failed")):
            pr, cert, receipt = fixture()
            mutation(receipt)
            self.assertEqual(self.decide(pr, cert, receipt)["status"], "blocked")

    def test_stale_receipt_blocks(self):
        pr, cert, receipt = fixture()
        receipt.pop("signature")
        receipt["completed_at"] = (NOW - timedelta(hours=2)).isoformat()
        receipt["signature"] = hmac.new(b"key", DOMAIN + canonical(receipt), hashlib.sha256).hexdigest()
        self.assertEqual(self.decide(pr, cert, receipt)["status"], "blocked")

    def approval(self, pr, cert, receipt):
        pr["required_checks"][0]["state"] = "infrastructure_unavailable"
        result = self.decide(pr, cert, receipt)
        grant = {k: pr[k] for k in ("repository", "pr", "head_sha", "base_sha", "merge_tree_sha")}
        grant.update(waive_checks=["gate"], expires_at=(NOW + timedelta(minutes=5)).isoformat(),
                     reason="Verified runner billing outage", receipt_sha256=hashlib.sha256(canonical(receipt)).hexdigest())
        return {"id": 42, "user": {"login": "human", "type": "User"},
                "body": APPROVAL_PREFIX + json.dumps(grant),
                "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()}

    def test_exact_human_outage_exception(self):
        pr, cert, receipt = fixture()
        comment = self.approval(pr, cert, receipt)
        self.assertEqual(self.decide(pr, cert, receipt, approval=comment, approvers=["human"])["status"], "exception_authorized")

    def test_bot_cannot_approve(self):
        pr, cert, receipt = fixture()
        comment = self.approval(pr, cert, receipt)
        comment["user"]["type"] = "Bot"
        self.assertEqual(self.decide(pr, cert, receipt, approval=comment, approvers=["human"])["status"], "human_required")

    def test_human_exception_cannot_waive_code_failure(self):
        pr, cert, receipt = fixture()
        comment = self.approval(pr, cert, receipt)
        pr["required_checks"][0]["state"] = "failure"
        self.assertEqual(self.decide(pr, cert, receipt, approval=comment, approvers=["human"])["status"], "blocked")

    def test_edited_or_wrong_revision_approval_rejected(self):
        pr, cert, receipt = fixture()
        comment = self.approval(pr, cert, receipt)
        comment["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertEqual(self.decide(pr, cert, receipt, approval=comment, approvers=["human"])["status"], "human_required")

    def test_http_error_has_no_alternate_write_path(self):
        calls = []
        class API:
            def _request(self, *args):
                calls.append(args)
                raise RuntimeError("404")
        decision = self.decide(*fixture())
        with self.assertRaises(RuntimeError): merge_native(API(), decision, refresh=lambda: decision)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith("/pulls/7/merge"))
        self.assertEqual(calls[0][2]["sha"], "a" * 40)

    def test_base_moved_before_merge(self):
        decision = self.decide(*fixture())
        with self.assertRaises(ValueError):
            merge_native(None, decision, refresh=lambda: {**decision, "base_sha": "f" * 40})

    def test_failure_is_not_assumed_to_be_billing(self):
        check = {"status": "completed", "conclusion": "failure"}
        self.assertEqual(check_state(check, [{"message": "unit tests failed"}]), "failure")
        self.assertEqual(check_state(check, [{"message": "Actions minutes spending limit exceeded"}]), "failure")
        check["app"] = {"slug": "github-actions"}
        self.assertEqual(check_state(check, [{"message": "Actions minutes spending limit exceeded"}],
                                     {"runner_id": 0, "steps": []}), "infrastructure_unavailable")


if __name__ == "__main__": unittest.main()
