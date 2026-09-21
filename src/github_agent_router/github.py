from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request


API = "https://api.github.com"


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

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self._request("GET", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments?per_page=100")

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self._request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}", {"body": body})

    def ensure_label(self, name: str, color: str = "6f42c1") -> None:
        encoded = parse.quote(name, safe="")
        try:
            self._request("GET", f"/repos/{self.owner}/{self.repo}/labels/{encoded}")
        except GitHubError as exc:
            if "404" not in str(exc):
                raise
            self._request("POST", f"/repos/{self.owner}/{self.repo}/labels", {"name": name, "color": color})

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
