"""Loads config/agents.yaml and config/health_weights.yaml into typed objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from . import settings


@dataclass(frozen=True)
class ActionSpec:
    name: str
    autonomy: str  # act, approve or never
    graduates: bool = False
    customer_facing: bool = False
    human_field: str | None = None


@dataclass(frozen=True)
class Config:
    launch_date: date
    shadow_days: int
    weights: dict[str, int]
    weights_meta: dict
    tiers: dict[str, int]
    ticket_window_days: int
    usage_weeks: int
    renewal_open_days: int
    expansion_threshold: float
    expansion_weeks: int
    usage_drop_fraction: float
    no_contact_days: int
    onboarding_days: list[int]
    ticket_followup_priority: str
    checkin_cooldown_days: int
    text_window_days: int
    qbr_min_arr: int
    qbr_prepare_days: int
    qbr_max_attempts: int
    arr_threshold: int
    graduation_window: int
    graduation_max_edit_rate: float
    actions: dict[str, ActionSpec] = field(default_factory=dict)

    def tier(self, score: int) -> str:
        if score >= self.tiers["green"]:
            return "green"
        if score >= self.tiers["yellow"]:
            return "yellow"
        return "red"


def load_config(config_dir: Path | None = None) -> Config:
    d = config_dir or settings.load().config_dir
    raw = yaml.safe_load((d / "agents.yaml").read_text(encoding="utf-8"))
    w = yaml.safe_load((d / raw["health"]["weights_file"]).read_text(encoding="utf-8"))
    actions = {name: ActionSpec(name=name, **spec) for name, spec in raw["actions"].items()}
    f, a = raw["followups"], raw["approval"]
    return Config(
        launch_date=raw["launch"]["date"],
        shadow_days=raw["launch"]["shadow_days"],
        weights=w["points"],
        weights_meta={k: v for k, v in w.items() if k != "points"},
        tiers=raw["health"]["tiers"],
        ticket_window_days=raw["health"]["ticket_window_days"],
        usage_weeks=raw["health"]["usage_weeks"],
        renewal_open_days=raw["renewals"]["open_days_before"],
        expansion_threshold=raw["expansion"]["seat_utilization_above"],
        expansion_weeks=raw["expansion"]["weeks"],
        usage_drop_fraction=f["usage_drop_fraction"],
        no_contact_days=f["no_contact_days"],
        onboarding_days=f["onboarding_days_after_go_live"],
        ticket_followup_priority=f["ticket_followup_priority"],
        checkin_cooldown_days=f["checkin_cooldown_days"],
        text_window_days=f["text_window_days"],
        qbr_min_arr=raw["qbr"]["min_arr"],
        qbr_prepare_days=raw["qbr"]["prepare_days_before"],
        qbr_max_attempts=raw["qbr"]["max_attempts"],
        arr_threshold=a["arr_threshold"],
        graduation_window=a["graduation"]["window"],
        graduation_max_edit_rate=a["graduation"]["max_edit_rate"],
        actions=actions,
    )


@lru_cache(maxsize=1)
def get() -> Config:
    return load_config()
