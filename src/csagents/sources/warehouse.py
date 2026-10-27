"""Product usage. Production queried Snowflake; here the same queries run against the wh schema."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg


@dataclass(frozen=True)
class UsageWeek:
    week_start: date
    seats: int
    wau: int
    appointments: bool
    records: bool
    invoicing: bool

    @property
    def utilization(self) -> float:
        return self.wau / self.seats if self.seats else 0.0

    @property
    def modules(self) -> int:
        return int(self.appointments) + int(self.records) + int(self.invoicing)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def recent_usage(conn: psycopg.Connection, today: date, weeks: int,
                 company_ids: list[int] | None = None) -> dict[int, list[UsageWeek]]:
    """The last `weeks` complete weeks before `today`, oldest first, per company."""
    end = week_start(today)
    start = end - timedelta(weeks=weeks)
    sql = ("SELECT * FROM wh.usage_weekly WHERE week_start >= %s AND week_start < %s"
           + (" AND company_id = ANY(%s)" if company_ids is not None else "") + " ORDER BY company_id, week_start")
    params = [start, end] + ([company_ids] if company_ids is not None else [])
    out: dict[int, list[UsageWeek]] = defaultdict(list)
    for r in conn.execute(sql, params):
        out[r["company_id"]].append(UsageWeek(r["week_start"], r["seats_licensed"], r["weekly_active_users"],
                                              r["appointments_used"], r["records_used"], r["invoicing_used"]))
    return out


def company_ids(conn: psycopg.Connection) -> list[int]:
    return [r["company_id"] for r in conn.execute("SELECT DISTINCT company_id FROM wh.usage_weekly ORDER BY 1")]


def renewal_history(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM wh.renewal_history ORDER BY renewal_date, id").fetchall()
