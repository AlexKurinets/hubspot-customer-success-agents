"""Smoke test: every dashboard page renders against a live API and HubSpot stand-in over the seeded database."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import TEST_DSN

DASH = Path(__file__).resolve().parents[1] / "dashboard"
PAGES = ["views/overview.py", "views/approvals.py", "views/accounts.py", "views/reliability.py"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(target: str, env: dict) -> tuple[subprocess.Popen, str]:
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", target, "--port", str(port)],
                            env={**os.environ, **env})
    url = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                return proc, url
        except httpx.TransportError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"{target} did not start")


@pytest.fixture(scope="module")
def api_url(seeded):
    # Separate processes, so their connection pools don't share state with the test's.
    mock, mock_url = _serve("hubspot_mock.app:create_app", {"DATABASE_URL": TEST_DSN, "MOCK_BURST_LIMIT": "100000"})
    api, url = _serve("csagents.api:create_app", {"DATABASE_URL": TEST_DSN, "HUBSPOT_URL": mock_url})
    yield url
    for p in (api, mock):
        p.terminate()
        p.wait(10)


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(api_url, page, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("API_URL", api_url)
    monkeypatch.syspath_prepend(str(DASH))
    sys.modules.pop("views.common", None)
    at = AppTest.from_file(str(DASH / "app.py"), default_timeout=90)
    at.run()
    if page != PAGES[0]:
        at.switch_page(page)
        at.run()
    assert not at.exception, at.exception
    assert at.title and at.title[0].value
    if page == "views/overview.py":
        assert len(at.metric) >= 4
