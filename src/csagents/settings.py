"""Runtime settings from the environment. Production read the HubSpot token and client secret from
AWS Secrets Manager; here they come from environment variables with demo defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DSN = "postgresql://cs:cs@localhost:5490/cs"


@dataclass(frozen=True)
class Settings:
    database_url: str
    hubspot_url: str
    hubspot_token: str
    hubspot_client_secret: str
    hubspot_app_id: str
    approver_token: str
    llm_provider: str
    llm_model: str
    llm_effort: str
    config_dir: Path


def load() -> Settings:
    here = Path(__file__).resolve()
    default_config = here.parents[2] / "config"
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DSN),
        hubspot_url=os.environ.get("HUBSPOT_URL", "http://localhost:8150"),
        hubspot_token=os.environ.get("HUBSPOT_TOKEN", "pat-demo-cs-agents"),
        hubspot_client_secret=os.environ.get("HUBSPOT_CLIENT_SECRET", "demo-client-secret"),
        hubspot_app_id=os.environ.get("HUBSPOT_APP_ID", "4100217"),
        approver_token=os.environ.get("APPROVER_TOKEN", "demo-csm"),
        llm_provider=os.environ.get("LLM_PROVIDER", "offline"),
        llm_model=os.environ.get("CS_LLM_MODEL", "claude-sonnet-5-5"),
        llm_effort=os.environ.get("CS_LLM_EFFORT", "low"),
        config_dir=Path(os.environ.get("CS_CONFIG_DIR", default_config)),
    )
