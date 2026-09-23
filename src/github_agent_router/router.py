from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Config
from .github import GitHubClient
from .jules import JulesClient, JulesError


MARKER_PREFIX = "<!-- github-agent-router:"
MARKER_RE = re.compile(r"<!-- github-agent-router:(\{.*?\}) -->", re.DOTALL)
ACTIVE_STATES = {"QUEUED", "PLANNING", "AWAITING_PLAN_APPROVAL", "IN_PROGRESS", "PAUSED"}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "CANCELED"}
VERIFICATION_RE = re.compile(
    r"<!-- github-agent-router:verification:(\{.*?\}) -->", re.DOTALL
)
CHATGPT_ORIGIN_RE = re.compile(
    r"<!-- chatgpt-opencli:origin:(\{.*?\}) -->", re.DOTALL
)
VERDICTS = {"certified", "changes_required", "needs_evidence", "needs_user"}
AUTONOMOUS_UNBLOCK_PROMPT = """Before waiting for user feedback, classify what is missing.
Proceed without asking if code, tests, repository docs, or a conservative non-destructive
assumption can answer it. If only machine-local evidence is unavailable, finish with a
needs_evidence verification verdict and list the exact bounded logs/commands needed.
Remain waiting for the user only for a material product, security, scope, or data-loss
decision that cannot be inferred safely."""


@dataclass
class RouteState:
    owner: str
    session: str
    url: str = ""
    round: int = 1
    state: str = ""
    repository: str = ""
    number: int = 0
    events: list[str] = field(default_factory=list)
    pending: str = ""
    operation: str = ""
    source: str = ""
    branch: str = ""
    operation_event: str = ""
    comment_id: int = 0
    needs_user: bool = False
    verification: str = ""
    handoff_issue: int = 0
    feedback_nudges: int = 0

    def comment(self, *, key: str = "") -> str:
        meta = asdict(self)
        if key:
            meta["signature"] = state_signature(meta, key)
        lines = [
            f"Jules owner: **{self.owner.upper()}**",
            f"Session: `{self.session}`",
            f"Round: **{self.round}**",
        ]
        if self.url:
            lines.append(f"Jules: {self.url}")
        if self.state:
            lines.append(f"State: **{self.state}**")
        if self.verification:
            lines.append(f"Verification: **{self.verification}**")
        if self.handoff_issue:
            lines.append(f"Handoff issue: **#{self.handoff_issue}**")
        return f"{MARKER_PREFIX}{json.dumps(meta, separators=(',', ':'))} -->\n\n" + "  \n".join(lines)



