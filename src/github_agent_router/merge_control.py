"""Load live GitHub facts before making or executing a merge decision."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urlparse

from .delivery import check_state, evaluate, merge_native, SUPPORTED_RULES
from .certification import read_state


def pages(gh, path, key=None):
    rows = []
    for page in range(1, 101):
        value = gh._request("GET", path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        batch = value[key] if key else value
        if not isinstance(batch, list):
            raise ValueError("invalid paginated response")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise ValueError("pagination limit exceeded; protection remains unknown")


def live_snapshot(gh, number):
    repo = gh.repository
    pr = gh._request("GET", f"/repos/{repo}/pulls/{number}")
    base, head = pr["base"], pr["head"]
    info = gh._request("GET", f"/repos/{repo}")
    branch = quote(base["ref"], safe="")
    details = gh._request("GET", f"/repos/{repo}/branches/{branch}")
    rules = pages(gh, f"/repos/{repo}/rules/branches/{branch}")
    unknown = [r.get("type", "unknown") for r in rules if r.get("type") not in SUPPORTED_RULES]
    requirements, review_rules = [], []
    strict = False
    for rule in rules:
        params = rule.get("parameters", {})
        if rule["type"] == "required_status_checks":
            requirements.extend(params.get("required_status_checks", []))
            strict = strict or params.get("strict_required_status_checks_policy") is True
        elif rule["type"] == "pull_request":
            review_rules.append(params)
            if "merge" not in params.get("allowed_merge_methods", ["merge"]):
                unknown.append("merge_commit_not_allowed")
            if params.get("require_extra_approval_for_unattributed_changes"):
                unknown.append("extra_approval_for_unattributed_changes")
    known = True
    bypass = False
    if details.get("protected"):
        # Legacy protection may coexist with rulesets. Missing access is not permission to ignore it.
        try:
            classic = gh._request("GET", f"/repos/{repo}/branches/{branch}/protection")
        except Exception as exc:
            if getattr(exc, "status", None) == 404 and rules and not details.get("protection"):
                classic = {}
            else:
                classic, known = {}, False
        for setting in ("required_linear_history", "required_signatures", "lock_branch"):
            if classic.get(setting, {}).get("enabled"):
                unknown.append(setting)
        if classic.get("restrictions"):
            unknown.append("classic_push_restrictions")
        checks = classic.get("required_status_checks") or {}
        strict = strict or checks.get("strict") is True
        requirements.extend(checks.get("checks") or [{"context": x} for x in checks.get("contexts", [])])
        if classic.get("required_pull_request_reviews"):
            review_rules.append(classic["required_pull_request_reviews"])
        if classic.get("required_conversation_resolution", {}).get("enabled"):
            review_rules.append({"required_review_thread_resolution": True})
        bypass = bool(classic and info.get("permissions", {}).get("admin") and
                      not classic.get("enforce_admins", {}).get("enabled"))
    for item in pages(gh, f"/repos/{repo}/rulesets?includes_parents=true"):
        url = item.get("_links", {}).get("self", {}).get("href", "")
        if urlparse(url).hostname != "api.github.com":
            known = False
            continue
        rule = gh._request("GET", urlparse(url).path)
        if rule.get("enforcement") == "active" and rule.get("current_user_can_bypass") not in {"always", "pull_request", "pull_requests", "exempt", "never"}:
            known = False
        if rule.get("enforcement") == "active" and rule.get("current_user_can_bypass") in {"always", "pull_request", "pull_requests", "exempt"}:
            bypass = True  # Conservative even if a different branch's ruleset grants it.
    reviews_ok = True
    if review_rules:
        required = max(r.get("required_approving_review_count", 0) for r in review_rules)
        latest = {}
        for review in pages(gh, f"/repos/{repo}/pulls/{number}/reviews"):
            if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                latest[(review.get("user") or {}).get("login")] = review
        authorized = {u: r for u, r in latest.items() if u and gh.can_write(u)}
        reviews_ok = (sum(r["state"] == "APPROVED" and r.get("commit_id") == head["sha"]
                          for r in authorized.values()) >= required and
                      not any(r["state"] == "CHANGES_REQUESTED" for r in authorized.values()))
        if any(r.get("require_code_owner_review") or r.get("require_code_owner_reviews") or
               r.get("require_last_push_approval") for r in review_rules):
            unknown.append("review_identity_policy_requires_native_evaluation")
        if any(r.get("required_review_thread_resolution") for r in review_rules):
            cursor = None
            for _ in range(100):
                query = """query($owner:String!,$name:String!,$number:Int!,$after:String){
                  repository(owner:$owner,name:$name){pullRequest(number:$number){
                    reviewThreads(first:100,after:$after){nodes{isResolved}
                      pageInfo{hasNextPage endCursor}}}}}"""
                owner, name = repo.split("/", 1)
                data = gh._request("POST", "/graphql", {"query": query,
                    "variables": {"owner": owner, "name": name, "number": number, "after": cursor}})
                threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
                reviews_ok = reviews_ok and all(x["isResolved"] for x in threads["nodes"])
                if not threads["pageInfo"]["hasNextPage"]:
                    break
                cursor = threads["pageInfo"]["endCursor"]
            else:
                known = False
    runs = pages(gh, f"/repos/{repo}/commits/{head['sha']}/check-runs?filter=latest", "check_runs")
    statuses = pages(gh, f"/repos/{repo}/commits/{head['sha']}/statuses")
    # A ruleset source app is part of a requirement, not just the context's display name.
    resolved = []
    if not requirements:
        requirements = [{"context": c["name"], "integration_id": (c.get("app") or {}).get("id")} for c in runs]
        requirements += [{"context": s["context"], "status_only": True} for s in statuses]
    seen = set()
    for requirement in requirements:
        name = requirement.get("context")
        app = requirement.get("integration_id", requirement.get("app_id"))
        identity = (name, app, requirement.get("status_only", False))
        if identity in seen:
            continue
        seen.add(identity)
        matches = [c for c in runs if c["name"] == name and not requirement.get("status_only") and
                   (app in (None, -1) or (c.get("app") or {}).get("id") == app)]
        matching_statuses = [s for s in statuses if s["context"] == name and app in (None, -1)]
        states = []
        if matches:
            newest = max(matches, key=lambda x: x["id"])
            annotations = pages(gh, f"/repos/{repo}/check-runs/{newest['id']}/annotations") if newest.get("conclusion") == "failure" else []
            job = None
            match = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/actions/runs/[0-9]+/job/([0-9]+)", newest.get("details_url", ""))
            if newest.get("conclusion") == "failure" and match and (newest.get("app") or {}).get("slug") == "github-actions":
                try:
                    candidate = gh._request("GET", f"/repos/{repo}/actions/jobs/{match.group(1)}")
                    if candidate.get("check_run_url") == f"https://api.github.com/repos/{repo}/check-runs/{newest['id']}":
                        job = candidate
                except Exception:
                    pass  # Failed scheduler provenance is not an outage exemption.
            states.append(check_state(newest, annotations, job))
        if matching_statuses:
            newest = max(matching_statuses, key=lambda x: x["id"])
            states.append(newest["state"])
        state = "missing" if not states else "success" if all(s == "success" for s in states) else (
            "infrastructure_unavailable" if all(s in {"success", "infrastructure_unavailable"} for s in states) else "failure")
        resolved.append({"name": name, "state": state, "app_id": app})
    tree = ""
    if pr.get("merge_commit_sha"):
        commit = gh._request("GET", f"/repos/{repo}/git/commits/{pr['merge_commit_sha']}")
        if [p["sha"] for p in commit.get("parents", [])] == [base["sha"], head["sha"]]:
            tree = commit["tree"]["sha"]
    return {"repository": repo, "pr": number, "head_sha": head["sha"], "base_sha": base["sha"],
            "merge_tree_sha": tree, "open": pr.get("state") == "open", "draft": pr.get("draft"),
            "same_repository": (head.get("repo") or {}).get("full_name", "").lower() == repo,
            "mergeable": pr.get("mergeable"), "protection_known": known,
            "unsupported_rules": unknown, "protected": details.get("protected"),
            "strict_up_to_date": strict, "bypass_capable": bypass,
            "reviews_satisfied": reviews_ok, "required_checks": resolved}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="github-agent-router merge-evaluate")
    parser.add_argument("repository")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--approval-comment-id", type=int)
    parser.add_argument("--policy", type=Path, required=True, help="trusted local delivery policy, not PR content")
    parser.add_argument("--execute", action="store_true", help="execute only the freshly authorized native PR merge")
    args = parser.parse_args(argv)
    from .config import Config
    from .github import GitHubClient
    config = Config.from_env()
    policy = json.loads(args.policy.read_text())
    repo = args.repository.lower()
    if repo not in policy.get("allowed_repositories", []):
        parser.error("repository is not in the explicit merge allowlist")
    receipt = json.loads(args.receipt.read_text())
    gh = GitHubClient(config.github_token, repo)
    def decision():
        snapshot = live_snapshot(gh, args.pr)
        state = read_state(gh, args.pr, config.jules_keys)
        cert = ({"repository": repo, "pr": args.pr, **state.get("verdict", {})}
                if state and state.get("verification") == "certified" else {})
        approval = None
        if args.approval_comment_id:
            approval = gh._request("GET", f"/repos/{repo}/issues/comments/{args.approval_comment_id}")
            if approval.get("issue_url") != f"https://api.github.com/repos/{repo}/issues/{args.pr}":
                raise ValueError("approval comment belongs to a different PR")
        return evaluate(snapshot, cert, receipt, evidence_key=os.getenv("GIT_FLEET_EVIDENCE_KEY", ""),
                        approval=approval, approvers=policy.get("exception_approvers", []))
    result = decision()
    if args.execute:
        result["merge"] = merge_native(gh, result, refresh=decision)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"ready", "exception_authorized"} and (not args.execute or result.get("merge", {}).get("merged") is True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
