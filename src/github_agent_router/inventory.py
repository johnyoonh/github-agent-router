"""Read-only inventory and explicit policy. Directory names never imply privacy."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


ROOTS = {"repos", "github", "chrome", "obsidian"}


def repository_name(remote: str) -> str:
    value = remote.strip()
    if value.startswith("git@github.com:"):
        value = value[len("git@github.com:"):]
    elif "://" in value:
        url = urlsplit(value)
        if url.hostname != "github.com" or url.scheme not in {"https", "ssh"} or url.password or url.query or url.fragment:
            raise ValueError("unsupported GitHub remote")
        if url.username not in {None, "git"} or url.port is not None:
            raise ValueError("credential-bearing or nonstandard remote is not permitted")
        value = url.path.lstrip("/")
    value = value.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("repository must be owner/name or a GitHub remote")
    return value.lower()


def read_manifest(path: str | Path) -> list[str]:
    names: set[str] = set()
    with Path(path).open(encoding="utf-8", newline="") as source:
        for line, row in enumerate(csv.reader(source, delimiter="\t"), 1):
            if not row or row[0].startswith("#"):
                continue
            if len(row) != 7 or row[0] not in ROOTS or row[4] not in {"yes", "no"} or row[5] not in {"yes", "no"}:
                raise ValueError(f"invalid repository manifest row {line}")
            try:
                names.add(repository_name(row[2]))
            except ValueError as exc:
                raise ValueError(f"invalid repository remote on row {line}") from exc
    return sorted(names)


POLICY_FIELDS = {"intended_visibility", "home", "overflow", "allowed_owners", "max_rounds", "auto_review_prs", "enabled", "repository_id"}


def read_policy(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {"schema_version": 1, "defaults": {}, "repositories": {}, "additional_repositories": []}
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate policy key: {key}")
            result[key] = value
        return result
    raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(raw, dict) or (type(raw.get("schema_version")) is not int or raw["schema_version"] != 1) or set(raw) - {"schema_version", "defaults", "repositories", "additional_repositories"}:
        raise ValueError("unsupported routing policy schema")
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - (POLICY_FIELDS - {"repository_id"}):
        raise ValueError("unknown routing defaults")
    overrides = raw.get("repositories", {})
    if not isinstance(overrides, dict):
        raise ValueError("repositories must be a policy mapping")
    normalized = {}
    for name, policy in overrides.items():
        name = repository_name(name)
        if name in normalized or not isinstance(policy, dict) or set(policy) - POLICY_FIELDS:
            raise ValueError("conflicting repository override or unknown policy field")
        normalized[name] = policy
    additional = raw.get("additional_repositories", [])
    if not isinstance(additional, list):
        raise ValueError("additional_repositories must be a list")
    raw["repositories"] = normalized
    raw["additional_repositories"] = sorted({repository_name(x) for x in additional})
    # Validate policy values even for currently absent/disabled entries.
    for policy in [defaults, *normalized.values()]:
        _validate(policy)
    return raw


def _validate(value: dict[str, Any]) -> None:
    if value.get("intended_visibility", "auto") not in {"auto", "private", "public", "to-be-public"}:
        raise ValueError("invalid intended_visibility")
    if value.get("home", "auto") not in {"auto", "a", "b"} or value.get("overflow", "") not in {"", "a", "b"}:
        raise ValueError("policy home must be auto/a/b; overflow must be explicit a/b or empty")
    for name in ("enabled", "auto_review_prs"):
        if name in value and type(value[name]) is not bool:
            raise ValueError(f"{name} must be a boolean")
    for name in ("max_rounds", "repository_id"):
        if name in value and (type(value[name]) is not int or value[name] < 1):
            raise ValueError(f"{name} must be a positive integer")
    if "allowed_owners" in value:
        owners = value["allowed_owners"]
        if not isinstance(owners, list) or not owners or any(x not in {"a", "b"} for x in owners) or len(owners) != len(set(owners)):
            raise ValueError("allowed_owners must be a nonempty, unique list of a/b")


@dataclass(frozen=True)
class RepositoryPolicy:
    repository: str
    repository_id: int
    default_branch: str
    private: bool
    intended_visibility: str
    home: str
    overflow: str
    allowed_owners: tuple[str, ...]
    max_rounds: int
    auto_review_prs: bool
    enabled: bool


def resolve_policy(info: dict[str, Any], document: dict[str, Any]) -> RepositoryPolicy:
    name = repository_name(info["full_name"])
    values = {**document.get("defaults", {}), **document.get("repositories", {}).get(name, {})}
    _validate(values)
    private, repo_id, branch = info.get("private"), info.get("id"), info.get("default_branch")
    if type(private) is not bool or type(repo_id) is not int or repo_id < 1 or not isinstance(branch, str) or not branch:
        raise ValueError("repository identity, visibility, or default branch is unavailable")
    if values.get("repository_id", repo_id) != repo_id:
        raise ValueError("repository identity changed; review enrollment")
    intended = values.get("intended_visibility", "auto")
    if (intended == "private" and not private) or (intended == "public" and private):
        raise ValueError("visibility conflicts with policy; use to-be-public for explicit publication intent")
    home = values.get("home", "auto")
    if home == "auto":
        home = "b" if not private or intended == "to-be-public" else "a"
    overflow = values.get("overflow", "")
    if home == overflow:
        raise ValueError("overflow must differ from home")
    allowed = tuple(values.get("allowed_owners", [home] + ([overflow] if overflow else [])))
    if home not in allowed or (overflow and overflow not in allowed):
        raise ValueError("home/overflow is not in allowed_owners")
    return RepositoryPolicy(name, repo_id, branch, private, intended, home, overflow, allowed,
                            values.get("max_rounds", 2), values.get("auto_review_prs", False), values.get("enabled", True))


def inventory_targets(repos: list[str], manifest: str | None, document: dict[str, Any]) -> list[str]:
    if repos and manifest:
        raise ValueError("choose explicit repositories or --manifest, not both")
    if repos:
        return sorted({repository_name(x) for x in repos})
    names = set(read_manifest(manifest) if manifest else [])
    names.update(document.get("additional_repositories", []))
    # Overrides modify enrollment; they never silently enroll an unrelated repository.
    return sorted(names)
