from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import base64
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from github_agent_router.cli import main
from github_agent_router.config import Config
from github_agent_router.github import GitHubClient, GitHubError, REQUIRED_LABELS
from github_agent_router.inventory import inventory_targets, read_manifest, read_policy, repository_name, resolve_policy
from github_agent_router.provisioning import MANAGED, PR_MARKER, WORKFLOW_PATH, provision_repository, render_workflow

CONFIG = Config("synthetic", {"a": "A", "b": "B"})
INFO = {"id": 7, "full_name": "example/project", "private": True, "default_branch": "trunk", "archived": False}
POLICY = {"schema_version": 1, "defaults": {}, "repositories": {}, "additional_repositories": []}
SHA = "a" * 40


class InventoryTests(unittest.TestCase):
    def test_remote_forms_normalize_and_deduplicate(self):
        forms = ["git@github.com:Example/Project.git", "https://github.com/example/project", "ssh://git@github.com/example/project.git", "example/project"]
        self.assertEqual({repository_name(x) for x in forms}, {"example/project"})

    def test_foreign_or_credential_bearing_remotes_rejected(self):
        for value in ("https://evil.invalid/example/project", "https://secret@github.com/example/project", "git@other:example/project", "https://github.com/example/project?token=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                repository_name(value)

    def test_four_root_manifest_is_deduplicated_without_path_inference(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.tsv"
            path.write_text("# root\tpath\tremote\tbranch\trestore\tautopush\tnotes\n" + "".join(f"{root}\tclone\tgit@github.com:example/project.git\tmain\tyes\tyes\tfixture\n" for root in ("repos", "github", "chrome", "obsidian")))
            self.assertEqual(read_manifest(path), ["example/project"])

    def test_visibility_and_publication_intent_select_account(self):
        self.assertEqual(resolve_policy(INFO, POLICY).home, "a")
        self.assertEqual(resolve_policy({**INFO, "private": False}, POLICY).home, "b")
        document = {**POLICY, "repositories": {"example/project": {"intended_visibility": "to-be-public"}}}
        planned = resolve_policy(INFO, document)
        self.assertEqual((planned.home, planned.allowed_owners, planned.overflow), ("b", ("b",), ""))

    def test_identity_or_visibility_conflict_blocks(self):
        for override in ({"repository_id": 99}, {"intended_visibility": "public"}, {"home": "a", "overflow": "b", "allowed_owners": ["a"]}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                resolve_policy(INFO, {**POLICY, "repositories": {"example/project": override}})

    def test_explicit_selection_does_not_expand_to_policy_extras(self):
        document = {**POLICY, "additional_repositories": ["unselected/project"]}
        self.assertEqual(inventory_targets(["example/project"], None, document), ["example/project"])
        with self.assertRaises(ValueError):
            inventory_targets(["example/project"], "manifest.tsv", document)

    def test_unknown_or_duplicate_policy_fields_rejected(self):
        fixtures = ['{"schema_version":1,"defaults":{"typo":true}}', '{"schema_version":1,"defaults":{},"defaults":{}}', '{"schema_version":1,"repositories":{"Example/Project":{},"example/project":{}}}', '{"schema_version":true}']
        with TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            for fixture in fixtures:
                path.write_text(fixture)
                with self.subTest(fixture=fixture), self.assertRaises(ValueError):
                    read_policy(path)

    def test_render_pins_both_references_and_only_allowed_secret(self):
        result = render_workflow(resolve_policy(INFO, POLICY), SHA)
        self.assertIn("@" + SHA, result)
        self.assertIn("router_ref: " + SHA, result)
        self.assertIn("JULES_A_API_KEY", result)
        self.assertNotIn("JULES_B_API_KEY", result)
        self.assertIn("ROUTER_READ_TOKEN", result)
        self.assertIn('overflow: ""', result)
        with self.assertRaises(ValueError):
            render_workflow(resolve_policy(INFO, POLICY), "main")

    def test_public_distribution_is_not_silently_published(self):
        with self.assertRaisesRegex(ValueError, "public target"):
            render_workflow(resolve_policy({**INFO, "private": False}, POLICY), SHA)


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.info = deepcopy(INFO)
        self.branches = {"trunk": "1" * 40}
        self.files = {"trunk": {}}
        self.labels = {}
        self.prs = []
        self.calls = []
        self.behind = 0
        self.unrelated = False
        self.lose_pr_response = False
        self.secret_names = ["JULES_A_API_KEY", "ROUTER_READ_TOKEN"]
        network = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        network.start(); self.addCleanup(network.stop)
        api = patch.object(GitHubClient, "_request", autospec=True, side_effect=self.request)
        api.start(); self.addCleanup(api.stop)
        jules = patch("github_agent_router.provisioning.JulesClient")
        self.jules = jules.start(); self.addCleanup(jules.stop)
        self.jules.return_value.find_source.return_value = "sources/example/project"

    def writes(self):
        return [x for x in self.calls if x[0] != "GET"]

    def request(self, client, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        parts = urlsplit(path); query = parse_qs(parts.query)
        endpoint = unquote(parts.path).removeprefix("/repos/example/project")
        if method == "GET" and endpoint == "":
            return deepcopy(self.info)
        if endpoint == "/actions/secrets":
            return {"secrets": [{"name": x} for x in self.secret_names]}
        if endpoint.startswith("/labels/"):
            label = endpoint[len("/labels/"):]
            if method == "GET":
                if label not in self.labels:
                    raise GitHubError("not found", 404)
                return deepcopy(self.labels[label])
            if method == "PATCH":
                self.labels[label] = {"name": payload["new_name"], "color": payload["color"], "description": payload["description"]}
                return deepcopy(self.labels[label])
        if endpoint == "/labels" and method == "POST":
            self.labels[payload["name"]] = deepcopy(payload)
            return deepcopy(payload)
        if endpoint.startswith("/git/ref/heads/"):
            branch = endpoint[len("/git/ref/heads/"):]
            if branch not in self.branches:
                raise GitHubError("not found", 404)
            return {"object": {"sha": self.branches[branch]}}
        if endpoint == "/git/refs" and method == "POST":
            branch = payload["ref"].removeprefix("refs/heads/")
            self.branches[branch] = payload["sha"]
            self.files[branch] = deepcopy(self.files["trunk"])
            return {"ref": payload["ref"]}
        if endpoint.startswith("/contents/"):
            file = endpoint[len("/contents/"):]
            branch = query.get("ref", ["trunk"])[0] if method == "GET" else payload["branch"]
            if method == "GET":
                if file not in self.files[branch]:
                    raise GitHubError("not found", 404)
                raw = self.files[branch][file]
                return {"type": "file", "content": base64.b64encode(raw.encode()).decode(), "sha": hashlib.sha1(raw.encode()).hexdigest()}
            if method == "PUT":
                self.files[branch][file] = base64.b64decode(payload["content"]).decode()
                self.branches[branch] = "2" * 40
                return {"commit": {"sha": "2" * 40}}
        if endpoint.startswith("/compare/"):
            return {"behind_by": self.behind, "files": [{"filename": WORKFLOW_PATH}] + ([{"filename": "unrelated.py"}] if self.unrelated else [])}
        if endpoint == "/pulls":
            if method == "GET":
                result = self.prs
                if "head" in query:
                    result = [pr for pr in result if "example:" + pr["head"]["ref"] == query["head"][0]]
                return deepcopy(result)
            if method == "POST":
                pr = {"number": len(self.prs) + 1, "html_url": "https://github.com/example/project/pull/1", "body": payload["body"], "base": {"ref": payload["base"]}, "head": {"ref": payload["head"], "repo": {"full_name": "example/project"}}}
                self.prs.append(pr)
                if self.lose_pr_response:
                    self.lose_pr_response = False
                    raise TimeoutError("synthetic lost PR response")
                return deepcopy(pr)
        raise AssertionError(f"unexpected API request: {method} {path}")

    def provision(self, config=CONFIG, document=POLICY):
        return provision_repository(config, "example/project", document, SHA)

    def test_provision_uses_actual_default_branch_and_one_scoped_pr(self):
        result = self.provision()
        self.assertEqual(result["default_branch"], "trunk")
        self.assertTrue(result["branch"].startswith("chatgpt/"))
        self.assertEqual(self.prs[0]["base"]["ref"], "trunk")
        self.assertEqual(self.files["trunk"], {})
        self.assertEqual(set(self.labels), {x[0] for x in REQUIRED_LABELS})
        self.assertEqual(len(self.prs), 1)

    def test_second_identical_run_makes_no_writes(self):
        first = self.provision(); before = len(self.writes())
        second = self.provision()
        self.assertEqual(first["url"], second["url"])
        self.assertEqual(len(self.writes()), before)
        self.assertEqual(len(self.prs), 1)

    def test_existing_managed_pr_is_updated_not_duplicated(self):
        self.provision()
        result = self.provision(document={**POLICY, "defaults": {"max_rounds": 3}})
        self.assertIn("max_rounds: 3", self.files[result["branch"]][WORKFLOW_PATH])
        self.assertEqual(len(self.prs), 1)

    def test_pr_post_timeout_reuses_observed_result(self):
        self.lose_pr_response = True
        with self.assertRaises(TimeoutError):
            self.provision()
        result = self.provision()
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(result["status"], "pr_open")

    def test_dry_run_makes_no_mutations(self):
        self.assertEqual(self.provision(replace(CONFIG, dry_run=True))["status"], "would_open_pr")
        self.assertEqual(self.writes(), [])
        self.jules.assert_not_called()

    def test_public_target_stops_before_any_mutation(self):
        self.info["private"] = False
        with self.assertRaisesRegex(ValueError, "public target"):
            self.provision()
        self.assertEqual(self.writes(), [])

    def test_missing_selected_account_or_secret_stops_before_writes(self):
        with self.assertRaisesRegex(ValueError, "Jules A"):
            self.provision(replace(CONFIG, jules_keys={"a": "", "b": "B"}))
        self.secret_names.remove("ROUTER_READ_TOKEN")
        with self.assertRaisesRegex(ValueError, "ROUTER_READ_TOKEN"):
            self.provision()
        self.assertEqual(self.writes(), [])

    def test_human_workflow_and_unrelated_branch_changes_preserved(self):
        self.files["trunk"][WORKFLOW_PATH] = "name: Human workflow\n"
        with self.assertRaisesRegex(ValueError, "not managed"):
            self.provision()
        self.assertEqual(self.writes(), [])
        self.files["trunk"].clear()
        self.provision(); self.unrelated = True
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.provision()

    def test_stale_branch_is_not_force_updated(self):
        self.provision(); self.behind = 1
        before = deepcopy(self.files)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.provision()
        self.assertEqual(self.files, before)

    def test_disabled_repository_does_not_write(self):
        result = self.provision(document={**POLICY, "defaults": {"enabled": False}})
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.writes(), [])

    def test_label_drift_repaired_without_touching_unrelated_label(self):
        self.labels["unrelated"] = {"name": "unrelated", "color": "abcdef", "description": "human"}
        self.labels["jules:run"] = {"name": "jules:run", "color": "ffffff", "description": "old"}
        self.provision()
        self.assertEqual(self.labels["unrelated"]["description"], "human")
        self.assertEqual(self.labels["jules:run"]["color"], REQUIRED_LABELS[0][1])


class CLITests(unittest.TestCase):
    def test_batch_failure_does_not_skip_independent_repository(self):
        output = io.StringIO()
        with patch("github_agent_router.cli.inspect_repository", side_effect=[ValueError("blocked"), {"repository": "z/project", "status": "planned"}]) as inspect, redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit:
                main(["plan", "a/project", "z/project"])
        self.assertEqual(exit.exception.code, 1)
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual([x["status"] for x in json.loads(output.getvalue())["results"]], ["blocked", "planned"])


if __name__ == "__main__":
    unittest.main()
