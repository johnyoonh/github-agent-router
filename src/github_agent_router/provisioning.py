"""Idempotent label/workflow provisioning through a dedicated pull request."""
from __future__ import annotations

from dataclasses import asdict
import base64
import hashlib
import re
from typing import Any
from urllib.parse import quote

from .config import Config
from .github import GitHubClient, GitHubError
from .inventory import RepositoryPolicy, resolve_policy
from .jules import JulesClient


ROUTER_REPOSITORY = "johnyoonh/github-agent-router"
WORKFLOW_PATH = ".github/workflows/jules-router.yml"
MANAGED = "# Managed by github-agent-router; schema=1"
PR_MARKER = "<!-- github-agent-router:provision:v1 -->"


def render_workflow(policy: RepositoryPolicy, revision: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("router revision must be a full, reviewed commit SHA")
    if not policy.private:
        raise ValueError("public target cannot call the private router; approve a public-safe distribution first")
    secrets = [f"      jules_{x}_api_key: ${{{{ secrets.JULES_{x.upper()}_API_KEY }}}}" for x in policy.allowed_owners]
    if policy.repository != ROUTER_REPOSITORY:
        secrets.append("      router_read_token: ${{ secrets.ROUTER_READ_TOKEN }}")
    return f'''{MANAGED}
name: Jules Router

on:
  issues:
    types: [opened, edited, labeled, reopened]
  pull_request:
    types: [opened, synchronize, reopened, labeled]
  issue_comment:
    types: [created]

permissions:
  contents: read
  issues: write
  pull-requests: write

jobs:
  jules:
    uses: {ROUTER_REPOSITORY}/.github/workflows/reusable-router.yml@{revision}
    with:
      router_ref: {revision}
      home: {policy.home}
      overflow: "{policy.overflow}"
      allowed_owners: "{','.join(policy.allowed_owners)}"
      max_rounds: {policy.max_rounds}
      auto_review_prs: {str(policy.auto_review_prs).lower()}
      verification_timeout: 1800
    secrets:
''' + "\n".join(secrets) + "\n"


def decode_file(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type", "file") != "file":
        raise ValueError("managed workflow path is not a regular file")
    return base64.b64decode(value.get("content", "")).decode("utf-8")


def readiness(gh: GitHubClient, config: Config, policy: RepositoryPolicy) -> None:
    """Do not activate a workflow with unchecked account/source or secret prerequisites."""
    for owner in policy.allowed_owners:
        key = config.jules_keys.get(owner)
        if not key:
            raise ValueError(f"Jules {owner.upper()} credential is required to verify selected-account source access")
        JulesClient(key).find_source(*policy.repository.split("/", 1))
    required = {f"JULES_{x.upper()}_API_KEY" for x in policy.allowed_owners}
    if policy.repository != ROUTER_REPOSITORY:
        required.add("ROUTER_READ_TOKEN")
    names: set[str] = set()
    for page in range(1, 1001):
        data = gh._request("GET", f"/repos/{policy.repository}/actions/secrets?per_page=100&page={page}")
        batch = data.get("secrets", [])
        names.update(x["name"] for x in batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError("secret metadata pagination limit exceeded")
    if required - names:
        raise ValueError("missing Actions secret metadata: " + ", ".join(sorted(required - names)))
    # Secret values are not readable. This cannot prove that the stored values equal
    # the locally tested credentials or that the checkout token is valid.


def inspect_repository(config: Config, repository: str, document: dict[str, Any]) -> dict[str, Any]:
    gh = GitHubClient(config.github_token, repository, dry_run=True)
    info = gh.get_repo()
    policy = resolve_policy(info, document)
    return {**asdict(policy), "archived": bool(info.get("archived")),
            "workflow_compatible": policy.private,
            "credentials": "not_checked", "status": "planned" if policy.enabled else "disabled"}


def provision_repository(config: Config, repository: str, document: dict[str, Any], revision: str) -> dict[str, Any]:
    gh = GitHubClient(config.github_token, repository, dry_run=config.dry_run)
    info = gh.get_repo()
    policy = resolve_policy(info, document)
    if not policy.enabled:
        return {"repository": policy.repository, "status": "disabled"}
    if info.get("archived"):
        raise ValueError("archived repository is not a provisioning target")
    content = render_workflow(policy, revision)
    existing = gh.get_file(WORKFLOW_PATH, ref=policy.default_branch)
    old = decode_file(existing)
    if old is not None and old != content and not old.startswith(MANAGED + "\n"):
        raise ValueError("existing workflow is not managed; review migration rather than overwriting it")
    if config.dry_run:
        return {"repository": policy.repository, "status": "unchanged" if old == content else "would_open_pr", "default_branch": policy.default_branch}
    if not config.github_token:
        raise ValueError("GITHUB_TOKEN is required to provision")
    readiness(gh, config, policy)
    gh.setup_labels()
    if old == content:
        return {"repository": policy.repository, "status": "unchanged"}
    base = quote(policy.default_branch, safe="")
    base_sha = gh._request("GET", f"/repos/{policy.repository}/git/ref/heads/{base}")["object"]["sha"]
    pulls = gh.paginated(f"/repos/{policy.repository}/pulls?state=open&base={base}")
    managed = [pr for pr in pulls if PR_MARKER in (pr.get("body") or "")]
    if len(managed) > 1:
        raise ValueError("multiple provisioning PRs; reconcile before writing")
    pr = managed[0] if managed else None
    if pr:
        branch = pr["head"]["ref"]
        if not branch.startswith("chatgpt/") or str((pr["head"].get("repo") or {}).get("full_name", "")).lower() != policy.repository:
            raise ValueError("provisioning PR is not on an owned chatgpt/ branch")
        comparison = gh._request("GET", f"/repos/{policy.repository}/compare/{base}...{quote(branch, safe='')}")
        if comparison.get("behind_by", 0) or any(x["filename"] != WORKFLOW_PATH for x in comparison.get("files", [])):
            raise ValueError("provisioning branch is stale or contains unrelated changes; rebase/reconcile without force")
    else:
        digest = hashlib.sha256((base_sha + content).encode()).hexdigest()[:16]
        branch = f"chatgpt/jules-router-{digest}"
        ref_path = f"/repos/{policy.repository}/git/ref/heads/{quote(branch, safe='')}"
        try:
            ref = gh._request("GET", ref_path)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            try:
                gh._request("POST", f"/repos/{policy.repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
            except GitHubError as create_error:
                if create_error.status != 422:
                    raise
                ref = gh._request("GET", ref_path)
            else:
                ref = {"object": {"sha": base_sha}}
        # Resume only an empty branch or exactly the workflow-only change we planned.
        if ref["object"]["sha"] != base_sha:
            comparison = gh._request("GET", f"/repos/{policy.repository}/compare/{base}...{quote(branch, safe='')}")
            if comparison.get("behind_by", 0) or not comparison.get("files") or any(x["filename"] != WORKFLOW_PATH for x in comparison["files"]):
                raise ValueError("branch collision with unrelated/stale work")
            if decode_file(gh.get_file(WORKFLOW_PATH, ref=branch)) != content:
                raise ValueError("branch contains an unrecognized workflow change")
    branch_content = decode_file(gh.get_file(WORKFLOW_PATH, ref=branch))
    if branch_content is not None and not branch_content.startswith(MANAGED + "\n"):
        raise ValueError("task branch workflow is not managed; preserve unrelated edits")
    gh.put_file(WORKFLOW_PATH, content, "ci: reconcile pinned Jules routing", branch=branch)
    if decode_file(gh.get_file(WORKFLOW_PATH, ref=branch)) != content:
        raise ValueError("workflow readback did not match the planned content")
    if not pr:
        # Re-read after writes, including recovery from an earlier uncertain PR POST.
        matching = gh.paginated(f"/repos/{policy.repository}/pulls?state=open&head={quote(gh.owner + ':' + branch, safe='')}&base={base}")
        if len(matching) > 1:
            raise ValueError("duplicate branch PRs")
        if matching:
            pr = matching[0]
        else:
            try:
                pr = gh._request("POST", f"/repos/{policy.repository}/pulls", {
                    "head": branch, "base": policy.default_branch, "title": "ci: configure pinned Jules routing",
                    "body": PR_MARKER + "\n\nReconciles explicit executor policy and a reviewed runtime revision.\n\nStored secret values and reusable-workflow access still require a live smoke test. No automatic merge or visibility changes.",
                })
            except GitHubError as exc:
                if exc.status != 422:
                    raise
                matching = gh.paginated(f"/repos/{policy.repository}/pulls?state=open&head={quote(gh.owner + ':' + branch, safe='')}&base={base}")
                if len(matching) != 1:
                    raise
                pr = matching[0]
    return {"repository": policy.repository, "status": "pr_open", "url": pr["html_url"], "default_branch": policy.default_branch,
            "live_validation": "not_run", "branch": branch}
