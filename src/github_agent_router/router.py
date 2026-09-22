from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
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

    def comment(self) -> str:
        meta = {
            "owner": self.owner,
            "session": self.session,
            "url": self.url,
            "round": self.round,
            "state": self.state,
        }
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
    for owner in ("a", "b"):
        if f"jules-owner:{owner}" in labels:
            return owner
    return None


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


def parse_route_state(comments: list[dict[str, Any]]) -> tuple[RouteState | None, int | None]:
    for comment in reversed(comments):
        body = comment.get("body") or ""
        match = MARKER_RE.search(body)
        if not match:
            continue
        try:
            raw = json.loads(match.group(1))
            return RouteState(
                owner=str(raw["owner"]),
                session=str(raw["session"]),
                url=str(raw.get("url", "")),
                round=int(raw.get("round", 1)),
                state=str(raw.get("state", "")),
            ), int(comment["id"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            continue
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
    gh.setup_labels()
    gh.upsert_router_comment(number, MARKER_PREFIX, state.comment())
    gh.add_labels(number, [f"jules-owner:{state.owner}"])
    other = "b" if state.owner == "a" else "a"
    gh.remove_label(number, f"jules-owner:{other}")


def create_for_owner(
    *,
    owner: str,
    config: Config,
    repo_full_name: str,
    branch: str,
    title: str,
    prompt: str,
    round_number: int,
) -> RouteState:
    client = JulesClient(config.jules_keys.get(owner, ""))
    repo_owner, repo_name = repo_full_name.split("/", 1)
    source = client.find_source(repo_owner, repo_name)
    session = client.create_session(
        source=source,
        branch=branch,
        title=title,
        prompt=prompt,
        auto_create_pr=True,
    )
    return RouteState(
        owner=owner,
        session=session["name"],
        url=str(session.get("url", "")),
        round=round_number,
        state=str(session.get("state", "")),
    )


def route(config: Config, event_name: str, payload: dict[str, Any]) -> str:
    repo_full_name = payload["repository"]["full_name"]
    gh = GitHubClient(config.github_token, repo_full_name)

    if event_name == "issue_comment":
        issue = payload.get("issue") or {}
        number = int(issue["number"])
        message = event_message(event_name, payload)
        if not re.match(r"^/jules(?:\s*:\s*|\s+|$)", message):
            return "ignored issue comment without /jules"
        state, _ = parse_route_state(gh.comments(number))
        if not state:
            return "ignored /jules comment because no routed session exists"
        if not config.jules_keys.get(state.owner):
            raise ValueError(f"missing API key for sticky owner {state.owner}")
        prompt = re.sub(r"^/jules(?:\s*:\s*|\s+)?", "", message).strip()
        if not prompt:
            return "ignored empty /jules command"
        JulesClient(config.jules_keys[state.owner]).send_message(state.session, prompt)
        gh.remove_label(number, "jules:needs-user")
        return f"sent message to {state.session}"

    title, prompt, branch, number, labels = build_prompt(payload)

    if "pull_request" in payload and not should_route_pr(payload["pull_request"], config):
        return "ignored PR: auto review disabled and jules:run absent"

    if "pull_request" not in payload and "jules:run" not in labels and not owner_from_labels(labels):
        return "ignored issue: jules:run absent"

    state, _ = parse_route_state(gh.comments(number))
    sticky_owner = owner_from_labels(labels) or (state.owner if state else None)

    if state:
        if not config.jules_keys.get(state.owner):
            raise ValueError(f"missing API key for sticky owner {state.owner}")
        client = JulesClient(config.jules_keys[state.owner])
        session = client.get_session(state.session)
        session_state = str(session.get("state", ""))
        state.state = session_state
        state.url = str(session.get("url", state.url))

        if session_state == "AWAITING_USER_FEEDBACK":
            gh.add_labels(number, ["jules:needs-user"])
            persist(gh, number, state)
            return f"{state.session} awaits user feedback"

        if session_state in TERMINAL_STATES:
            if state.round >= config.max_rounds:
                gh.add_labels(number, ["jules:needs-user"])
                persist(gh, number, state)
                return f"stopped at max Jules rounds ({config.max_rounds})"
            next_state = create_for_owner(
                owner=state.owner,
                config=config,
                repo_full_name=repo_full_name,
                branch=branch,
                title=f"{title} (review round {state.round + 1})",
                prompt=prompt + "\n\nThis is a follow-up review. Re-check the current branch after the previous Jules round.",
                round_number=state.round + 1,
            )
            persist(gh, number, next_state)
            return f"created sticky follow-up {next_state.session}"

        # Otherwise continue active session (ACTIVE_STATES or other non-terminal states)
        client.send_message(state.session, event_message(event_name, payload))
        persist(gh, number, state)
        return f"continued active sticky session {state.session}"

    repository_private = None
    if not sticky_owner and config.home == "auto":
        repository_private = gh.is_private()
    owner = sticky_owner or choose_owner(labels, config, repository_private=repository_private)
    if config.dry_run:
        return f"dry-run: would route #{number} to Jules {owner}"

    try:
        new_state = create_for_owner(
            owner=owner,
            config=config,
            repo_full_name=repo_full_name,
            branch=branch,
            title=title,
            prompt=prompt,
            round_number=1,
        )
    except (JulesError, LookupError) as exc:
        # Overflow is allowed only for never-claimed work.
        overflow_owner = _overflow_owner(config, owner)
        if sticky_owner or not overflow_owner or overflow_owner == owner or not config.jules_keys.get(overflow_owner):
            raise
        if isinstance(exc, JulesError) and exc.status != 429:
            raise
        owner = overflow_owner
        new_state = create_for_owner(
            owner=owner,
            config=config,
            repo_full_name=repo_full_name,
            branch=branch,
            title=title,
            prompt=prompt,
            round_number=1,
        )

    persist(gh, number, new_state)
    return f"created {new_state.session} on Jules {new_state.owner}"


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
    """Verify configured Jules API keys and GitHub token."""
    print("Checking configured credentials...")
    ok = True
    for owner in ("a", "b"):
        key = config.jules_keys.get(owner, "")
        if not key:
            print(f"  [Jules {owner.upper()}] Not configured (JULES_{owner.upper()}_API_KEY is empty)")
            continue
        try:
            client = JulesClient(key)
            sources = client.list_sources()
            names = [
                f"{s.get('githubRepo', {}).get('owner', '')}/{s.get('githubRepo', {}).get('repo', '')}"
                for s in sources
                if s.get("githubRepo")
            ]
            sample = f" (repos: {', '.join(names[:3])}{'...' if len(names) > 3 else ''})" if names else ""
            print(f"  [Jules {owner.upper()}] OK - {len(sources)} source(s) accessible{sample}")
        except Exception as exc:
            print(f"  [Jules {owner.upper()}] Error: {exc}")
            ok = False

    if config.github_token:
        print("  [GitHub] GITHUB_TOKEN is set")
    else:
        print("  [GitHub] GITHUB_TOKEN is not set")
        ok = False
    return 0 if ok else 1


def setup_repo_labels(config: Config, repo_full_name: str) -> None:
    """Ensure standard router labels exist on the specified repository."""
    if not config.github_token:
        raise ValueError("GITHUB_TOKEN is required to setup labels")
    gh = GitHubClient(config.github_token, repo_full_name)
    ensured = gh.setup_labels()
    print(f"Successfully ensured {len(ensured)} labels on {repo_full_name}: {', '.join(ensured)}")


def generate_workflow_content(
    home: str = "auto",
    overflow: str = "auto",
    max_rounds: int = 2,
    auto_review_prs: bool = False,
    private_home: str = "a",
    public_home: str = "b",
) -> str:
    overflow_val = f'"{overflow}"' if overflow else '""'
    auto_review_val = "true" if auto_review_prs else "false"
    return f"""name: Jules Router

on:
  issues:
    types: [opened, edited, labeled, reopened]
  pull_request:
    types: [opened, synchronize, reopened, labeled]
  issue_comment:
    types: [created]

concurrency:
  group: jules-router-${{{{ github.repository }}}}-${{{{ github.event.issue.number || github.event.pull_request.number || github.run_id }}}}
  cancel-in-progress: false

jobs:
  jules:
    uses: johnyoonh/github-agent-router/.github/workflows/reusable-router.yml@main
    with:
      home: {home}
      overflow: {overflow_val}
      private_home: {private_home}
      public_home: {public_home}
      max_rounds: {max_rounds}
      auto_review_prs: {auto_review_val}
    secrets:
      jules_a_api_key: ${{{{ secrets.JULES_A_API_KEY }}}}
      jules_b_api_key: ${{{{ secrets.JULES_B_API_KEY }}}}
"""


def provision_repo(
    config: Config,
    repo_full_name: str,
    home: str = "auto",
    overflow: str = "auto",
    max_rounds: int = 2,
    auto_review_prs: bool = False,
    branch: str | None = None,
    private_home: str = "a",
    public_home: str = "b",
) -> None:
    """Provisions a target repository on GitHub: ensures labels and commits workflow."""
    if not config.github_token:
        raise ValueError("GITHUB_TOKEN is required to provision repository")
    gh = GitHubClient(config.github_token, repo_full_name)
    print(f"Provisioning {repo_full_name}...")
    ensured = gh.setup_labels()
    print(f"  [Labels] Ensured {len(ensured)} labels: {', '.join(ensured)}")

    workflow_content = generate_workflow_content(
        home,
        overflow,
        max_rounds,
        auto_review_prs,
        private_home,
        public_home,
    )
    workflow_path = ".github/workflows/jules-router.yml"
    gh.put_file(
        path=workflow_path,
        content=workflow_content,
        message="ci: add Jules router workflow",
        branch=branch,
    )
    print(f"  [Workflow] Created/updated {workflow_path}")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    config = Config.from_env()

    if args and args[0] in ("--help", "-h", "help"):
        print("Usage:")
        print("  github-agent-router                                Route GitHub event from environment")
        print("  github-agent-router check                          Verify Jules API keys and GitHub token")
        print("  github-agent-router setup-labels <repo> [repo2...] Ensure required labels exist on repo(s)")
        print("  github-agent-router provision <repo> [repo2...]    Ensure labels and install visibility-aware jules-router.yml")
        return

    if args and args[0] == "check":
        sys.exit(check_credentials(config))

    if args and args[0] == "setup-labels":
        repos = args[1:]
        if not repos:
            repo = os.getenv("GITHUB_REPOSITORY", "")
            if not repo:
                print("Usage: github-agent-router setup-labels <owner/repo> [repo2...]", file=sys.stderr)
                sys.exit(1)
            repos = [repo]
        for r in repos:
            setup_repo_labels(config, r)
        return

    if args and args[0] == "provision":
        repos = args[1:]
        if not repos:
            repo = os.getenv("GITHUB_REPOSITORY", "")
            if not repo:
                print("Usage: github-agent-router provision <owner/repo> [repo2...]", file=sys.stderr)
                sys.exit(1)
            repos = [repo]
        for r in repos:
            provision_repo(
                config,
                r,
                home=config.home,
                overflow=config.overflow or "",
                max_rounds=config.max_rounds,
                auto_review_prs=config.auto_review_prs,
                private_home=config.private_home,
                public_home=config.public_home,
            )
        return

    try:
        event_name, payload = load_event()
        print(route(config, event_name, payload))
    except Exception as exc:
        print(f"github-agent-router: {exc}", file=sys.stderr)
        raise



if __name__ == "__main__":
    main()
