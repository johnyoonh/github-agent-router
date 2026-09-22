from __future__ import annotations

import base64
import json
from typing import Any
from urllib import error, parse, request



API = "https://api.github.com"


REQUIRED_LABELS: list[tuple[str, str, str]] = [
    ("jules:run", "0e8a16", "Opt-in to autonomous Jules review and routing"),
    ("jules-owner:a", "1d76db", "Sticky Jules identity A ownership"),
    ("jules-owner:b", "5319e7", "Sticky Jules identity B ownership"),
    ("jules:needs-user", "d93f0b", "Jules is waiting for user clarification or review cap reached"),
    ("agent:jules", "fbca04", "Authored by Jules; excluded from autonomous review loops"),
]


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, repository: str):
        if "/" not in repository:
            raise ValueError("repository must be owner/name")
        self.token = token
        self.repository = repository
        self.owner, self.repo = repository.split("/", 1)

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
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
            detail = exc.read().decode(errors="replace")
            raise GitHubError(f"GitHub API {exc.code}: {detail}") from exc
        if not raw:
            return {}
        return json.loads(raw)

    def get_repo(self) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.owner}/{self.repo}")

    def is_private(self) -> bool:
        private = self.get_repo().get("private")
        if not isinstance(private, bool):
            raise GitHubError("GitHub API response missing boolean private field")
        return private

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self._request("GET", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments?per_page=100")


    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self._request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}", {"body": body})

    def ensure_label(self, name: str, color: str = "6f42c1", description: str = "") -> None:
        encoded = parse.quote(name, safe="")
        payload: dict[str, str] = {"name": name, "color": color}
        if description:
            payload["description"] = description
        try:
            self._request("GET", f"/repos/{self.owner}/{self.repo}/labels/{encoded}")
        except GitHubError as exc:
            if "404" not in str(exc):
                raise
            self._request("POST", f"/repos/{self.owner}/{self.repo}/labels", payload)

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
            if "404" not in str(exc):
                raise

    def upsert_router_comment(self, number: int, marker_prefix: str, body: str) -> None:
        for comment in self.comments(number):
            if marker_prefix in (comment.get("body") or ""):
                self.update_comment(int(comment["id"]), body)
                return
        self.add_comment(number, body)

    def get_file(self, path: str, ref: str | None = None) -> dict[str, Any] | None:
        query = f"?ref={parse.quote(ref)}" if ref else ""
        try:
            return self._request("GET", f"/repos/{self.owner}/{self.repo}/contents/{path}{query}")
        except GitHubError as exc:
            if "404" in str(exc):
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
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if existing and "sha" in existing:
            payload["sha"] = existing["sha"]
        if branch:
            payload["branch"] = branch
        return self._request("PUT", f"/repos/{self.owner}/{self.repo}/contents/{path}", payload)
