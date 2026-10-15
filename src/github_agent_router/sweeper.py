"""One bounded external sweep; run from a trusted scheduler, not a waiting Actions job."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time

from .certification import ACTIVE, read_state
from .merge_control import pages


@contextmanager
def lease(path):
    lock = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("sweeper already leased; reconcile stale lease explicitly") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def sweep(gh, keys, factory, checkpoint, *, now, budget=5, dry_run=False):
    dispatched = 0
    results = []
    for pr in pages(gh, f"/repos/{gh.repository}/pulls?state=open"):
        number = pr["number"]
        labels = {x["name"] for x in pr.get("labels", [])}
        if not any(x.startswith("jules-owner:") for x in labels):
            continue
        state = read_state(gh, number, keys)
        if not state:
            continue
        current = (state.get("head_sha") == pr["head"]["sha"] and
                   state.get("base_sha") == pr["base"]["sha"])
        if current and state.get("verification") in {
                "certified", "changes_required", "needs_evidence", "needs_user"}:
            results.append({"pr": number, "status": "settled"})
            continue
        identity = f"{gh.repository}#{number}"
        entry = checkpoint.get(identity, {})
        fingerprint = hashlib.sha256(json.dumps(
            {"state": state, "head_sha": pr["head"]["sha"], "base_sha": pr["base"]["sha"]},
            sort_keys=True).encode()).hexdigest()
        if entry.get("reserved_fingerprint") == fingerprint:
            results.append({"pr": number, "status": "dispatch_reserved_or_uncertain"})
            continue
        if now < max(entry.get("due", 0), state.get("next_probe_at", 0)):
            continue
        if state.get("pending"):
            results.append({"pr": number, "status": "reconciliation_required"})
            continue
        session = factory(keys[state["owner"]]).get_session(state["session"])
        attempt = min(entry.get("attempt", 0) + 1, 5)
        next_entry = {"attempt": attempt, "due": now + min(30 * 2 ** attempt, 900)}
        if session.get("state") in ACTIVE:
            if not dry_run:
                checkpoint[identity] = next_entry
            results.append({"pr": number, "status": "pending"})
            continue
        if dispatched >= budget:
            break
        if dry_run:
            results.append({"pr": number, "status": "would_dispatch"})
            dispatched += 1
            continue
        next_entry["reserved_fingerprint"] = fingerprint
        checkpoint[identity] = next_entry
        # Caller persists this reservation before POST, so timeouts never cause blind retries.
        checkpoint.flush()
        gh._request("POST", f"/repos/{gh.repository}/dispatches", {
            "event_type": "jules-cert-sweep", "client_payload": {
                "pr_number": number, "head_sha": pr["head"]["sha"],
                "base_sha": pr["base"]["sha"], "session": state["session"]}})
        results.append({"pr": number, "status": "dispatched"})
        dispatched += 1
    return results


class Checkpoint(dict):
    def __init__(self, path):
        self.path = path
        super().__init__(json.loads(path.read_text()) if path.exists() else {})
    def flush(self):
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--max-dispatches", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.max_dispatches <= 20:
        parser.error("max-dispatches must be between 1 and 20")
    from .config import Config
    from .github import GitHubClient
    from .jules import JulesClient
    config = Config.from_env()
    with lease(args.state):
        checkpoint = Checkpoint(args.state)
        gh = GitHubClient(config.github_token, args.repository.lower(), dry_run=args.dry_run)
        results = sweep(gh, config.jules_keys, JulesClient, checkpoint,
                        now=time.time(), budget=args.max_dispatches, dry_run=args.dry_run)
        if not args.dry_run:
            checkpoint.flush()
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
