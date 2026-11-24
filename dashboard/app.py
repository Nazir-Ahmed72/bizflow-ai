from __future__ import annotations

import streamlit as st

from api import api_online

st.set_page_config(page_title="BizFlow AI", page_icon=":material/storefront:", layout="wide")

pages = {
    "Customer": [
        st.Page("pages/chat.py", title="Chat with the bot", icon=":material/chat:", default=True),
        st.Page("pages/my_orders.py", title="My orders", icon=":material/receipt_long:"),
    ],
    "Company": [
        st.Page("pages/manager.py", title="Payments & inventory", icon=":material/account_balance:"),
        st.Page("pages/delivery.py", title="Delivery", icon=":material/local_shipping:"),
    ],
}

with st.sidebar:
    st.markdown("### BizFlow AI")
    st.caption("Order agent for a Karachi FMCG distributor")
    online = api_online()

navigation = st.navigation(pages)
if online:
    navigation.run()
else:
    st.title("BizFlow AI")
    st.info("Start the API first:  `cd backend`  then  `uvicorn app.api.main:app`")
