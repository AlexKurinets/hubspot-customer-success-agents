import altair as alt
import pandas as pd
import streamlit as st
from views.common import AUTONOMY_COLORS, TIER_COLORS, get, header, label

s = header("Customer Success agents")
a, lead, q = s["actions"], s["renewal_lead_days"], s["qbr_prep_minutes"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("HubSpot actions a month", f"{a['per_month']:,}", help=f"{a['total']:,} actions in the last {s['window']['days']} days")
c2.metric("With no human touch", f"{a['autonomous_share']:.0%}",
          help="Ran at autonomy level act: nobody approved, edited or prepared it")
c3.metric("Renewal risk reaches the CSM", f"{lead['after_median']:.0f} days out",
          delta=f"{lead['after_median'] - lead['before_median']:+.0f} days vs {lead['before_median']:.0f} before",
          help=f"Median over {lead['after_n']} at-risk renewals in the window; before: {lead['before_n']} at-risk renewals "
               "in the year before launch, days out when a CSM first touched them")
c4.metric("QBR prep per review", f"{q['after_median']:.0f} min",
          delta=f"{q['after_median'] - q['before_median']:+.0f} min vs {q['before_median']:.0f} before",
          delta_color="inverse", help=f"CSM-logged review time on {q['after_n']} QBR packs in the window")

st.caption(f"{s['accounts']:,} accounts · {s['csms']} CSMs · launched {s['launch_date']} with one shadow week · "
           f"{s['approvals_open']} approvals open")

left, right = st.columns([3, 2])
with left:
    st.subheader("Actions per month")
    rows = pd.DataFrame(get("/metrics/monthly"))
    if rows.empty:
        st.info("No actions yet.")
    else:
        rows["who"] = rows["autonomy"].map({"act": "No human touch", "approve": "Approved by a CSM"})
        monthly = rows.groupby(["month", "who"], as_index=False)["n"].sum()
        chart = alt.Chart(monthly).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, stroke="#fcfcfb",
                                            strokeWidth=2).encode(
            x=alt.X("month:N", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("n:Q", title="Actions", stack="zero"),
            color=alt.Color("who:N", title=None, scale=alt.Scale(domain=list(AUTONOMY_COLORS),
                                                                  range=list(AUTONOMY_COLORS.values())),
                            legend=alt.Legend(orient="top")),
            order=alt.Order("who:N", sort="descending"),
            tooltip=[alt.Tooltip("month:N", title="Month"), alt.Tooltip("who:N", title="Path"),
                     alt.Tooltip("n:Q", title="Actions", format=",")],
        ).properties(height=300)
        st.altair_chart(chart, use_container_width=True)
        st.caption("February includes the shadow week, when the agents only logged what they would have done.")

with right:
    st.subheader("Accounts by health tier")
    tiers = pd.DataFrame([{"tier": t, "accounts": s["tiers"].get(t, 0)} for t in ("green", "yellow", "red")])
    bars = alt.Chart(tiers).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4).encode(
        y=alt.Y("tier:N", sort=["green", "yellow", "red"], title=None),
        x=alt.X("accounts:Q", title="Accounts"),
        color=alt.Color("tier:N", scale=alt.Scale(domain=list(TIER_COLORS), range=list(TIER_COLORS.values())),
                        legend=None),
        tooltip=["tier", alt.Tooltip("accounts:Q", format=",")])
    text = alt.Chart(tiers).mark_text(align="left", dx=4, color="#52514e").encode(
        y=alt.Y("tier:N", sort=["green", "yellow", "red"]), x="accounts:Q", text=alt.Text("accounts:Q", format=","))
    st.altair_chart((bars + text).properties(height=170), use_container_width=True)
    pts = s["health_model"]["points"]
    st.caption(f"Score = {pts['seat_utilization']} × seat utilization + {pts['module_adoption']} × module adoption + "
               f"{pts['ticket_ok']} × ticket load (inverted) + {pts['invoice_current']} × invoice current. "
               f"Green from {s['health_model']['tiers']['green']}, yellow from {s['health_model']['tiers']['yellow']}. "
               f"Held-out AUC {s['health_model'].get('held_out_auc', 'n/a')}.")

st.subheader("Actions in the window by type")
by_type = pd.DataFrame([{"action": label(k), "No human touch": v["act"], "Approved by a CSM": v["approve"]}
                        for k, v in a["by_type"].items()])
if not by_type.empty:
    long = by_type.melt(id_vars="action", var_name="who", value_name="n")
    order = by_type.assign(t=by_type["No human touch"] + by_type["Approved by a CSM"]).sort_values("t", ascending=False)
    chart = alt.Chart(long).mark_bar(stroke="#fcfcfb", strokeWidth=2).encode(
        y=alt.Y("action:N", sort=list(order["action"]), title=None, axis=alt.Axis(labelLimit=220, labelOverlap=False)),
        x=alt.X("n:Q", title="Actions", stack="zero"),
        color=alt.Color("who:N", title=None, scale=alt.Scale(domain=list(AUTONOMY_COLORS),
                                                              range=list(AUTONOMY_COLORS.values())),
                        legend=alt.Legend(orient="top")),
        tooltip=[alt.Tooltip("action:N", title="Action"), alt.Tooltip("who:N", title="Path"),
                 alt.Tooltip("n:Q", title="Actions", format=",")],
    ).properties(height=380)
    st.altair_chart(chart, use_container_width=True)

st.subheader("Graduation from approval to autonomous")
grad = pd.DataFrame(s["graduation"])
grad["action"] = grad["action_type"].map(label)
st.dataframe(grad[["action", "graduated", "graduated_on", "days_after_launch", "last_decisions",
                   "edited_or_rejected"]].rename(columns={
                       "days_after_launch": "days after launch", "graduated_on": "graduated on",
                       "last_decisions": "last decisions", "edited_or_rejected": "edited or rejected"}),
             hide_index=True, use_container_width=True)
st.caption("Rule: the last 50 CSM decisions on the type, under 5% edited or rejected. Accounts over the ARR threshold "
           "and anything touching billing still go to the CSM.")
