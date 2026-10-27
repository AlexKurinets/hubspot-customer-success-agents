"""Tests run against a real Postgres (TEST_DATABASE_URL). The HubSpot stand-in and the API run in-process
behind TestClient; webhooks from the stand-in are posted to the API exactly as over HTTP."""

from __future__ import annotations

import os
from datetime import date

import pytest

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://cs:cs@localhost:5491/cs_test")
os.environ["DATABASE_URL"] = TEST_DSN
os.environ["LLM_PROVIDER"] = "offline"

from fastapi.testclient import TestClient  # noqa: E402
from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402

from csagents import config, db  # noqa: E402
from csagents.agents.context import AgentContext  # noqa: E402
from csagents.api import create_app as create_api  # noqa: E402
from csagents.hubspot.client import HubSpotClient  # noqa: E402
from csagents.hubspot.schema import ensure_agent_properties  # noqa: E402
from csagents.llm.offline import OfflineLLM  # noqa: E402
from hubspot_mock.app import create_app as create_mock  # noqa: E402

LAUNCH = date(2026, 2, 2)
LIVE = date(2026, 2, 9)  # first day after the shadow week


def wipe() -> None:
    c = db.connect(TEST_DSN)
    c.execute("DROP SCHEMA IF EXISTS hs CASCADE")
    c.commit()
    db.reset(c)
    c.close()


class Stack:
    """The HubSpot stand-in and the API in-process, wired so webhooks flow from one to the other."""

    def __init__(self, mock_env: dict | None = None, sleep=lambda s: None):
        wipe()
        self.api_app = create_api(TEST_DSN, crm=None)
        self.api = TestClient(self.api_app, base_url="http://api").__enter__()
        env = {"DATABASE_URL": TEST_DSN, "MOCK_WEBHOOK_TARGET": "http://api/webhooks/hubspot",
               "MOCK_BURST_LIMIT": "100000", **(mock_env or {})}
        self.mock_app = create_mock(env, deliver=lambda url, headers, body: self.api.post(
            "/webhooks/hubspot", content=body, headers=headers).status_code)
        self.mock = TestClient(self.mock_app, base_url="http://hubspot").__enter__()
        self.crm = HubSpotClient(http=self.mock, token="pat-demo-cs-agents", sleep=sleep)
        self.portal = HubSpotClient(http=self.mock, token="pat-demo-portal-users", sleep=sleep)
        self.api_app.state.crm = self.crm
        ensure_agent_properties(self.crm)
        self.conn = db.connect(TEST_DSN)
        self.savers = (db.connect(TEST_DSN, autocommit=True), db.connect(TEST_DSN, autocommit=True))
        PostgresSaver(self.savers[0]).setup()

    def flush(self) -> dict:
        return self.mock.post("/_mock/webhooks/flush").json()

    def ctx(self, day: date, source: str = "nightly", llm=None) -> AgentContext:
        return AgentContext(self.conn, self.crm, llm or OfflineLLM(), config.get(), day, self.savers[0], source=source,
                            approval_saver_conn=self.savers[1])

    def close(self) -> None:
        for c in (self.conn, *self.savers):
            c.close()
        self.mock.__exit__(None, None, None)
        self.api.__exit__(None, None, None)


@pytest.fixture
def stack():
    s = Stack()
    yield s
    s.close()


def company(stack: Stack, name: str = "Wrenhollow Animal Hospital", arr: int = 12000, renewal: str = "2026-09-30",
            **extra) -> int:
    """One company with a practice manager, created the way people's edits arrive: with the portal token."""
    from csagents.hubspot.client import assoc

    props = {"name": name, "hubspot_owner_id": "7100001", "cs_arr": str(arr), "cs_renewal_date": renewal,
             "cs_seats_licensed": "12", "cs_go_live_date": "2024-05-01", **extra}
    cid = int(stack.portal.create("companies", props)["id"])
    stack.portal.create("contacts", {"firstname": "Rosa", "lastname": "Quinlan", "email": "rosa.quinlan@wrenhollow.example.com",
                                     "cs_role": "practice_manager"}, [assoc(cid, 279)])
    return cid


@pytest.fixture(scope="session")
def seeded():
    """150 accounts simulated from launch to 31 March 2026, shared by the end-to-end and dashboard tests."""
    from csagents.synthetic import simulate, world

    s = Stack()
    w = world.generate(s.conn, s.portal, LAUNCH, accounts=150, seed=11)
    errors = simulate.run(s.conn, s.savers, s.crm, s.portal, OfflineLLM(), config.get(), w, LAUNCH, date(2026, 3, 31),
                          s.flush, csms_until=date(2026, 3, 27))
    yield {"stack": s, "errors": errors, "world": w}
    s.close()
