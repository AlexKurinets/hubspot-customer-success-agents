"""The one HubSpot client every agent goes through.

It owns pacing (TokenBucket), retries with jittered exponential backoff on 429 and 5xx, and the typed
calls the agents need. After `max_attempts` failures it raises HubSpotUnavailable and the caller sends
the work to the dead-letter queue. The model never sees this object or the token.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator

import httpx

from .. import settings
from .ratelimit import TokenBucket

BATCH = 100
PAGE = 200

# HubSpot-defined association type ids used by the agents.
TASK_TO_COMPANY, TASK_TO_CONTACT = 192, 204
NOTE_TO_COMPANY, NOTE_TO_DEAL = 190, 214
EMAIL_TO_COMPANY, EMAIL_TO_CONTACT = 186, 198
DEAL_TO_COMPANY = 341


class HubSpotError(Exception):
    def __init__(self, status: int, message: str, category: str = ""):
        super().__init__(f"HTTP {status} {category}: {message}")
        self.status, self.category, self.message = status, category, message


class HubSpotUnavailable(HubSpotError):
    """Retries are exhausted; the work goes to the dead-letter queue."""


def assoc(to_id: int, type_id: int) -> dict:
    return {"to": {"id": str(to_id)}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}


class HubSpotClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, http: httpx.Client | None = None,
                 max_attempts: int = 5, backoff_base: float = 0.5, backoff_cap: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep, jitter: Callable[[], float] = random.random,
                 bucket: TokenBucket | None = None):
        s = settings.load()
        self.http = http or httpx.Client(base_url=base_url or s.hubspot_url, timeout=30)
        self.token = token or s.hubspot_token
        self.max_attempts, self.backoff_base, self.backoff_cap = max_attempts, backoff_base, backoff_cap
        self.sleep, self.jitter = sleep, jitter
        self.bucket = bucket or TokenBucket(sleep=sleep)
        self.stats: Counter = Counter()

    # ---------------------------------------------------------------- transport

    def request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        last = ""
        for attempt in range(1, self.max_attempts + 1):
            self.bucket.acquire()
            try:
                r = self.http.request(method, path, json=json, params=params,
                                      headers={"Authorization": f"Bearer {self.token}"})
            except httpx.TransportError as e:
                self.stats["server_errors"] += 1
                last = type(e).__name__
                rate_limited = False
            else:
                self.stats["requests"] += 1
                self.bucket.observe(r.headers)
                rate_limited = r.status_code == 429
                if rate_limited:
                    self.stats["rate_limited"] += 1
                elif r.status_code >= 500:
                    self.stats["server_errors"] += 1
                elif r.status_code >= 400:
                    body = r.json() if r.content else {}
                    raise HubSpotError(r.status_code, body.get("message", r.text), body.get("category", ""))
                else:
                    return r.json() if r.content else {}
                last = f"HTTP {r.status_code}"
            if attempt < self.max_attempts:
                delay = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
                delay = delay / 2 + self.jitter() * delay / 2
                if rate_limited:
                    delay = max(delay, self.bucket.interval_s / 10)
                self.stats["retries"] += 1
                self.sleep(delay)
        raise HubSpotUnavailable(599, f"{method} {path} failed after {self.max_attempts} attempts ({last})", "UNAVAILABLE")

    def take_stats(self) -> dict:
        out = dict(self.stats)
        out["throttle_waits"] = self.bucket.waits
        self.stats.clear()
        self.bucket.waits = 0
        return out

    # ---------------------------------------------------------------- objects

    def get(self, object_type: str, object_id: int, properties: Iterable[str] = (),
            history: Iterable[str] = ()) -> dict:
        params = {"properties": ",".join(properties)}
        if history:
            params["propertiesWithHistory"] = ",".join(history)
        return self.request("GET", f"/crm/v3/objects/{object_type}/{object_id}", params=params)

    def create(self, object_type: str, properties: dict, associations: list[dict] | None = None) -> dict:
        return self.request("POST", f"/crm/v3/objects/{object_type}",
                            json={"properties": properties, "associations": associations or []})

    def update(self, object_type: str, object_id: int, properties: dict) -> dict:
        return self.request("PATCH", f"/crm/v3/objects/{object_type}/{object_id}", json={"properties": properties})

    def batch_read(self, object_type: str, ids: list[int], properties: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            r = self.request("POST", f"/crm/v3/objects/{object_type}/batch/read",
                             json={"inputs": [{"id": str(x)} for x in chunk], "properties": properties})
            out.extend(r["results"])
        return out

    def batch_update(self, object_type: str, items: list[tuple[int, dict]]) -> int:
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            self.request("POST", f"/crm/v3/objects/{object_type}/batch/update",
                         json={"inputs": [{"id": str(x), "properties": p} for x, p in chunk]})
        return len(items)

    def batch_create(self, object_type: str, inputs: list[dict]) -> list[dict]:
        out = []
        for i in range(0, len(inputs), BATCH):
            r = self.request("POST", f"/crm/v3/objects/{object_type}/batch/create", json={"inputs": inputs[i:i + BATCH]})
            out.extend(r["results"])
        return out

    def search(self, object_type: str, filters: list[dict], properties: list[str], limit: int = PAGE,
               after: int = 0, sorts: list | None = None) -> dict:
        body = {"filterGroups": [{"filters": filters}] if filters else [], "properties": properties,
                "limit": limit, "after": str(after), "sorts": sorts or [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]}
        return self.request("POST", f"/crm/v3/objects/{object_type}/search", json=body)

    def search_all(self, object_type: str, filters: list[dict], properties: list[str]) -> Iterator[dict]:
        """Every match, however many. Search stops at 10,000 results per query, so instead of paging with
        `after` this sorts on hs_object_id and asks again for ids above the last one seen."""
        last = 0
        while True:
            page = self.search(object_type, [*filters, {"propertyName": "hs_object_id", "operator": "GT", "value": str(last)}],
                               properties)
            yield from page["results"]
            if len(page["results"]) < PAGE:
                return
            last = int(page["results"][-1]["id"])

    # ---------------------------------------------------------------- associations, owners, schema

    def associations(self, from_type: str, to_type: str, ids: list[int]) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            r = self.request("POST", f"/crm/v4/associations/{from_type}/{to_type}/batch/read",
                             json={"inputs": [{"id": str(x)} for x in chunk]})
            for res in r["results"]:
                out[int(res["from"]["id"])] = [int(t["toObjectId"]) for t in res["to"]]
        return out

    def owners(self) -> list[dict]:
        return self.request("GET", "/crm/v3/owners", params={"limit": 100})["results"]

    def property_exists(self, object_type: str, name: str) -> bool:
        try:
            self.request("GET", f"/crm/v3/properties/{object_type}/{name}")
            return True
        except HubSpotError as e:
            if e.status == 404:
                return False
            raise

    def ensure_group(self, object_type: str, name: str, label: str) -> bool:
        try:
            self.request("GET", f"/crm/v3/properties/{object_type}/groups/{name}")
            return False
        except HubSpotError as e:
            if e.status != 404:
                raise
        self.request("POST", f"/crm/v3/properties/{object_type}/groups", json={"name": name, "label": label})
        return True

    def create_property(self, object_type: str, spec: dict) -> None:
        self.request("POST", f"/crm/v3/properties/{object_type}", json=spec)
