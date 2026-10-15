"""Batch commands and a one-shot delivery control plane; no implicit activation."""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
import os
import sys
from . import __version__


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "merge-evaluate":
        from .merge_control import main as merge_main
        raise SystemExit(merge_main(raw[1:]))
    if raw and raw[0] == "sweep":
        from .sweeper import main as sweep_main
        raise SystemExit(sweep_main(raw[1:]))
    parser = argparse.ArgumentParser(prog="github-agent-router")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("route", help="route once; PRs use revision-bound certification")
    commands.add_parser("probe", help="one authenticated event iteration; never waits or sleeps")
    commands.add_parser("merge-evaluate", help="evaluate signed evidence and native merge authorization")
    commands.add_parser("sweep", help="one external backoff sweep; no Actions idle polling")
    watch = commands.add_parser("watch", help="deprecated alias for one-shot PR probes; timeout/interval are ignored")
    watch.add_argument("--timeout", type=float, default=60)
    watch.add_argument("--interval", type=float, default=30)
    for command in ("plan", "check", "setup-labels", "provision"):
        sub = commands.add_parser(command)
        sub.add_argument("repositories", nargs="*")
        sub.add_argument("--manifest")
        sub.add_argument("--policy")
        sub.add_argument("--dry-run", action="store_true")
        if command == "provision":
            sub.add_argument("--router-ref", required=True, help="reviewed full SHA; workflow and code share one pin")
    args = parser.parse_args(raw)
    from .config import Config
    from .github import GitHubClient
    config = Config.from_env()
    if args.command in {None, "route", "probe", "watch"}:
        from .router import load_event, route
        from .certification import event_pr, probe
        event, payload = load_event()
        if event_pr(event, payload) is not None:
            # Includes legacy `watch` invocations: no second string-parsed certification engine.
            print(json.dumps(probe(config, event, payload), sort_keys=True))
        else:
            print(json.dumps({"execution": "success", "certification": "not_applicable",
                              "merge_ready": False, "detail": route(config, event, payload)}))
        return
    from .inventory import inventory_targets, read_policy, resolve_policy
    from .provisioning import inspect_repository, provision_repository, readiness
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
            results.append({"repository": repository, "status": "blocked", "error": str(exc)})
    print(json.dumps({"schema_version": 1, "version": __version__, "results": results}, indent=2))
    if any(x["status"] == "blocked" for x in results):
        raise SystemExit(1)
