from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

from . import approvals, config, db, metrics, replay, scoring, settings
from .agents import jobs, nightly
from .agents.context import AgentContext
from .hubspot.client import HubSpotClient
from .hubspot.schema import ensure_agent_properties
from .llm import get_llm
from .sources import warehouse
from .synthetic import simulate, world

SEED_END = date(2026, 6, 30)


def _clients() -> tuple[HubSpotClient, HubSpotClient]:
    s = settings.load()
    portal_token = __import__("os").environ.get("MOCK_PORTAL_TOKEN", "pat-demo-portal-users")
    return HubSpotClient(), HubSpotClient(base_url=s.hubspot_url, token=portal_token)


def _flush(portal: HubSpotClient):
    return lambda: portal.http.post("/_mock/webhooks/flush", timeout=120).json()


def _saver_conn():
    """Two autocommit connections for LangGraph: nightly threads, and the approval threads they start."""
    return db.connect(autocommit=True), db.connect(autocommit=True)


def _setup_checkpoints(saver_conn) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    PostgresSaver(saver_conn[0]).setup()


def _progress(out: dict) -> None:
    day = date.fromisoformat(out["today"])
    if day.day == 1 or day.weekday() == 0 and day.day <= 7:
        planned = {a["agent"]: a.get("planned", 0) for a in out["agents"]}
        print(f"{out['today']}: planned {planned}, jobs {out['jobs']}, resumed {out['resumed']}", flush=True)


def cmd_seed(args) -> int:
    cfg = config.get()
    with db.connect() as conn:
        db.migrate(conn)
        if args.if_empty and conn.execute("SELECT 1 FROM ops.clock").fetchone():
            print("already seeded")
            return 0
        db.reset(conn)
        crm, portal = _clients()
        portal.http.post("/_mock/reset").raise_for_status()
        portal.http.post("/_mock/webhooks/auto", json={"enabled": False})
        print(f"agent properties created: {len(ensure_agent_properties(crm))}")
        started = time.perf_counter()
        w = world.generate(conn, portal, cfg.launch_date, accounts=args.accounts)
        print(f"generated {len(w.accounts)} accounts in {time.perf_counter() - started:.0f}s", flush=True)
        saver_conn = _saver_conn()
        _setup_checkpoints(saver_conn)
        started = time.perf_counter()
        errors = simulate.run(conn, saver_conn, crm, portal, get_llm(), cfg, w, cfg.launch_date, args.end,
                              _flush(portal), csms_until=args.end - timedelta(days=2), progress=_progress)
        portal.http.post("/_mock/webhooks/auto", json={"enabled": True})
        print(f"simulated {cfg.launch_date} to {args.end} in {time.perf_counter() - started:.0f}s")
        _print_status(conn, cfg)
        if errors:
            print("\n".join(errors[:20]), file=sys.stderr)
            return 1
    return 0


def cmd_advance(args) -> int:
    cfg = config.get()
    with db.connect() as conn:
        crm, portal = _clients()
        w = world.World.load(conn)
        saver_conn = _saver_conn()
        portal.http.post("/_mock/webhooks/auto", json={"enabled": False})
        for _ in range(args.days):
            day = db.today(conn) + timedelta(days=1)
            out = simulate.run_day(conn, saver_conn, crm, portal, get_llm(), cfg, w, day, _flush(portal), csms=False)
            print(json.dumps({"today": out["today"], "agents": out["agents"], "jobs": out["jobs"]}, default=str))
            if any(a["status"] != "done" for a in out["agents"]):
                return 1
        portal.http.post("/_mock/webhooks/auto", json={"enabled": True})
    return 0


def _ctx(conn, source: str = "worker") -> AgentContext:
    nightly_conn, approval_conn = _saver_conn()
    return AgentContext(conn, HubSpotClient(), get_llm(), config.get(), db.today(conn), nightly_conn, source=source,
                        approval_saver_conn=approval_conn)


