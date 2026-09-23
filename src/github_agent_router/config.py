from __future__ import annotations

from dataclasses import dataclass, field
import os


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    github_token: str = field(repr=False)
    jules_keys: dict[str, str] = field(repr=False)
    home: str = "auto"
    overflow: str | None = None
    private_home: str = "a"
    public_home: str = "b"
    max_rounds: int = 2
    auto_review_prs: bool = False
    dry_run: bool = False
    serialized: bool = False
    allowed_owners: tuple[str, ...] = ("a", "b")

    def __post_init__(self) -> None:
        if self.home not in {"auto", "a", "b"} or self.overflow not in {None, "auto", "a", "b"}:
            raise ValueError("home/overflow must be auto, a, b, or an empty overflow")
        if self.private_home not in {"a", "b"} or self.public_home not in {"a", "b"}:
            raise ValueError("private/public home must be a or b")
        if type(self.max_rounds) is not int or self.max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer")
        if not self.allowed_owners or set(self.allowed_owners) - {"a", "b"}:
            raise ValueError("allowed_owners must contain a and/or b")

    @classmethod
    def from_env(cls) -> "Config":
        keys = {
            "a": os.getenv("JULES_A_API_KEY", "").strip(),
            "b": os.getenv("JULES_B_API_KEY", "").strip(),
        }
        overflow = os.getenv("JULES_OVERFLOW", "").strip().lower() or None
        return cls(
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            jules_keys=keys,
            home=os.getenv("JULES_HOME", "auto").strip().lower() or "auto",
            overflow=overflow,
            private_home=os.getenv("JULES_PRIVATE_HOME", "a").strip().lower() or "a",
            public_home=os.getenv("JULES_PUBLIC_HOME", "b").strip().lower() or "b",
            max_rounds=max(1, int(os.getenv("JULES_MAX_ROUNDS", "2"))),
            auto_review_prs=_bool("JULES_AUTO_REVIEW_PRS", False),
            dry_run=_bool("AGENT_ROUTER_DRY_RUN", False),
            serialized=_bool("AGENT_ROUTER_SERIALIZED", False),
            allowed_owners=tuple(x.strip().lower() for x in os.getenv("JULES_ALLOWED_OWNERS", "a,b").split(",") if x.strip()),
        )
