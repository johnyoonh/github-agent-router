from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Config
from .github import GitHubClient
from .jules import JulesClient, JulesError


MARKER_PREFIX = "<!-- github-agent-router:"
MARKER_RE = re.compile(r"<!-- github-agent-router:(\{.*?\}) -->", re.DOTALL)
ACTIVE_STATES = {"QUEUED", "PLANNING", "AWAITING_PLAN_APPROVAL", "IN_PROGRESS", "PAUSED"}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "CANCELED"}


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
            return state, int(comment["id"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("malformed bot-authored routing state; reconcile before continuing") from exc
    return None, None


def should_route_pr(pr: dict[str, Any], config: Config) -> bool:
    labels = label_names(pr)
    head = str((pr.get("head") or {}).get("ref", ""))
    if "agent:jules" in labels or head.startswith(("jules/", "jules-", "google-jules/")):
        return False
    return config.auto_review_prs or "jules:run" in labels or bool(owner_from_labels(labels))


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
        prompt = f"""Review GitHub PR #{number} in {repo['full_name']} on branch {branch}.

PR title: {pr.get('title', '')}
PR description:
{body}

Review against the stated intent and acceptance criteria. Inspect correctness, regressions, edge cases, security, error handling, concurrency, API/data compatibility, performance regressions, and missing tests.

Fix only verified problems. Add regression tests for behavioral bugs. Run relevant tests and static checks. Do not perform subjective cleanup or unrelated refactors. If product behavior is ambiguous or requires a user decision, ask for clarification rather than guessing.

If there are no meaningful problems, do not manufacture changes."""
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
    if event_name not in {"issues", "pull_request", "issue_comment"}:
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
        elif state.state in {"AWAITING_USER_FEEDBACK", "AWAITING_PLAN_APPROVAL"}:
            state.needs_user = True
            complete(state)
            return f"{state.session} awaits user feedback"
        elif state.state not in ACTIVE_STATES | TERMINAL_STATES:
            state.needs_user = True
            complete(state)
            return "stopped on unknown session state; preserved owner and round"
        elif state.state in TERMINAL_STATES:
            if state.round >= config.max_rounds:
                state.needs_user = True
                complete(state)
                return f"stopped at max Jules rounds ({config.max_rounds})"
        if command or state.state in ACTIVE_STATES:
            state.pending, state.operation = "send", uuid.uuid4().hex
            state.operation_event = ident
            save(state)
            client.send_message(state.session, prompt if command else event_message(event_name, payload))
            state.needs_user = False
            complete(state)
            return f"sent message to {state.session}" if command else f"continued active sticky session {state.session}"
    title, prompt, branch, _, _ = build_prompt(payload)
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