def cmd_worker(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("csagents.worker")
    while True:
        try:
            with db.connect() as conn:
                ctx = _ctx(conn, "webhook")
                done = jobs.process(ctx)
                resumed = approvals.resume_decided(_ctx(conn, "approval"))
            if done or resumed:
                log.info("jobs %s, approvals resumed %s", done, resumed)
        except Exception:
            log.exception("worker tick failed")
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_tick(args) -> int:
    args.once = True
    return cmd_worker(args)


def cmd_nightly(args) -> int:
    with db.connect() as conn:
        out = nightly.run_all(_ctx(conn, "nightly"))
    print(json.dumps(out, default=str))
    return 1 if any(o["status"] != "done" for o in out) else 0


def cmd_replay(args) -> int:
    with db.connect() as conn:
        ctx = _ctx(conn, "replay")
        if args.dead_letters:
            print(json.dumps(replay.dead_letters(ctx)))
        if args.stale_claims:
            print(json.dumps(replay.stale_claims(ctx, args.older_than)))
        if args.run:
            agent, day = args.run.split(":")
            ctx.today = date.fromisoformat(day)
            out = nightly.run_agent(ctx, agent, resume=True)
            print(json.dumps(out, default=str))
            return 0 if out["status"] == "done" else 1
    return 0


def _print_status(conn, cfg) -> None:
    s = metrics.summary(conn, cfg)
    a, lead, q = s["actions"], s["renewal_lead_days"], s["qbr_prep_minutes"]
    print(f"business date {s['today']}, {s['accounts']} accounts, tiers {s['tiers']}")
    print(f"last {s['window']['days']} days: {a['total']} actions ({a['per_month']} a month), "
          f"{a['autonomous_share']:.0%} with no human touch")
    print(f"renewal risk reached CSMs a median {lead['after_median']} days before renewal "
          f"(n={lead['after_n']}), against {lead['before_median']} before launch (n={lead['before_n']})")
    print(f"QBR prep median {q['after_median']} minutes (n={q['after_n']}), against {q['before_median']} before (n={q['before_n']})")
    for g in s["graduation"]:
        print(f"{g['action_type']}: graduated {g['graduated']}, on {g['graduated_on']} "
              f"({g['days_after_launch']} days after launch)")
    print(f"open approvals {s['approvals_open']}")


def cmd_status(args) -> int:
    with db.connect() as conn:
        _print_status(conn, config.get())
        r = metrics.reliability(conn)
        print(f"ledger {r['ledger']}, collisions {r['collisions']}, webhook duplicates {r['webhook_duplicates']}, "
              f"dead letters open {r['dead_letters_open']}")
    return 0


def cmd_fit_weights(args) -> int:
    cfg = config.get()
    with db.connect() as conn:
        history = warehouse.renewal_history(conn)
    fit = scoring.fit_weights(history, cfg.launch_date - timedelta(days=182))
    print(json.dumps(fit, indent=2))
    if args.write:
        path = Path(settings.load().config_dir) / "health_weights.yaml"
        header = ("# Points per signal, from `csagents fit-weights --write`: a logistic regression of renewal on the\n"
                  "# four signals over the renewal history before the last six months, rescaled to 100 and rounded to\n"
                  "# fives. held_out_auc is measured on the last six months of renewals.\n")
        path.write_text(header + yaml.safe_dump(fit, sort_keys=False), encoding="utf-8")
        print(f"wrote {path}")
    return 0


def cmd_setup_hubspot(args) -> int:
    created = ensure_agent_properties(HubSpotClient())
    print(f"created {len(created)} properties: {', '.join(created) or 'none, all present'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="csagents")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="generate the synthetic portal and simulate launch to --end")
    s.add_argument("--if-empty", action="store_true")
    s.add_argument("--accounts", type=int, default=1200)
    s.add_argument("--end", type=date.fromisoformat, default=SEED_END)
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("advance", help="move the business date forward; the world changes and the agents run")
    s.add_argument("--days", type=int, default=1)
    s.set_defaults(fn=cmd_advance)
    s = sub.add_parser("worker", help="process webhook jobs and resume answered approvals in a loop")
    s.add_argument("--interval", type=float, default=5)
    s.add_argument("--once", action="store_true")
    s.set_defaults(fn=cmd_worker)
    s = sub.add_parser("tick", help="one worker pass")
    s.set_defaults(fn=cmd_tick, interval=0)
    sub.add_parser("nightly", help="run the three agents for the current business date").set_defaults(fn=cmd_nightly)
    s = sub.add_parser("replay", help="recovery: dead letters, stale ledger claims, a failed nightly run")
    s.add_argument("--dead-letters", action="store_true")
    s.add_argument("--stale-claims", action="store_true")
    s.add_argument("--older-than", type=int, default=10)
    s.add_argument("--run", help="a nightly thread id such as health:2026-06-30")
    s.set_defaults(fn=cmd_replay)
    sub.add_parser("status", help="the seeded figures as text").set_defaults(fn=cmd_status)
    s = sub.add_parser("fit-weights", help="refit the health score points on the renewal history")
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_fit_weights)
    sub.add_parser("setup-hubspot", help="create the cs_agent_ properties").set_defaults(fn=cmd_setup_hubspot)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
