import streamlit as st

st.set_page_config(page_title="Customer Success agents", page_icon=":material/support_agent:", layout="wide")

pages = [
    st.Page("views/overview.py", title="Overview", icon=":material/monitoring:", default=True),
    st.Page("views/approvals.py", title="Approvals", icon=":material/fact_check:", url_path="approvals"),
    st.Page("views/accounts.py", title="Accounts", icon=":material/storefront:", url_path="accounts"),
    st.Page("views/reliability.py", title="Reliability", icon=":material/shield:", url_path="reliability"),
]
st.navigation(pages).run()
