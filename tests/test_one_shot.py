import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from github_agent_router.certification import probe, read_state, parse_verdict, event_pr


class GitHub:
    repository = "a/b"
    def __init__(self):
        self.pr = {"number": 7, "state": "open", "draft": False, "labels": [],
                   "head": {"ref": "chatgpt/test", "sha": "a" * 40, "repo": {"full_name": "a/b"}},
                   "base": {"sha": "b" * 40}, "body": "test", "title": "Test"}
        self.records, self.statuses = [], []
        self.handoffs = 0
    def can_write(self, login): return login in {"human", "trusted-app[bot]"}
    def _request(self, method, path, payload=None):
        if "/pulls/" in path: return copy.deepcopy(self.pr)
        if "/statuses/" in path: self.statuses.append(payload); return {}
        if path == "/repos/a/b": return {"private": True}
        raise AssertionError(path)
    def comments(self, number): return copy.deepcopy(self.records)
    def add_comment(self, number, body):
        value = {"id": 10, "body": body, "user": {"login": "github-actions[bot]", "type": "Bot"}}
        self.records.append(value)
        return value
    def update_comment(self, number, body): self.records[0]["body"] = body
    def add_labels(self, number, labels):
        existing = {x["name"] for x in self.pr["labels"]}
        self.pr["labels"].extend({"name": x} for x in labels if x not in existing)
    def remove_label(self, number, label): self.pr["labels"] = [x for x in self.pr["labels"] if x["name"] != label]
    def ensure_handoff_issue(self, *args): self.handoffs += 1; return 99


