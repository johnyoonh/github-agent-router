from __future__ import annotations

from dataclasses import dataclass
import os


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    github_token: str
    jules_keys: dict[str, str]
    home: str = "a"
    overflow: str | None = "b"
    max_rounds: int = 2
    auto_review_prs: bool = False
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        keys = {
            "a": os.getenv("JULES_A_API_KEY", "").strip(),
            "b": os.getenv("JULES_B_API_KEY", "").strip(),
        }
        overflow = os.getenv("JULES_OVERFLOW", "b").strip() or None
        return cls(
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            jules_keys=keys,
            home=os.getenv("JULES_HOME", "a").strip().lower(),
            overflow=overflow.lower() if overflow else None,
            max_rounds=max(1, int(os.getenv("JULES_MAX_ROUNDS", "2"))),
            auto_review_prs=_bool("JULES_AUTO_REVIEW_PRS", False),
            dry_run=_bool("AGENT_ROUTER_DRY_RUN", False),
        )
