"""Batch commands with per-repository results and no implicit fleet activation."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os

from . import __version__
from .config import Config
from .github import GitHubClient
from .inventory import inventory_targets, read_policy, resolve_policy
from .provisioning import inspect_repository, provision_repository, readiness


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="github-agent-router")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("route", help="route the GitHub event from the environment")
    for command in ("plan", "check", "setup-labels", "provision"):
        sub = commands.add_parser(command)
        sub.add_argument("repositories", nargs="*")
        sub.add_argument("--manifest", help="repository inventory TSV; does not scan the user's machine")
        sub.add_argument("--policy", help="explicit routing policy JSON")
        sub.add_argument("--dry-run", action="store_true")
        if command == "provision":
            sub.add_argument("--router-ref", required=True, help="reviewed full commit SHA; both workflow and code are pinned")
    args = parser.parse_args(argv)
    config = Config.from_env()
    if args.command in {None, "route"}:
        from .router import load_event, route
        event, payload = load_event()
        print(route(config, event, payload))
        return
    document = read_policy(args.policy)
    repos = inventory_targets(args.repositories, args.manifest, document)
    if not repos:
        repos = inventory_targets([os.environ["GITHUB_REPOSITORY"]], None, document) if os.getenv("GITHUB_REPOSITORY") else []
    if not repos:
        parser.error("provide repositories, --manifest, or GITHUB_REPOSITORY")
    config = replace(config, dry_run=config.dry_run or args.dry_run)
    results = []
    for repository in repos:
        try:
            if args.command == "plan":
                result = inspect_repository(config, repository, document)
            elif args.command == "provision":
                result = provision_repository(config, repository, document, args.router_ref)
            else:
                gh = GitHubClient(config.github_token, repository, dry_run=config.dry_run)
                info = gh.get_repo()
                policy = resolve_policy(info, document)
                if not policy.enabled:
                    result = {"repository": policy.repository, "status": "disabled"}
                elif info.get("archived"):
                    raise ValueError("archived repository requires explicit unenrollment")
                elif args.command == "check":
                    readiness(gh, config, policy)
                    result = {"repository": policy.repository, "status": "prerequisites_checked",
                              "github_access": "verified", "jules_sources": list(policy.allowed_owners),
                              "stored_secret_values": "not_readable_or_verified", "live_workflow": "not_tested",
                              "workflow_compatible": policy.private}
                elif config.dry_run:
                    result = {"repository": policy.repository, "status": "would_reconcile_labels"}
                else:
                    if not config.github_token:
                        raise ValueError("GITHUB_TOKEN is required to reconcile labels")
                    result = {"repository": policy.repository, "status": "labels_reconciled", "labels": gh.setup_labels()}
            results.append(result)
        except Exception as exc:
            # Continue independent targets; failures never become a successful batch.
            results.append({"repository": repository, "status": "blocked", "error": str(exc)})
    print(json.dumps({"schema_version": 1, "version": __version__, "results": results}, indent=2))
    if any(x["status"] == "blocked" for x in results):
        raise SystemExit(1)
