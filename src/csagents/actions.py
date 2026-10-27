from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class PlannedAction:
    """One write an agent wants to make. The key names the business event, so the same event reached by
    two paths (a nightly run and a webhook) produces the same key and the ledger lets only one through."""

    action_type: str
    agent: str
    company_id: int
    company_name: str
    owner_id: int | None
    trigger: str
    payload: dict = field(default_factory=dict)
    arr: float = 0.0
    billing_flag: bool = False

    @property
    def key(self) -> str:
        return f"{self.action_type}:{self.company_id}:{self.trigger}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PlannedAction:
        return cls(**d)

    @property
    def customer_facing(self) -> bool:
        return self.action_type.startswith("send_")
