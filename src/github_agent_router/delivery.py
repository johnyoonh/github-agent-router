"""Revision-bound merge decisions. No Git-data writes, force pushes, or fake greens."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import re
from urllib.parse import quote

DOMAIN = b"git-fleet/evidence/v1\0"
SHA = re.compile(r"[0-9a-f]{40}")
APPROVAL_PREFIX = "/fleet-approve-exception "
SUPPORTED_RULES = {"deletion", "non_fast_forward", "pull_request", "required_status_checks"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return parsed.astimezone(timezone.utc)


def validate_receipt(receipt, pr, *, key, now=None, max_age=3600):
    """Authenticate evidence independently from the code under test and PR labels."""
    now = now or datetime.now(timezone.utc)
    if not key:
        return "evidence_signing_key_missing"
    data = dict(receipt)
    signature = data.pop("signature", "")
    expected = hmac.new(key.encode(), DOMAIN + canonical(data), hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        return "evidence_signature_invalid"
    for name in ("repository", "pr", "head_sha", "base_sha", "merge_tree_sha"):
        if data.get(name) != pr.get(name):
            return "evidence_revision_mismatch"
    if data.get("schema_version") != 1 or data.get("status") != "passed" or data.get("repair_shas"):
        return "evidence_not_final_source_pr"
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks or any(
            not isinstance(c, dict) or not c.get("argv") or c.get("returncode") != 0 or c.get("timed_out")
            for c in checks):
        return "evidence_checks_not_passed"
    try:
        age = (now - utc(data["completed_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return "evidence_timestamp_invalid"
    if not 0 <= age <= max_age:
        return "evidence_expired"
    return ""


def validate_exception(comment, decision, receipt, *, approvers, now=None):
    """An exact, recent GitHub comment by an allowlisted human is the approval.

    Agents never manufacture this comment. It cannot waive code failures, reviews,
    stale evidence, or unknown protection; it only names verified CI outages.
    """
    now = now or datetime.now(timezone.utc)
    user = comment.get("user") or {}
    if user.get("type") != "User" or user.get("login") not in approvers:
        return False
    body = comment.get("body", "")
    if not isinstance(body, str) or not body.startswith(APPROVAL_PREFIX):
        return False
    try:
        grant = json.loads(body[len(APPROVAL_PREFIX):])
        if any(grant.get(k) != decision.get(k) for k in
               ("repository", "pr", "head_sha", "base_sha", "merge_tree_sha")):
            return False
        created = utc(comment["created_at"])
        expires = utc(grant["expires_at"])
        if not 0 <= (now - created).total_seconds() <= 900 or not now < expires <= created + timedelta(minutes=15):
            return False
        if comment.get("updated_at") != comment.get("created_at"):
            return False
        if grant.get("receipt_sha256") != hashlib.sha256(canonical(receipt)).hexdigest():
            return False
        if not isinstance(grant.get("reason"), str) or not grant["reason"].strip():
            return False
        return sorted(grant.get("waive_checks", [])) == sorted(decision.get("infrastructure_checks", []))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def evaluate(pr, certification, receipt, *, evidence_key, approval=None,
             approvers=(), now=None):
    """Pure decision function; caller supplies authenticated, freshly loaded inputs."""
    result = {k: pr.get(k) for k in ("repository", "pr", "head_sha", "base_sha", "merge_tree_sha")}
    reasons = []
    if not pr.get("open") or pr.get("draft") or not pr.get("same_repository"):
        reasons.append("pr_not_eligible")
    if pr.get("mergeable") is not True:
        reasons.append("mergeability_unknown_or_conflicting")
    if pr.get("protection_known") is not True:
        reasons.append("protection_unknown")
    if pr.get("unsupported_rules"):
        reasons.append("unsupported_protection_rules")
    if pr.get("reviews_satisfied") is not True:
        reasons.append("reviews_not_satisfied")
    if any(not isinstance(pr.get(k), str) or not SHA.fullmatch(pr[k]) for k in ("head_sha", "base_sha", "merge_tree_sha")):
        reasons.append("revision_unknown")
    if not certification or certification.get("verdict") != "certified" or any(
            certification.get(k) != pr.get(k) for k in ("repository", "pr", "head_sha", "base_sha", "merge_tree_sha")):
        reasons.append("jules_certification_stale_or_missing")
    evidence_error = validate_receipt(receipt, pr, key=evidence_key, now=now)
    if evidence_error:
        reasons.append(evidence_error)
    checks = pr.get("required_checks", [])
    outages = [c["name"] for c in checks if c.get("state") == "infrastructure_unavailable"]
    failures = [c["name"] for c in checks if c.get("state") not in {"success", "infrastructure_unavailable"}]
    if failures:
        reasons.append("required_checks_not_passed")
    result["infrastructure_checks"] = outages
    result["blocking_checks"] = failures
    # A non-bypass identity plus strict up-to-date checks closes the base-SHA race.
    # Elevated identities are never silently used for automatic protected merges.
    if pr.get("protected") and not pr.get("strict_up_to_date"):
        reasons.append("strict_base_protection_required")
    result["reasons"] = reasons
    result["status"] = "blocked" if reasons else "ready"
    if not reasons and outages:
        result["status"] = "human_required"
        if approval and validate_exception(approval, result, receipt, approvers=approvers, now=now):
            result["status"] = "exception_authorized"
            result["approval_comment_id"] = approval["id"]
    elif not reasons and pr.get("bypass_capable"):
        result["status"] = "human_required"
        result["reasons"] = ["use_non_bypass_merge_identity"]
    return result


def merge_native(gh, decision, *, refresh):
    """Only the native PR merge endpoint. A 404/403 never causes another write path."""
    if decision.get("status") not in {"ready", "exception_authorized"}:
        raise ValueError("decision does not authorize merge")
    current = refresh()
    if any(current.get(k) != decision.get(k) for k in
           ("repository", "pr", "head_sha", "base_sha", "merge_tree_sha", "status", "approval_comment_id")):
        raise ValueError("merge inputs or authorization changed; re-evaluate")
    result = gh._request("PUT", f"/repos/{decision['repository']}/pulls/{decision['pr']}/merge",
                         {"sha": decision["head_sha"], "merge_method": "merge"})
    if result.get("merged") and result.get("sha"):
        commit = gh._request("GET", f"/repos/{decision['repository']}/git/commits/{result['sha']}")
        result["promotion_ready"] = commit.get("tree", {}).get("sha") == decision["merge_tree_sha"]
        if not result["promotion_ready"]:
            result["warning"] = "Merged tree changed; reverify before any deployment"
    return result


def check_state(check, annotations, job=None):
    if check.get("status") != "completed":
        return "pending"
    if check.get("conclusion") == "success":
        return "success"
    # Positive evidence only; never infer an outage merely from a red/absent check.
    messages = " ".join(str(a.get("message", "")) for a in annotations).lower()
    billing = ("spending limit" in messages or "actions minutes" in messages or
               "recent account payments have failed" in messages or
               "billing issue" in messages)
    scheduler_rejected = (job is not None and job.get("runner_id") == 0 and
                          job.get("steps") == [] and
                          (check.get("app") or {}).get("slug") == "github-actions")
    return "infrastructure_unavailable" if billing and scheduler_rejected else "failure"
