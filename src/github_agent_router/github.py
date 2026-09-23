from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib import error, parse, request



API = "https://api.github.com"


REQUIRED_LABELS: list[tuple[str, str, str]] = [
    ("jules:run", "0e8a16", "Opt-in to autonomous Jules review and routing"),
    ("jules-owner:a", "1d76db", "Sticky Jules identity A ownership"),
    ("jules-owner:b", "5319e7", "Sticky Jules identity B ownership"),
    ("jules:needs-user", "d93f0b", "Jules is waiting for a material user decision"),
    ("jules:certified", "2da44e", "Jules behavioral and adversarial verification satisfied"),
    ("jules:changes-required", "cf222e", "Jules found a verified shortcoming that must be addressed"),
    ("agent:blocked", "d4c5f9", "Automation is blocked on evidence, permissions, or a material decision"),
    ("chatgpt:handoff", "1f6feb", "Durable handoff to ChatGPT or a local evidence collector"),
    ("agent:chatgpt", "8250df", "Trusted chatgpt/ branch routed for independent verification"),
    ("agent:jules", "fbca04", "Authored by Jules; excluded from autonomous review loops"),
]


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(self, token: str, repository: str, *, dry_run: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be owner/name")
        self.token = token
        self.repository = repository
        self.owner, self.repo = repository.split("/", 1)
        self.dry_run = dry_run
        self._labels: set[str] = set()

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
        if self.dry_run and method != "GET":
            raise ValueError("dry-run forbids GitHub writes")
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{API}{path}", data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=30) as response:
                raw = response.read()
        except error.HTTPError as exc:
            # Never include a response body that may echo credentials or private data.
            raise GitHubError(f"GitHub API {exc.code}", exc.code) from exc
        if not raw:
            return {}
        return json.loads(raw)

    def get_repo(self) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.owner}/{self.repo}")

    def is_private(self) -> bool:
        value = self.get_repo().get("private")
        if not isinstance(value, bool):
            raise ValueError("repository visibility is unavailable")
        return value

    def paginated(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 10001):
            batch = self._request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise GitHubError("expected a paginated list")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise GitHubError("pagination limit exceeded; refusing incomplete state")

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self.paginated(f"/repos/{self.repository}/issues/{number}/comments")

    def can_write(self, login: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", login):
            return False
        try:
            data = self._request("GET", f"/repos/{self.repository}/collaborators/{parse.quote(login, safe='')}/permission")
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise
        return data.get("permission") in {"write", "admin"}

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self._request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}", {"body": body})

    def ensure_label(self, name: str, color: str = "6f42c1", description: str = "") -> None:
        if name in self._labels:
            return
        for known, standard_color, standard_description in REQUIRED_LABELS:
            if known == name:
                color, description = standard_color, standard_description
                break
        path = f"/repos/{self.repository}/labels/{parse.quote(name, safe='')}"
        desired = {"name": name, "color": color, "description": description}
        try:
            existing = self._request("GET", path)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            try:
                self._request("POST", f"/repos/{self.repository}/labels", desired)
            except GitHubError as create_error:
                if create_error.status != 422:
                    raise
                # A concurrent creator is possible, but 422 alone is not evidence of success.
                existing = self._request("GET", path)
            else:
                existing = desired
        if any((str(existing.get(k) or "").lower() if k == "color" else existing.get(k)) != v for k, v in desired.items()):
            self._request("PATCH", path, {"new_name": name, "color": color, "description": description})
        self._labels.add(name)

    def setup_labels(self) -> list[str]:
        ensured = []
        for name, color, desc in REQUIRED_LABELS:
            self.ensure_label(name, color, desc)
            ensured.append(name)
        return ensured


    def add_labels(self, number: int, labels: list[str]) -> None:
        for label in labels:
            self.ensure_label(label)
        self._request("POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        encoded = parse.quote(label, safe="")
        try:
            self._request("DELETE", f"/repos/{self.owner}/{self.repo}/issues/{number}/labels/{encoded}")
        except GitHubError as exc:
            if exc.status != 404:
                raise

    def ensure_handoff_issue(self, marker: str, title: str, body: str) -> int:
        """Create or refresh one durable ChatGPT/local-evidence handoff issue."""
        if not marker or marker not in body:
            raise ValueError("handoff body must contain its stable marker")
        labels = ["chatgpt:handoff", "agent:blocked"]
        for label in labels:
            self.ensure_label(label)
        encoded = parse.quote("chatgpt:handoff", safe="")
        for issue in self.paginated(
            f"/repos/{self.repository}/issues?state=open&labels={encoded}"
        ):
            if issue.get("pull_request"):
                continue
            if marker not in (issue.get("body") or ""):
                continue
            number = int(issue["number"])
            self._request(
                "PATCH",
                f"/repos/{self.repository}/issues/{number}",
                {"title": title, "body": body},
            )
            self.add_labels(number, labels)
            return number
        created = self._request(
            "POST",
            f"/repos/{self.repository}/issues",
            {"title": title, "body": body, "labels": labels},
        )
        number = created.get("number")
        if type(number) is not int:
            raise GitHubError("handoff issue creation returned no issue number")
        return number

    def upsert_router_comment(self, number: int, marker_prefix: str, body: str) -> None:
        for comment in reversed(self.comments(number)):
            if (comment.get("user") or {}).get("login") == "github-actions[bot]" and marker_prefix in (comment.get("body") or ""):
                self.update_comment(int(comment["id"]), body)
                return
        self.add_comment(number, body)

    def get_file(self, path: str, ref: str | None = None) -> dict[str, Any] | None:
        query = f"?ref={parse.quote(ref)}" if ref else ""
        try:
            return self._request("GET", f"/repos/{self.owner}/{self.repo}/contents/{path}{query}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def put_file(
        self,
        path: str,
        content: str,
        message: str,
        branch: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_file(path, ref=branch)
        if existing and base64.b64decode(existing.get("content", "")).decode("utf-8") == content:
            return {"unchanged": True, "content": existing}
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if existing and "sha" in existing:
            payload["sha"] = existing["sha"]
        if branch:
            payload["branch"] = branch
        return self._request("PUT", f"/repos/{self.owner}/{self.repo}/contents/{path}", payload)
