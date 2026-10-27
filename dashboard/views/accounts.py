import altair as alt
import pandas as pd
import streamlit as st
from views.common import SINGLE, TIER_COLORS, get, header, label, md

s = header("Accounts")
tiers = s["health_model"]["tiers"]
tier = st.segmented_control("Tier", ["red", "yellow", "green"], default="red")
accounts = get("/accounts", tier=tier) if tier else get("/accounts")
if not accounts:
    st.info("No accounts in this tier.")
    st.stop()
choice = st.selectbox("Account (lowest score first)", [a["company_id"] for a in accounts],
                      format_func=lambda i: next(f"{a['name']} · score {a['score']} · renews {a['renewal_date']}"
                                                 for a in accounts if a["company_id"] == i))
d = get(f"/accounts/{choice}")
c = d["company"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Health score", c.get("cs_agent_health_score"), help=f"Tier {c.get('cs_agent_health_tier')}")
c2.metric("Annual value", f"{float(c.get('cs_arr') or 0):,.0f}")
c3.metric("Renewal", c.get("cs_renewal_date") or "n/a")
c4.metric("Last contacted", (c.get("notes_last_contacted") or "")[:10] or "n/a")
if c.get("cs_agent_health_reason"):
    st.markdown(md(f"**Reasons the agent found:** {c['cs_agent_health_reason']}"))

left, right = st.columns(2)
history = pd.DataFrame(d["health_history"])
with left:
    st.subheader("Score over time")
    if not history.empty:
        history["business_date"] = pd.to_datetime(history["business_date"])
        line = alt.Chart(history).mark_line(color=SINGLE, strokeWidth=2, interpolate="step-after").encode(
            x=alt.X("business_date:T", title=None), y=alt.Y("score:Q", title="Score", scale=alt.Scale(domain=[0, 100])))
        dots = alt.Chart(history).mark_point(size=64, filled=True, stroke="#fcfcfb", strokeWidth=2).encode(
            x="business_date:T", y="score:Q",
            color=alt.Color("tier:N", scale=alt.Scale(domain=list(TIER_COLORS), range=list(TIER_COLORS.values())),
                            legend=alt.Legend(orient="top", title=None)),
            tooltip=[alt.Tooltip("business_date:T", title="Date"), "score", "tier"])
        rules = alt.Chart(pd.DataFrame({"y": [tiers["green"], tiers["yellow"]]})).mark_rule(
            color="#cbc9c3", strokeDash=[4, 4]).encode(y="y:Q")
        st.altair_chart((rules + line + dots).properties(height=260), use_container_width=True)
with right:
    st.subheader("Points by signal, latest")
    if not history.empty:
        pts = history.iloc[-1]["points"]
        df = pd.DataFrame([{"signal": k.replace("_", " "), "points": v} for k, v in pts.items()])
        bars = alt.Chart(df).mark_bar(color=SINGLE, cornerRadiusTopRight=4, cornerRadiusBottomRight=4).encode(
            y=alt.Y("signal:N", title=None, sort=None), x=alt.X("points:Q", title="Points"),
            tooltip=["signal", "points"])
        text = bars.mark_text(align="left", dx=4, color="#52514e").encode(text="points:Q")
        st.altair_chart((bars + text).properties(height=200), use_container_width=True)

st.subheader("HubSpot timeline")
timeline = pd.DataFrame(d["timeline"])
if d.get("timeline_error"):
    st.warning(d["timeline_error"])
elif timeline.empty:
    st.info("Nothing on the timeline yet.")
else:
    def summary(r):
        for k in ("hs_note_body", "hs_task_subject", "hs_email_subject", "dealname"):
            if isinstance(r.get(k), str) and r[k]:
                return r[k][:160]
        return ""
    timeline["summary"] = timeline.apply(summary, axis=1)
    st.dataframe(timeline[["when", "type", "summary"]], hide_index=True, use_container_width=True)

st.subheader("Agent actions (ledger)")
ledger = pd.DataFrame(d["ledger"])
if not ledger.empty:
    ledger["action"] = ledger["action_type"].map(label)
    st.dataframe(ledger[["business_date", "action", "autonomy", "status", "source", "action_key"]],
                 hide_index=True, use_container_width=True)

if d["qbr_drafts"]:
    st.subheader("QBR drafts")
    for q in d["qbr_drafts"][:2]:
        st.markdown(md(f"**{q['meeting_date']}** · figure check {q['check_status']} after {q['attempts']} attempt(s)"))
        st.text(q["narrative"])
if d["approvals"]:
    st.subheader("Approvals")
    ap = pd.DataFrame(d["approvals"])
    ap["action"] = ap["action_type"].map(label)
    st.dataframe(ap[["id", "requested_on", "action", "status", "decided_by", "outcome"]], hide_index=True,
                 use_container_width=True)
