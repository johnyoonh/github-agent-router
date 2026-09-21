from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request


BASE_URL = "https://jules.googleapis.com/v1alpha"


class JulesError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Jules API {status}: {message}")
        self.status = status
        self.message = message


class JulesClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("missing Jules API key")
        self.api_key = api_key

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {"x-goog-api-key": self.api_key}
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=30) as response:
                raw = response.read()
        except error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            raise JulesError(exc.code, raw) from exc
        if not raw:
            return {}
        return json.loads(raw)

    def list_sources(self) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        token = ""
        while True:
            query = "?pageSize=100"
            if token:
                query += "&pageToken=" + parse.quote(token)
            data = self._request("GET", f"/sources{query}")
            sources.extend(data.get("sources", []))
            token = data.get("nextPageToken", "")
            if not token:
                return sources

    def find_source(self, owner: str, repo: str) -> str:
        owner_l, repo_l = owner.lower(), repo.lower()
        for source in self.list_sources():
            gh = source.get("githubRepo") or {}
            if str(gh.get("owner", "")).lower() == owner_l and str(gh.get("repo", "")).lower() == repo_l:
                return source["name"]
        raise LookupError(f"Jules account cannot see GitHub source {owner}/{repo}")

    def create_session(
        self,
        *,
        source: str,
        branch: str,
        title: str,
        prompt: str,
        auto_create_pr: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "prompt": prompt,
            "sourceContext": {
                "source": source,
                "githubRepoContext": {"startingBranch": branch},
            },
            "requirePlanApproval": False,
        }
        if auto_create_pr:
            payload["automationMode"] = "AUTO_CREATE_PR"
        return self._request("POST", "/sessions", payload)

    def get_session(self, session_name: str) -> dict[str, Any]:
        session_id = session_name.rsplit("/", 1)[-1]
        return self._request("GET", f"/sessions/{parse.quote(session_id)}")

    def send_message(self, session_name: str, prompt: str) -> None:
        session_id = session_name.rsplit("/", 1)[-1]
        self._request(
            "POST",
            f"/sessions/{parse.quote(session_id)}:sendMessage",
            {"prompt": prompt},
        )
