"""HTTP boundary checks use synthetic responses and never reach the network."""
import base64
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from github_agent_router.github import GitHubClient, GitHubError, REQUIRED_LABELS
from github_agent_router.jules import JulesClient, JulesError


class ClientTests(unittest.TestCase):
    def test_dry_run_rejects_every_github_mutation_before_network(self):
        client = GitHubClient("synthetic", "example/project", dry_run=True)
        with patch("urllib.request.urlopen") as network:
            for method in ("POST", "PATCH", "PUT", "DELETE"):
                with self.subTest(method=method), self.assertRaisesRegex(ValueError, "dry-run"):
                    client._request(method, "/repos/example/project/issues", {})
            network.assert_not_called()

    def test_github_errors_keep_status_without_echoing_body(self):
        response = HTTPError("https://api.github.com", 403, "Forbidden", {}, io.BytesIO(b"private synthetic response"))
        with patch("urllib.request.urlopen", side_effect=response):
            with self.assertRaises(GitHubError) as caught:
                GitHubClient("synthetic", "example/project").get_repo()
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn("private", str(caught.exception))

    def test_jules_errors_do_not_echo_body(self):
        response = HTTPError("https://jules.googleapis.com", 401, "Unauthorized", {}, io.BytesIO(b"private synthetic response"))
        with patch("urllib.request.urlopen", side_effect=response):
            with self.assertRaises(JulesError) as caught:
                JulesClient("synthetic").list_sources()
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("private", str(caught.exception))

    def test_non_404_error_containing_404_never_creates_label(self):
        client = GitHubClient("synthetic", "example/project")
        with patch.object(client, "_request", side_effect=GitHubError("request 404 in diagnostic", 403)) as request:
            with self.assertRaises(GitHubError):
                client.ensure_label("jules:run")
        request.assert_called_once()

    def test_label_creation_race_is_verified_and_cached(self):
        client = GitHubClient("synthetic", "example/project")
        name, color, description = REQUIRED_LABELS[0]
        observed = {"name": name, "color": color, "description": description}
        with patch.object(client, "_request", side_effect=[GitHubError("missing", 404), GitHubError("already exists", 422), observed]) as request:
            client.ensure_label(name)
            client.ensure_label(name)
        self.assertEqual([c.args[0] for c in request.call_args_list], ["GET", "POST", "GET"])

    def test_422_without_a_verified_label_is_not_success(self):
        client = GitHubClient("synthetic", "example/project")
        with patch.object(client, "_request", side_effect=[GitHubError("missing", 404), GitHubError("invalid", 422), GitHubError("still missing", 404)]):
            with self.assertRaises(GitHubError):
                client.ensure_label("jules:run")
        self.assertNotIn("jules:run", client._labels)

    def test_identical_file_has_no_write(self):
        client = GitHubClient("synthetic", "example/project")
        with patch.object(client, "_request", return_value={"content": base64.b64encode(b"same").decode(), "sha": "old"}) as request:
            self.assertTrue(client.put_file("example.yml", "same", "unused", branch="chatgpt/test")["unchanged"])
        self.assertEqual([c.args[0] for c in request.call_args_list], ["GET"])

    def test_permission_errors_fail_closed(self):
        client = GitHubClient("synthetic", "example/project")
        for permission in ("read", "none", "triage", ""):
            with self.subTest(permission=permission), patch.object(client, "_request", return_value={"permission": permission}):
                self.assertFalse(client.can_write("reader"))
        with patch.object(client, "_request", side_effect=GitHubError("missing", 404)):
            self.assertFalse(client.can_write("unknown"))
        with patch.object(client, "_request", side_effect=GitHubError("forbidden", 403)):
            with self.assertRaises(GitHubError):
                client.can_write("reader")

    def test_session_pagination_refuses_a_repeated_cursor(self):
        client = JulesClient("synthetic")
        with patch.object(client, "_request", return_value={"sessions": [], "nextPageToken": "repeat"}):
            with self.assertRaisesRegex(ValueError, "pagination"):
                client.list_sessions()


if __name__ == "__main__":
    unittest.main()