def label_names(obj: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for label in obj.get("labels") or []:
        result.add(label if isinstance(label, str) else str(label.get("name", "")))
    return {x for x in result if x}


def owner_from_labels(labels: set[str]) -> str | None:
    owners = [owner for owner in ("a", "b") if f"jules-owner:{owner}" in labels]
    if len(owners) > 1:
        raise ValueError("conflicting Jules owner labels; reconcile ownership")
    return owners[0] if owners else None


def preferred_home(config: Config, repository_private: bool | None = None) -> str:
    """Resolve the configured primary Jules account for a repository."""
    if config.home == "auto":
        if repository_private is None:
            raise ValueError("repository visibility is required when JULES_HOME=auto")
        home = config.private_home if repository_private else config.public_home
    else:
        home = config.home
    if home not in config.jules_keys:
        setting = "JULES_PRIVATE_HOME" if repository_private else "JULES_PUBLIC_HOME"
        if config.home != "auto":
            setting = "JULES_HOME"
        raise ValueError(f"unknown {setting}={home}")
    return home


def _overflow_owner(config: Config, home: str) -> str | None:
    overflow = config.overflow
    if overflow == "auto":
        overflow = "b" if home == "a" else "a"
    if overflow is not None and overflow not in config.jules_keys:
        raise ValueError(f"unknown JULES_OVERFLOW={overflow}")
    return overflow


def choose_owner(
    labels: set[str],
    config: Config,
    repository_private: bool | None = None,
) -> str:
    sticky = owner_from_labels(labels)
    if sticky:
        return sticky
    home = preferred_home(config, repository_private)
    if config.jules_keys.get(home):
        return home
    overflow = _overflow_owner(config, home)
    if overflow and config.jules_keys.get(overflow):
        return overflow
    raise ValueError("no configured Jules API key is available")


def state_signature(meta: dict[str, Any], key: str) -> str:
    derived = hmac.new(key.encode(), b"github-agent-router/state/v1", hashlib.sha256).digest()
    return hmac.new(derived, json.dumps(meta, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def parse_route_state(
    comments: list[dict[str, Any]], *, repository: str | None = None,
    number: int | None = None, keys: dict[str, str] | None = None,
    trusted_logins: set[str] | None = None,
) -> tuple[RouteState | None, int | None]:
    """Read only bot-authored state; runtime additionally requires a bound signature.

    Unsigned legacy bot state deliberately blocks creation instead of losing locality.
    Key rotation or ambiguous state requires explicit reconciliation, never a new task.
    """
    for comment in reversed(comments):
        author = comment.get("user") or {}
        bot = author.get("login") == "github-actions[bot]" and author.get("type") == "Bot"
        if not bot and not (keys is not None and author.get("login") in (trusted_logins or set())):
            continue
        match = MARKER_RE.search(comment.get("body") or "")
        if not match:
            continue
        try:
            raw = json.loads(match.group(1))
            signature = raw.pop("signature", "")
            if keys is not None:
                if raw.get("comment_id") and raw["comment_id"] != comment["id"]:
                    continue  # A quote/replay is not the canonical record.
                if raw.get("comment_id") != comment["id"]:
                    raise ValueError("legacy state is not bound to its comment ID; reconcile it")
                key = keys.get(raw.get("owner", ""), "")
                if not key or not isinstance(signature, str) or not hmac.compare_digest(signature, state_signature(raw, key)):
                    raise ValueError("unsigned or invalid state; reconcile legacy state or rotated keys")
                if raw.get("repository") != repository or raw.get("number") != number:
                    raise ValueError("router state belongs to a different repository or issue")
            state = RouteState(**raw)
            if state.owner not in {"a", "b"} or type(state.round) is not int or state.round < 1:
                raise ValueError("invalid state owner or round")
            if state.pending not in {"", "create", "send"}:
                raise ValueError("invalid pending operation")
            if not (state.pending == "create" and not state.session) and not re.fullmatch(r"sessions/[A-Za-z0-9_-]+", state.session):
                raise ValueError("invalid session name")
            if not isinstance(state.events, list) or any(not isinstance(e, str) for e in state.events) or len(state.events) > 256:
                raise ValueError("invalid event history")
            if state.verification and state.verification not in VERDICTS | {"failed", "inconclusive"}:
                raise ValueError("invalid verification state")
            if type(state.handoff_issue) is not int or state.handoff_issue < 0:
                raise ValueError("invalid handoff issue")
            if type(state.feedback_nudges) is not int or state.feedback_nudges < 0:
                raise ValueError("invalid feedback nudge count")
            return state, int(comment["id"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("malformed bot-authored routing state; reconcile before continuing") from exc
    return None, None


def should_route_pr(pr: dict[str, Any], config: Config) -> bool:
    labels = label_names(pr)
    head = str((pr.get("head") or {}).get("ref", ""))
    if "agent:jules" in labels or head.startswith(("jules/", "jules-", "google-jules/")):
        return False
    return (
        head.startswith("chatgpt/")
        or config.auto_review_prs
        or "jules:run" in labels
        or bool(owner_from_labels(labels))
    )


def build_prompt(payload: dict[str, Any]) -> tuple[str, str, str, int, set[str]]:
    repo = payload["repository"]
    default_branch = repo.get("default_branch") or "main"

    if "pull_request" in payload:
        pr = payload["pull_request"]
        number = int(pr["number"])
        branch = (pr.get("head") or {}).get("ref") or default_branch
        labels = label_names(pr)
        title = f"Review PR #{number}: {pr.get('title', '')}"[:120]
        body = pr.get("body") or ""
        head_sha = (pr.get("head") or {}).get("sha") or ""
        prompt = f"""Independently verify GitHub PR #{number} in {repo['full_name']} on branch {branch} (head {head_sha}).

PR title: {pr.get('title', '')}
PR description:
{body}

Act as an adversarial verification gate, not a style reviewer. Derive concrete behavioral success criteria from the request and changed code. Run the existing relevant tests and static checks, add focused regression or behavioral tests when coverage is missing, and deliberately probe realistic failure modes: edge/error paths, state transitions, concurrency/races, security boundaries, compatibility, and data-loss risks when relevant.

Fix only verified problems and keep fixes/tests scoped. Do not manufacture cleanup. Do not ask for routine confirmation, logs you can derive from the repository, or choices that have a conservative non-destructive answer. If machine-local evidence is truly required, do not wait indefinitely: return needs_evidence with the exact bounded commands/logs required. Use needs_user only for a material product, security, scope, or data-loss decision that cannot be inferred safely.

Your final agent message for every completed review round MUST contain exactly one marker with JSON:
<!-- github-agent-router:verification:{{"verdict":"certified","summary":"concise evidence summary","tests":["command/result or not_applicable with reason"],"red_team":["adversarial case/result"],"requests":[]}} -->

Allowed verdicts are certified, changes_required, needs_evidence, and needs_user.
- certified: behavioral checks and at least one adversarial/red-team probe passed; no known merge-blocking shortcoming remains.
- changes_required: a verified defect remains; describe it and any corrective PR/output.
- needs_evidence: only external/local evidence is missing; requests must name the smallest safe evidence needed.
- needs_user: a material decision is required; requests must contain the exact question.

A terminal session without a valid marker is not certification."""
        return title, prompt, branch, number, labels

    issue = payload["issue"]
    number = int(issue["number"])
    labels = label_names(issue)
    title = f"Issue #{number}: {issue.get('title', '')}"[:120]
    prompt = f"""Work on GitHub issue #{number} in {repo['full_name']}.

Issue title: {issue.get('title', '')}
Issue body:
{issue.get('body') or ''}

Implement the requested behavior with the smallest coherent change. Preserve existing contracts unless the issue explicitly changes them. Add or update tests. Run relevant tests and static checks. If the requirement is materially ambiguous or a product decision is needed, ask for clarification rather than guessing."""
    return title, prompt, default_branch, number, labels


def extract_verification(activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest structured Jules verification verdict."""
    for activity in reversed(activities):
        message = str((activity.get("agentMessaged") or {}).get("agentMessage", ""))
        match = VERIFICATION_RE.search(message)
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ValueError("Jules verification marker contains invalid JSON") from exc
        verdict = value.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError("Jules verification marker has an unknown verdict")
        if not isinstance(value.get("summary"), str) or not value["summary"].strip():
            raise ValueError("Jules verification marker requires summary")
        for key in ("tests", "red_team", "requests"):
            rows = value.get(key, [])
            if not isinstance(rows, list) or any(not isinstance(x, str) for x in rows):
                raise ValueError(f"Jules verification marker {key} must be a list of strings")
            value[key] = rows
        if verdict == "certified" and (not value["tests"] or not value["red_team"]):
            raise ValueError("certification requires behavioral test and red-team evidence")
        if verdict in {"needs_evidence", "needs_user"} and not value["requests"]:
            raise ValueError(f"{verdict} requires an explicit request")
        return value
    return None


def extract_chatgpt_origin(body: str) -> dict[str, str]:
    """Read optional provenance emitted by chatgpt-opencli without trusting arbitrary URLs."""
    match = CHATGPT_ORIGIN_RE.search(body or "")
    if not match:
        return {}
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    result: dict[str, str] = {}
    conversation = raw.get("conversation")
    if isinstance(conversation, str) and re.fullmatch(r"https://chatgpt\.com/(?:c|share)/[^\s]+", conversation):
        result["conversation"] = conversation
    for key in ("job_id", "project", "agents_sha"):
        value = raw.get(key)
        if isinstance(value, str) and value and len(value) <= 256:
            result[key] = value
    return result


def ensure_handoff(
    gh: GitHubClient,
    payload: dict[str, Any],
    state: RouteState,
    verification: dict[str, Any],
) -> int:
    repo = payload["repository"]["full_name"].lower()
    obj = payload.get("pull_request") or payload.get("issue") or {}
    number = int(obj["number"])
    kind = "pull request" if "pull_request" in payload else "issue"
    marker = f"<!-- github-agent-router:handoff:v1 {repo}#{number} -->"
    origin = extract_chatgpt_origin(str(obj.get("body") or ""))
    source_url = (
        str(obj.get("html_url") or "")
        or f"https://github.com/{repo}/{'pull' if kind == 'pull request' else 'issues'}/{number}"
    )
    requests = verification.get("requests") or []
    request_text = "\n".join(f"- {x}" for x in requests) if requests else "- None specified."
    origin_text = origin.get("conversation", "Unavailable; create a fresh project-scoped handoff rather than guessing a conversation.")
    agents_text = origin.get("agents_sha", "Unavailable; the local consumer must read the current applicable AGENTS.md before acting.")
    body = f"""{marker}

Source: {kind} #{number} — {source_url}
Jules session: {state.url or state.session}
Verification verdict: {verification.get('verdict')}
Summary: {verification.get('summary')}

Requested evidence or decision:
{request_text}

ChatGPT continuation:
- conversation: {origin_text}
- project: {origin.get('project', 'derive from the repository checkout')}
- AGENTS.md provenance: {agents_text}

Local consumer contract:
1. Reconcile the checkout with `fleet sync --only {repo}`; do not use git-fleet as the reasoning agent.
2. Read the current applicable local/repository AGENTS.md. If chatgpt-opencli maintains project Instructions, synchronize them before submitting the handoff; never copy private instruction text into this issue.
3. Collect only the bounded evidence requested above, redact credentials/private payloads, and attach a concise result.
4. If a canonical ChatGPT conversation URL is present, chatgpt-opencli may continue that conversation. Otherwise start a new repository-project handoff; never guess an original ChatGPT.com session.
5. Return actionable evidence/fixes to the source PR. A `/jules ...` comment may resume the sticky Jules session after the branch/evidence is ready.

This issue is a durable queue item, not authorization to bypass CI, branch protection, or user decisions."""
    title = f"[agent handoff] {repo} #{number}: {verification.get('verdict')}"
    return gh.ensure_handoff_issue(marker, title, body)


def event_message(event_name: str, payload: dict[str, Any]) -> str:
    if event_name == "issue_comment":
        return str((payload.get("comment") or {}).get("body", "")).strip()
    if "pull_request" in payload:
        sha = ((payload["pull_request"].get("head") or {}).get("sha") or "")
        return f"The pull request branch was updated (head {sha}). Re-review the current branch and address any verified remaining problems."
    return "The GitHub issue changed. Re-read the issue and continue using the latest requirements."


def persist(gh: GitHubClient, number: int, state: RouteState) -> None:
    """Repair derived labels only, after the canonical signed state is durable."""
    gh.setup_labels()
    gh.add_labels(number, [f"jules-owner:{state.owner}"])
    other = "b" if state.owner == "a" else "a"
    gh.remove_label(number, f"jules-owner:{other}")
    if state.needs_user:
        gh.add_labels(number, ["jules:needs-user"])
    else:
        gh.remove_label(number, "jules:needs-user")

    if state.verification == "certified":
        gh.add_labels(number, ["jules:certified"])
        for label in ("jules:changes-required", "agent:blocked", "chatgpt:handoff"):
            gh.remove_label(number, label)
    elif state.verification == "changes_required":
        gh.add_labels(number, ["jules:changes-required", "agent:blocked"])
        gh.remove_label(number, "jules:certified")
    elif state.verification in {"needs_evidence", "needs_user", "failed", "inconclusive"}:
        gh.add_labels(number, ["agent:blocked"])
        gh.remove_label(number, "jules:certified")

    if state.handoff_issue:
        gh.add_labels(number, ["chatgpt:handoff"])


_LOCKS: dict[tuple[str, int], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def event_id(event_name: str, payload: dict[str, Any]) -> str:
    obj = payload.get("pull_request") or payload.get("issue") or {}
    comment = payload.get("comment") or {}
    relevant = [event_name, payload.get("action"), obj.get("number"), obj.get("title"),
                obj.get("body"), obj.get("updated_at"), (obj.get("head") or {}).get("sha"),
                comment.get("id"), comment.get("body")]
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def route(config: Config, event_name: str, payload: dict[str, Any]) -> str:
    obj = payload.get("pull_request") or payload.get("issue") or {}
    key = (payload["repository"]["full_name"].lower(), int(obj["number"]))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    # Local threads are serialized; cross-run serialization is the workflow's contract.
    with lock:
        return _route(config, event_name, payload)


def _route(config: Config, event_name: str, payload: dict[str, Any]) -> str:
    repo = payload["repository"]["full_name"].lower()
    gh = GitHubClient(config.github_token, repo, dry_run=config.dry_run)
    if event_name not in {"issues", "pull_request", "issue_comment", "watch"}:
        return "ignored unsupported event"
    obj = payload.get("pull_request") or payload.get("issue") or {}
    number = int(obj["number"])
    labels = label_names(obj)
    command = event_name == "issue_comment"
    if command:
        message = event_message(event_name, payload)
        if not re.match(r"^/jules(?:\s*:\s*|\s+|$)", message):
            return "ignored issue comment without /jules"
        prompt = re.sub(r"^/jules(?:\s*:\s*|\s+)?", "", message).strip()
        if not prompt:
            return "ignored empty /jules command"
    elif "pull_request" in payload and not should_route_pr(obj, config):
        return "ignored PR: auto review disabled and jules:run absent"
    elif "pull_request" not in payload and "jules:run" not in labels and not owner_from_labels(labels):
        return "ignored issue: jules:run absent"
    if payload.get("action") == "labeled" and (payload.get("label") or {}).get("name") != "jules:run":
        return "ignored router bookkeeping label"
    if obj.get("state") == "closed":
        return "ignored closed issue or PR"
    if "pull_request" in payload:
        head_repo = (obj.get("head") or {}).get("repo") or {}
        if str(head_repo.get("full_name", repo)).lower() != repo:
            return "ignored fork PR: head branch is not in the routed source"
    sender = (payload.get("sender") or {}).get("login", "")
    if command and (payload.get("comment") or {}).get("user", {}).get("login") != sender:
        return "ignored command with mismatched author"
    if gh.can_write(sender) is not True:
        return "ignored event from actor without repository write permission"
    if "pull_request" in payload and str((obj.get("head") or {}).get("ref", "")).startswith("chatgpt/") and not config.dry_run:
        gh.add_labels(number, ["agent:chatgpt", "jules:run"])
        labels.update({"agent:chatgpt", "jules:run"})
    comments = gh.comments(number)
    writers = {sender}
    for row in comments:
        login = (row.get("user") or {}).get("login", "")
        if MARKER_PREFIX in (row.get("body") or "") and login and login not in writers and login != "github-actions[bot]":
            if gh.can_write(login) is True:
                writers.add(login)
    state, comment_id = parse_route_state(comments, repository=repo, number=number, keys=config.jules_keys, trusted_logins=writers)
    sticky = owner_from_labels(labels)
    if state and sticky and sticky != state.owner:
        raise ValueError("owner label disagrees with signed session; reconcile ownership")
    if command and not state:
        return "ignored /jules comment because no routed session exists"
    if not state and sticky:
        raise ValueError("owner label without verified session; reconcile before creating work")
    private = gh.is_private() if not state and config.home == "auto" else None
    owner = state.owner if state else choose_owner(labels, config, private)
    if owner not in config.allowed_owners:
        raise ValueError("sticky or selected owner is not permitted; no automatic transfer")
    if not config.jules_keys.get(owner):
        raise ValueError(f"missing API key for sticky owner {owner}")
    if config.dry_run:
        return f"dry-run: would route #{number} to Jules {owner}"
    if not config.serialized:
        raise ValueError("AGENT_ROUTER_SERIALIZED=true requires a per-repository/issue executor lock")
    ident = event_id(event_name, payload)

    def save(value: RouteState) -> None:
        nonlocal comment_id
        value.repository, value.number = repo, number
        if comment_id is None:
            created = gh.add_comment(number, "Preparing authenticated Jules routing state.")
            if type(created.get("id")) is not int:
                raise ValueError("state write has no confirmed comment ID; do not create a session")
            comment_id = created["id"]
        value.comment_id = comment_id
        gh.update_comment(comment_id, value.comment(key=config.jules_keys[value.owner]))

    def complete(value: RouteState) -> None:
        value.pending = ""
        if ident not in value.events:
            value.events.append(ident)
        save(value)  # Always save the external result before derived label updates.
        persist(gh, number, value)

    if state and state.pending:
        if state.pending == "create":
            client = JulesClient(config.jules_keys[state.owner])
            marker = f"[github-agent-router operation:{state.operation}]"
            matches = [x for x in client.list_sessions()
                       if marker in x.get("prompt", "")
                       and (x.get("sourceContext") or {}).get("source") == state.source]
            if len(matches) == 1:
                found = matches[0]
                state.session = found["name"]
                state.state = str(found.get("state", ""))
                state.url = str(found.get("url", ""))
                state.pending = ""
                # Record the ORIGINAL operation, not the event which discovered recovery.
                if state.operation_event not in state.events:
                    state.events.append(state.operation_event)
                save(state)
                persist(gh, number, state)
                return f"recovered session {state.session}; no creation retried"
        raise ValueError("pending external operation is uncertain; reconcile it before retrying")
    if state and ident in state.events:
        persist(gh, number, state)
        return "ignored already processed event; repaired labels"
    if state and len(state.events) >= 256:
        raise ValueError("event history limit reached; archive/reconcile state before continuing")
    if state:
        client = JulesClient(config.jules_keys[state.owner])
        session = client.get_session(state.session)
        state.state = str(session.get("state", ""))
        state.url = str(session.get("url", state.url))
        if command:
            if state.state in TERMINAL_STATES or state.state not in ACTIVE_STATES | {"AWAITING_USER_FEEDBACK"}:
                return "ignored command for terminal or unknown session state"
        elif event_name == "watch" and state.state in ACTIVE_STATES:
            return f"pending Jules verification: {state.state}"
        elif state.state == "AWAITING_USER_FEEDBACK":
            if state.feedback_nudges < 1:
                state.feedback_nudges += 1
                state.pending, state.operation = "send", uuid.uuid4().hex
                state.operation_event = ident
                save(state)
                client.send_message(state.session, AUTONOMOUS_UNBLOCK_PROMPT)
                state.needs_user = False
                complete(state)
                return f"nudged {state.session} to classify the blocker before asking the user"
            verification = extract_verification(client.list_activities(state.session)) or {
                "verdict": "needs_user",
                "summary": "Jules remained in AWAITING_USER_FEEDBACK after one bounded autonomous unblock attempt.",
                "tests": [],
                "red_team": [],
                "requests": ["Review the Jules session and answer only the material decision it cannot infer."],
            }
            state.verification = "needs_user"
            state.needs_user = True
            state.handoff_issue = ensure_handoff(gh, payload, state, verification)
            complete(state)
            return f"needs user; surfaced in handoff issue #{state.handoff_issue}"
        elif state.state == "AWAITING_PLAN_APPROVAL":
            verification = {
                "verdict": "needs_user",
                "summary": "Jules unexpectedly requires plan approval although router sessions normally auto-approve plans.",
                "tests": [],
                "red_team": [],
                "requests": ["Review/approve the Jules plan or correct the session configuration."],
            }
            state.verification = "needs_user"
            state.needs_user = True
            state.handoff_issue = ensure_handoff(gh, payload, state, verification)
            complete(state)
            return f"needs plan approval; surfaced in handoff issue #{state.handoff_issue}"
        elif state.state not in ACTIVE_STATES | TERMINAL_STATES:
            verification = {
                "verdict": "needs_evidence",
                "summary": f"Jules entered unknown state {state.state}.",
                "tests": [],
                "red_team": [],
                "requests": ["Inspect the Jules session state and router logs; do not restart or transfer ownership blindly."],
            }
            state.verification = "inconclusive"
            state.handoff_issue = ensure_handoff(gh, payload, state, verification)
            complete(state)
            return f"blocked on unknown Jules state; handoff issue #{state.handoff_issue}"
        elif state.state in TERMINAL_STATES:
            verification = None
            if state.state == "COMPLETED":
                try:
                    verification = extract_verification(client.list_activities(state.session))
                except ValueError as exc:
                    verification = {
                        "verdict": "needs_evidence",
                        "summary": f"Jules returned a malformed certification record: {exc}",
                        "tests": [],
                        "red_team": [],
                        "requests": ["Inspect the terminal Jules activity and produce a valid structured verification verdict."],
                    }
                    state.verification = "inconclusive"
                    state.handoff_issue = ensure_handoff(gh, payload, state, verification)
                    complete(state)
                    return f"inconclusive verification; handoff issue #{state.handoff_issue}"
                if verification:
                    state.verification = str(verification["verdict"])
                    if state.verification == "certified":
                        state.needs_user = False
                        state.handoff_issue = 0
                        complete(state)
                        return f"certified by Jules: {verification['summary']}"
                    state.needs_user = state.verification == "needs_user"
                    state.handoff_issue = ensure_handoff(gh, payload, state, verification)
                    complete(state)
                    if state.verification == "changes_required":
                        return f"changes required; handoff issue #{state.handoff_issue}"
                    if state.verification == "needs_evidence":
                        return f"needs local evidence; handoff issue #{state.handoff_issue}"
                    return f"needs user; surfaced in handoff issue #{state.handoff_issue}"
            if state.round >= config.max_rounds:
                verification = verification or {
                    "verdict": "needs_evidence",
                    "summary": f"Jules ended in {state.state} without a valid certification after {state.round} round(s).",
                    "tests": [],
                    "red_team": [],
                    "requests": ["Inspect Jules terminal activities and the source PR; determine whether to repair or re-run verification."],
                }
                state.verification = "failed" if state.state != "COMPLETED" else "inconclusive"
                state.handoff_issue = ensure_handoff(gh, payload, state, verification)
                complete(state)
                return f"verification blocked at max rounds; handoff issue #{state.handoff_issue}"
        if command or state.state in ACTIVE_STATES:
            state.pending, state.operation = "send", uuid.uuid4().hex
            state.operation_event = ident
            save(state)
            client.send_message(state.session, prompt if command else event_message(event_name, payload))
            state.needs_user = False
            complete(state)
            return f"sent message to {state.session}" if command else f"continued active sticky session {state.session}"
    title, prompt, branch, _, _ = build_prompt(payload)
    if state and state.state == "COMPLETED":
        prompt += "\n\nThe previous round completed without a valid verification marker. Re-check the current branch and finish with the required structured verdict."
    elif state and state.state in {"FAILED", "CANCELLED", "CANCELED"}:
        prompt += f"\n\nThe previous verification round ended in {state.state}. Re-run only the necessary verification and produce the required structured verdict."
    next_round = state.round + 1 if state else 1
    repo_owner, repo_name = repo.split("/", 1)
    client = JulesClient(config.jules_keys[owner])
    try:
        source = client.find_source(repo_owner, repo_name)
    except (JulesError, LookupError) as exc:
        alternate = _overflow_owner(config, owner)
        if state or not alternate or alternate == owner or alternate not in config.allowed_owners or not config.jules_keys.get(alternate):
            raise
        if isinstance(exc, JulesError) and exc.status != 429:
            raise
        owner = alternate
        client = JulesClient(config.jules_keys[owner])
        source = client.find_source(repo_owner, repo_name)
    new = RouteState(owner, "", round=next_round, repository=repo, number=number,
                     events=list(state.events) if state else [], pending="create",
                     operation=uuid.uuid4().hex, source=source, branch=branch, operation_event=ident)
    save(new)
    # No blind POST retries or cross-account fallback after this durable reservation.
    session = client.create_session(source=source, branch=branch, title=title,
                                    prompt=prompt + f"\n\n[github-agent-router operation:{new.operation}]",
                                    auto_create_pr=True)
    new.session, new.state = session["name"], str(session.get("state", ""))
    new.url = str(session.get("url", ""))
    complete(new)
    return f"created sticky follow-up {new.session}" if state else f"created {new.session} on Jules {new.owner}"


def watch_event(
    config: Config,
    event_name: str,
    payload: dict[str, Any],
    *,
    timeout: float = 1800,
    interval: float = 30,
) -> str:
    """Boundedly wait for a routed PR to reach a certified or explicit blocked verdict."""
    if event_name != "pull_request":
        return "verification watch not applicable to this event"
    if not should_route_pr(payload.get("pull_request") or {}, config):
        return "verification watch not required for an unrouted PR"
    deadline = time.monotonic() + max(0.0, timeout)
    attempt = 0
    while True:
        probe = json.loads(json.dumps(payload))
        probe["action"] = f"watch-{attempt}"
        result = route(config, "watch", probe)
        if result.startswith("certified by Jules:"):
            return result
        if result.startswith((
            "changes required;",
            "needs local evidence;",
            "needs user;",
            "needs plan approval;",
            "blocked on unknown Jules state;",
            "inconclusive verification;",
            "verification blocked at max rounds;",
        )):
            raise RuntimeError(result)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Jules certification did not settle within {timeout:g}s; last state: {result}")
        attempt += 1
        time.sleep(max(0.1, interval))


def load_event() -> tuple[str, dict[str, Any]]:
    event_name = os.getenv("GITHUB_EVENT_NAME", "").strip()
    raw = os.getenv("GITHUB_EVENT_JSON", "")
    if raw:
        return event_name, json.loads(raw)
    path = os.getenv("GITHUB_EVENT_PATH", "")
    if not path:
        raise ValueError("GITHUB_EVENT_JSON or GITHUB_EVENT_PATH is required")
    with open(path, "r", encoding="utf-8") as fh:
        return event_name, json.load(fh)


def check_credentials(config: Config) -> int:
    """Legacy account-only probe; use CLI check for per-repository readiness."""
    print("Checking configured Jules accounts (not repository readiness)...")
    ok = any(config.jules_keys.values())
    for owner in ("a", "b"):
        key = config.jules_keys.get(owner, "")
        if not key:
            print(f"  [Jules {owner.upper()}] Not configured (JULES_{owner.upper()}_API_KEY is empty)")
            continue
        try:
            client = JulesClient(key)
            sources = client.list_sources()
            print(f"  [Jules {owner.upper()}] Authenticated - {len(sources)} source(s); selected repository not checked")
        except Exception as exc:
            print(f"  [Jules {owner.upper()}] Error: {exc}")
            ok = False

    if config.github_token:
        print("  [GitHub] Token present; authentication and repository permissions NOT verified")
    else:
        print("  [GitHub] GITHUB_TOKEN is not set")
    return 0 if ok else 1


def setup_repo_labels(config: Config, repo_full_name: str) -> None:
    """Ensure standard router labels exist on the specified repository."""
    if not config.github_token:
        raise ValueError("GITHUB_TOKEN is required to setup labels")
    if config.dry_run:
        print(f"dry-run: would reconcile labels on {repo_full_name}")
        return
    gh = GitHubClient(config.github_token, repo_full_name)
    ensured = gh.setup_labels()
    print(f"Successfully ensured {len(ensured)} labels on {repo_full_name}: {', '.join(ensured)}")


def generate_workflow_content(
    home: str = "auto", overflow: str = "", max_rounds: int = 2,
    auto_review_prs: bool = False, *, revision: str = "",
) -> str:
    """Compatibility renderer; production provisioning resolves inventory policy first."""
    from .inventory import RepositoryPolicy
    from .provisioning import ROUTER_REPOSITORY, render_workflow
    if home not in {"auto", "a", "b"} or overflow not in {"", "auto", "a", "b"}:
        raise ValueError("invalid executor selection")
    allowed = ("a", "b") if home == "auto" or overflow == "auto" else tuple(dict.fromkeys([home] + ([overflow] if overflow else [])))
    policy = RepositoryPolicy(ROUTER_REPOSITORY, 1, "main", True, "auto", home, overflow, allowed, max_rounds, auto_review_prs, True)
    return render_workflow(policy, revision)


def provision_repo(
    config: Config, repo_full_name: str, home: str = "auto", overflow: str = "",
    max_rounds: int = 2, auto_review_prs: bool = False, branch: str | None = None,
    *, revision: str = "",
) -> dict[str, Any]:
    from .provisioning import provision_repository
    if branch is not None:
        raise ValueError("provisioning chooses a dedicated chatgpt/ PR branch, never a direct target branch")
    if overflow == "auto":
        raise ValueError("batch provisioning requires an explicit permitted overflow account")
    policy = {"schema_version": 1, "defaults": {"home": home, "overflow": overflow, "max_rounds": max_rounds, "auto_review_prs": auto_review_prs}, "repositories": {}}
    return provision_repository(config, repo_full_name, policy, revision)


def main(argv: list[str] | None = None) -> None:
    from .cli import main as cli_main
    cli_main(argv)


if __name__ == "__main__":
    main()
