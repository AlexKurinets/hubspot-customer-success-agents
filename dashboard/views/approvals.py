import pandas as pd
import streamlit as st
from views.common import get, header, label, md, post

header("Approvals")
st.write("These are the Slack approval messages: production posted each one to the CSM with approve, edit and reject "
         "buttons. Deciding here records the answer; the worker resumes the waiting agent thread, re-checks billing and "
         "the record, and only then writes to HubSpot.")

rows = get("/approvals", status="open")
if not rows:
    st.success("No approvals are waiting.")
else:
    df = pd.DataFrame(rows)
    df["action"] = df["action_type"].map(label)
    st.dataframe(df[["id", "requested_on", "action", "company_name", "reason"]].rename(
        columns={"requested_on": "requested", "company_name": "account"}), hide_index=True, use_container_width=True)

    choice = st.selectbox("Open an approval", [r["id"] for r in rows],
                          format_func=lambda i: next(f"{r['id']}: {label(r['action_type'])} for {r['company_name']}"
                                                     for r in rows if r["id"] == i))
    r = next(x for x in rows if x["id"] == choice)
    st.markdown(md(f"**Why it needs a person:** {r['reason']}"))
    p = r["payload"]
    st.markdown(md(f"**What the agent proposes:** {p.get('why', '')}"))
    if "body" in p:
        st.text_input("Subject", p.get("subject", ""), disabled=True)
        body = st.text_area("Draft", p["body"], height=180)
    else:
        body = None
        if r["action_type"] == "prepare_qbr":
            draft = get(f"/qbr/{p['draft_id']}")
            st.markdown(md(f"Figure check: **{draft['check_status']}** after {draft['attempts']} attempt(s)"
                           + (f"; numbers not in the figures: {', '.join(draft['mismatches'])}" if draft["mismatches"] else "")))
            st.text_area("QBR narrative", draft["narrative"], height=200, disabled=True)
            st.json(draft["figures"], expanded=False)
        elif r["action_type"] == "change_deal_amount":
            st.markdown(md(f"Renewal deal {p['deal_id']}: {p['old_amount']:,.0f} to {p['new_amount']:,}"))

    with st.form("decide"):
        name = st.text_input("Your name", "Maya Okafor")
        token = st.text_input("Approver token", type="password", help="APPROVER_TOKEN in .env; demo-csm by default")
        decision = st.radio("Decision", ["approve", "edit", "reject"], horizontal=True)
        if st.form_submit_button("Send decision"):
            payload = {"decision": decision, "by": name}
            if decision == "edit" and body is not None:
                payload["body"] = body
            resp = post(f"/approvals/{choice}/decide", payload, token)
            if resp.status_code == 200:
                st.success("Recorded. The worker carries it out within a few seconds.")
            else:
                st.error(f"{resp.status_code}: {resp.json().get('detail')}")

st.subheader("Recent decisions")
recent = pd.DataFrame(get("/approvals", status="all", limit=40))
if not recent.empty:
    recent = recent[recent["decision"].notna()]
    recent["action"] = recent["action_type"].map(label)
    st.dataframe(recent[["id", "decided_on", "action", "company_name", "decision", "decided_by", "status", "outcome"]]
                 .rename(columns={"decided_on": "decided", "company_name": "account", "decided_by": "by"}),
                 hide_index=True, use_container_width=True)
