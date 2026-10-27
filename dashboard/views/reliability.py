import pandas as pd
import streamlit as st
from views.common import get, header, label

header("Reliability")
r = get("/metrics/reliability")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Ledger rows done", f"{r['ledger'].get('done', 0):,}", help=f"claimed {r['ledger'].get('claimed', 0)}, "
                                                                        f"failed {r['ledger'].get('failed', 0)}")
c2.metric("Duplicate claims stopped", r["collisions"],
          help="Second claims of a key already claimed, by source: "
               + ", ".join(f"{k} {v}" for k, v in r["collisions_by_source"].items()))
c3.metric("Webhook redeliveries ignored", r["webhook_duplicates"],
          help=f"Events stored by event id; {r['webhook_rejections']} requests rejected on signature or age")
c4.metric("Dead letters open", r["dead_letters_open"])

c1, c2, c3, c4 = st.columns(4)
cl = r["client"]
c1.metric("HubSpot requests", f"{cl['requests']:,}")
c2.metric("Retries", cl["retries"], help=f"429s {cl['rate_limited']}, 5xx {cl['server_errors']}")
c3.metric("Waits from the token bucket", f"{cl['throttle_waits']:,}")
c4.metric("Failed nightly runs", len(r["failed_runs"]))

left, right = st.columns(2)
with left:
    st.subheader("Webhook events")
    st.dataframe(pd.DataFrame([{"status": k, "events": v} for k, v in r["webhooks"].items()]), hide_index=True,
                 use_container_width=True)
    st.caption("own_change: an INTEGRATION change made by this app, stored and not acted on.")
    st.subheader("QBR figure check")
    st.dataframe(pd.DataFrame([{"result": k, "drafts": v} for k, v in r["qbr_checks"].items()]), hide_index=True,
                 use_container_width=True)
with right:
    st.subheader("Blocked by preconditions, window")
    st.dataframe(pd.DataFrame([{"precondition": k, "actions": v} for k, v in r["blocked_in_window"].items()]),
                 hide_index=True, use_container_width=True)
    st.subheader("Shadow week (logged, not executed)")
    st.dataframe(pd.DataFrame([{"action": label(k), "would have run": v} for k, v in r["shadow_week"].items()]),
                 hide_index=True, use_container_width=True)

st.subheader("Weekly report: what the agents chose not to do")
blocked = pd.DataFrame(r["blocked_last_7_days"])
if blocked.empty:
    st.info("Nothing blocked in the last seven days.")
else:
    blocked["action"] = blocked["action_type"].map(label)
    st.dataframe(blocked[["business_date", "action", "company_id", "precondition", "detail"]], hide_index=True,
                 use_container_width=True)

st.subheader("Approved changes cancelled at execution")
cancelled = pd.DataFrame(r["cancelled_approvals"])
if cancelled.empty:
    st.info("None.")
else:
    cancelled["action"] = cancelled["action_type"].map(label)
    st.dataframe(cancelled[["id", "decided_on", "action", "company_name", "outcome"]], hide_index=True,
                 use_container_width=True)