class Jules:
    def __init__(self): self.created, self.gets, self.activities, self.sessions = 0, 0, [], {}
    def find_source(self, *args): return "sources/github/a/b"
    def create_session(self, **kwargs):
        self.created += 1
        name = f"sessions/{self.created}"
        value = {"name": name, "state": "IN_PROGRESS", "prompt": kwargs["prompt"],
                 "sourceContext": {"source": kwargs["source"]}}
        self.sessions[name] = value
        return value
    def get_session(self, name): self.gets += 1; return self.sessions[name]
    def list_sessions(self): return list(self.sessions.values())
    def list_activities(self, name): return self.activities
    def send_message(self, *args): pass


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.gh, self.jules = GitHub(), Jules()
        self.config = SimpleNamespace(github_token="fake", dry_run=False, serialized=True,
            jules_keys={"a": "key", "b": "other"}, home="a", private_home="a", public_home="b",
            allowed_owners=("a",), auto_review_prs=True, max_rounds=2)
        self.payload = {"repository": {"full_name": "a/b"}, "sender": {"login": "human"},
                        "pull_request": {"number": 7}, "action": "opened"}
    def run_probe(self, event="pull_request", payload=None, now=1000):
        return probe(self.config, event, payload or self.payload, gh=self.gh,
                     jules_factory=lambda key: self.jules, now=now)
    def certify(self, head=None, base=None):
        self.jules.sessions[f"sessions/{self.jules.created}"]["state"] = "COMPLETED"
        value = {"verdict": "certified", "summary": "verified", "head_sha": head or "a" * 40,
                 "base_sha": base or "b" * 40, "merge_tree_sha": "c" * 40,
                 "tests": ["tests passed"], "red_team": ["race checked"], "requests": []}
        self.jules.activities = [{"createTime": "2026-10-15T12:00:00Z", "agentMessaged": {"agentMessage":
            "<!-- github-agent-router:verification:" + json.dumps(value) + " -->"}}]
    def test_one_shot_pending_not_green(self):
        result = self.run_probe()
        self.assertEqual(result["execution"], "success")
        self.assertEqual(result["certification"], "pending")
        self.assertFalse(result["merge_ready"])
        self.assertEqual(self.gh.statuses[-1]["state"], "pending")
        self.assertEqual(self.jules.created, 1)
        self.assertEqual(self.jules.gets, 0)
    def test_signed_envelope_has_exact_hmac_bytes_and_legacy_session_display(self):
        import base64, hashlib, hmac, re
        self.run_probe(now=1000.0)
        body = self.gh.records[0]["body"]
        envelope = json.loads(re.search(r"<!-- github-agent-router:envelope:(.*) -->", body).group(1))
        data = base64.b64decode(envelope["payload"])
        key = hmac.new(b"key", b"github-agent-router/state/v1", hashlib.sha256).digest()
        self.assertEqual(hmac.new(key, data, hashlib.sha256).hexdigest(), envelope["signature"])
        self.assertEqual(json.loads(data)["session"], "sessions/1")
        self.assertIn("Session: `sessions/1`", body)
        self.assertIn({"name": "jules:run"}, self.gh.pr["labels"])
        self.assertIn({"name": "agent:chatgpt"}, self.gh.pr["labels"])

    def test_current_revision_certifies(self):
        self.run_probe(); self.certify()
        result = self.run_probe(now=1100)
        self.assertEqual(result["certification"], "certified")
        self.assertEqual(self.gh.statuses[-1]["state"], "success")
        state = read_state(self.gh, 7, self.config.jules_keys)
        self.assertEqual(state["head_sha"], "a" * 40)
    def test_base_changed_needs_new_sticky_round(self):
        self.run_probe(); self.certify()
        self.gh.pr["base"]["sha"] = "d" * 40
        result = self.run_probe(now=1100)
        self.assertEqual(result["certification"], "pending")
        self.assertEqual(self.jules.created, 2)
    def test_active_old_revision_not_duplicated(self):
        self.run_probe()
        self.gh.pr["head"]["sha"] = "e" * 40
        self.assertEqual(self.run_probe(now=1100)["certification"], "pending")
        self.assertEqual(self.jules.created, 1)
    def test_dispatch_uses_actual_actor_not_repo_owner(self):
        self.run_probe()
        payload = {"repository": {"full_name": "a/b"}, "sender": {"login": "intruder"},
                   "action": "jules-cert-sweep", "client_payload": {"pr_number": 7, "owner": "human"}}
        with self.assertRaises(ValueError): self.run_probe("repository_dispatch", payload)
    def test_dispatch_payload_injection_rejected(self):
        with self.assertRaises(ValueError):
            event_pr("repository_dispatch", {"action": "jules-cert-sweep", "client_payload": {"pr_number": "7'; exit 0"}})
    def test_backoff_prevents_repeat_api_poll(self):
        self.run_probe()
        payload = {"repository": {"full_name": "a/b"}, "sender": {"login": "human"},
                   "action": "jules-cert-sweep", "client_payload": {"pr_number": 7}}
        self.assertGreater(self.run_probe("repository_dispatch", payload, now=1001)["retry_after"], 0)
        self.assertEqual(self.jules.gets, 0)
    def test_no_mutation_without_serialization(self):
        self.config.serialized = False
        with self.assertRaises(ValueError): self.run_probe()
        self.assertEqual(self.jules.created, 0)
        self.assertEqual(self.gh.records, [])
    def test_uncertain_creation_recovers_no_duplicate(self):
        original = self.jules.create_session
        def timeout(**kwargs): original(**kwargs); raise TimeoutError("unknown outcome")
        self.jules.create_session = timeout
        with self.assertRaises(TimeoutError): self.run_probe()
        self.jules.create_session = original
        self.run_probe(now=1100)
        self.assertEqual(self.jules.created, 1)
    def test_uncertain_creation_zero_matches_never_retries(self):
        self.jules.create_session = lambda **kwargs: (_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(TimeoutError): self.run_probe()
        with self.assertRaises(ValueError): self.run_probe(now=1100)
        self.assertEqual(self.jules.created, 0)
    def test_newest_message_must_have_exactly_one_marker(self):
        self.run_probe(); self.certify()
        self.jules.activities.append({"createTime": "2026-10-15T12:01:00Z", "agentMessaged": {"agentMessage": "Actually, unresolved failure"}})
        with self.assertRaises(ValueError): parse_verdict(self.jules.activities, "a" * 40, "b" * 40)
    def test_activity_order_cannot_replay_an_old_certificate(self):
        self.run_probe(); self.certify()
        self.jules.activities.insert(0, {"createTime": "2026-10-15T12:01:00Z",
            "agentMessaged": {"agentMessage": "Newer blocking defect"}})
        with self.assertRaises(ValueError): parse_verdict(self.jules.activities, "a" * 40, "b" * 40)

    def test_missing_provider_timestamp_cannot_certify(self):
        self.run_probe(); self.certify()
        self.jules.activities[0].pop("createTime")
        with self.assertRaises(ValueError): parse_verdict(self.jules.activities, "a" * 40, "b" * 40)

    def test_tampered_state_never_starts_replacement(self):
        self.run_probe()
        self.gh.records[0]["body"] = self.gh.records[0]["body"].replace("IN_PROGRESS", "COMPLETED")
        with self.assertRaises(ValueError): self.run_probe(now=1100)
        self.assertEqual(self.jules.created, 1)

    def followup(self):
        return {"repository": {"full_name": "a/b"}, "sender": {"login": "human"},
                "issue": {"number": 7, "pull_request": {}},
                "comment": {"id": 44, "body": "/jules use the new evidence",
                            "user": {"login": "human"}}}
    def test_followup_is_sent_once_to_active_session(self):
        self.run_probe()
        sent = []
        self.jules.send_message = lambda *args: sent.append(args)
        payload = self.followup()
        payload["issue"]["pull_request"] = {"url": "https://api.github.com/repos/a/b/pulls/7"}
        self.run_probe("issue_comment", payload, now=1100)
        self.run_probe("issue_comment", payload, now=1101)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.jules.created, 1)
    def test_uncertain_followup_never_retries_send(self):
        self.run_probe()
        self.jules.send_message = lambda *args: (_ for _ in ()).throw(TimeoutError())
        payload = self.followup()
        payload["issue"]["pull_request"] = {"url": "https://api.github.com/repos/a/b/pulls/7"}
        with self.assertRaises(TimeoutError): self.run_probe("issue_comment", payload, now=1100)
        with self.assertRaises(ValueError): self.run_probe("issue_comment", payload, now=1101)
        self.assertEqual(self.jules.created, 1)


if __name__ == "__main__": unittest.main()
