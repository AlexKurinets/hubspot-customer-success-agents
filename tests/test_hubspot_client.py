"""The HubSpot client and the stand-in's HubSpot behaviours the agents rely on."""

import time

import pytest
from conftest import Stack, company

from csagents.hubspot.client import HubSpotClient, HubSpotError, HubSpotUnavailable
from csagents.hubspot.ratelimit import TokenBucket


def test_batches_stop_at_100_and_unknown_properties_fail(stack):
    with pytest.raises(HubSpotError) as e:
        stack.portal.request("POST", "/crm/v3/objects/companies/batch/read", json={"inputs": [{"id": str(i)} for i in range(101)]})
    assert e.value.status == 400
    cid = company(stack)
    with pytest.raises(HubSpotError) as e:
        stack.crm.update("companies", cid, {"cs_agent_mood": "fine"})
    assert "PROPERTY_DOESNT_EXIST" in e.value.message


def test_private_app_scopes_are_least_privilege(stack):
    cid = company(stack)
    with pytest.raises(HubSpotError) as e:
        stack.crm.create("tickets", {"subject": "x"})
    assert e.value.status == 403 and e.value.category == "MISSING_SCOPES"
    with pytest.raises(HubSpotError):
        stack.crm.update("contacts", 1, {"firstname": "x"})
    assert stack.crm.get("companies", cid, ["name"])["properties"]["name"]


def test_search_all_gets_past_the_result_cap():
    s = Stack(mock_env={"MOCK_SEARCH_CAP": "300"})
    try:
        s.portal.batch_create("companies", [{"properties": {"name": f"Clinic {i}", "cs_segment": "single-site"}}
                                            for i in range(450)])
        with pytest.raises(HubSpotError) as e:
            s.crm.search("companies", [], ["name"], limit=200, after=200)
        assert e.value.status == 400 and "limited to 300" in e.value.message
        found = list(s.crm.search_all("companies", [{"propertyName": "cs_segment", "operator": "EQ", "value": "single-site"}],
                                      ["name"]))
        assert len(found) == 450 and len({f["id"] for f in found}) == 450
    finally:
        s.close()


def test_retries_429_and_5xx_with_backoff_then_succeeds(stack):
    cid = company(stack)
    stack.mock.post("/_mock/faults", json={"status": 429, "count": 2, "match": f"/companies/{cid}"})
    stack.mock.post("/_mock/faults", json={"status": 502, "count": 1, "match": f"/companies/{cid}"})
    slept = []
    crm = HubSpotClient(http=stack.mock, token="pat-demo-cs-agents", sleep=slept.append, jitter=lambda: 0.5)
    assert crm.get("companies", cid, ["name"])["id"] == str(cid)
    assert crm.stats["retries"] == 3 and crm.stats["rate_limited"] == 2 and crm.stats["server_errors"] == 1
    # Two 429s wait at least a tenth of the rate-limit window; the 502 waits the jittered third backoff step.
    assert slept == [1.0, 1.0, 1.5]


def test_gives_up_after_five_attempts(stack):
    cid = company(stack)
    stack.mock.post("/_mock/faults", json={"status": 503, "count": 10, "match": f"/companies/{cid}"})
    crm = HubSpotClient(http=stack.mock, token="pat-demo-cs-agents", sleep=lambda s: None)
    with pytest.raises(HubSpotUnavailable):
        crm.get("companies", cid, ["name"])
    assert crm.stats["requests"] == 5


def test_token_bucket_keeps_the_client_under_the_burst_limit():
    """A one-second window of 20 requests: 60 calls finish with no 429 because the bucket waits first."""
    s = Stack(mock_env={"MOCK_BURST_LIMIT": "20", "MOCK_BURST_WINDOW_MS": "1000"}, sleep=time.sleep)
    try:
        cid = company(s)
        time.sleep(1.1)  # let the setup calls, made under the same app token, leave the window
        crm = HubSpotClient(http=s.mock, token="pat-demo-cs-agents", bucket=TokenBucket(max_requests=20, interval_s=1.0))
        for _ in range(60):
            crm.get("companies", cid, ["name"])
        assert crm.stats["rate_limited"] == 0
        assert crm.bucket.waits > 0
    finally:
        s.close()


def test_token_bucket_takes_its_limit_from_the_headers():
    clock = [0.0]
    waits = []
    def sleep(s):
        waits.append(s)
        clock[0] += s

    b = TokenBucket(max_requests=100, interval_s=10, clock=lambda: clock[0], sleep=sleep)
    b.observe({"X-HubSpot-RateLimit-Max": "10", "X-HubSpot-RateLimit-Interval-Milliseconds": "1000",
               "X-HubSpot-RateLimit-Remaining": "9"})
    # Allowance 9 of 10: a quarter as burst, the rest refilled over the one-second window, so no
    # rolling second can see more than 9.
    assert (b.capacity, b.rate) == (pytest.approx(2.25), pytest.approx(6.75))
    b.acquire()
    b.acquire()
    assert waits == []
    b.acquire()
    assert waits[0] == pytest.approx(0.75 / 6.75, rel=0.01)
    b.observe({"X-HubSpot-RateLimit-Max": "10", "X-HubSpot-RateLimit-Interval-Milliseconds": "1000",
               "X-HubSpot-RateLimit-Remaining": "1"})
    assert b.low and b.tokens == 0
    b.acquire()
    assert waits[-1] == pytest.approx(1.0)  # down to the margin: wait out the whole window


def test_property_history_records_who_changed_what(stack):
    cid = company(stack)
    stack.portal.request("PATCH", f"/crm/v3/objects/companies/{cid}", json={"properties": {"cs_arr": "13000"}})
    stack.crm.update("companies", cid, {"cs_agent_health_score": "71"})
    obj = stack.crm.get("companies", cid, ["cs_arr"], history=["cs_arr", "cs_agent_health_score"])
    assert obj["propertiesWithHistory"]["cs_arr"][0]["sourceType"] == "CRM_UI"
    assert obj["propertiesWithHistory"]["cs_agent_health_score"][0]["sourceType"] == "INTEGRATION"
