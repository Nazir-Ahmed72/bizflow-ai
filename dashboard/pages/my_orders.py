from __future__ import annotations

import pandas as pd
import streamlit as st

from api import SHOPS, ApiError, get, phone_path, rs

STATUS_LABEL = {
    "awaiting_payment": ":orange[waiting for payment]",
    "ready_to_dispatch": ":blue[paid, waiting for delivery]",
    "out_for_delivery": ":violet[on the way]",
    "delivered": ":green[delivered]",
    "returned": ":red[returned]",
    "cancelled": ":gray[cancelled]",
}

st.title("My orders")
shops = {k: v for k, v in SHOPS.items() if k != "Unregistered number"}
shop = st.sidebar.selectbox("Shop", list(shops), key="orders_shop")

try:
    data = get(f"/customers/{phone_path(shops[shop])}")
except ApiError as exc:
    st.error(str(exc))
    st.stop()

c = data["customer"]
orders = data["orders"]
st.subheader(c["name"])
a, b, d, e = st.columns(4)
a.metric("Price category", f"{c['price_category']} ({float(c['discount_pct']):g}% off)")
b.metric("Area", c["area"] or "-")
d.metric("Orders", len(orders))
e.metric("Open orders", sum(o["status"] not in ("delivered", "cancelled", "returned") for o in orders))
st.caption(f"Delivery address: {c['address']} · Phone {c['phone']}")

if not orders:
    st.info("No orders yet. Place one on the chat page.")
    st.stop()

table = pd.DataFrame([{
    "Order": o["order_number"],
    "Placed": pd.to_datetime(o["created_at"]).tz_convert("Asia/Karachi").strftime("%d %b %H:%M"),
    "Status": o["status"].replace("_", " "),
    "Total": rs(o["total"]),
    "Payment": (o["payment"] or {}).get("status", "not reported").replace("_", " "),
    "Delivery date": o["delivery_date"] or "-",
    "Rider": (o["delivery"] or {}).get("rider_name") or "-",
} for o in orders])
st.dataframe(table, hide_index=True, width="stretch")

st.markdown("#### Order details")
for o in orders:
    with st.expander(f"{o['order_number']} · {rs(o['total'])} · {o['status'].replace('_', ' ')}"):
        st.markdown(f"Status: {STATUS_LABEL.get(o['status'], o['status'])}"
                    + (f" · Invoice {o['invoice_number']}" if o["invoice_number"] else ""))
        st.dataframe(pd.DataFrame([{"Product": i["name"], "Qty": i["qty"], "Unit price": rs(i["unit_price"]),
                                    "Line total": rs(i["line_total"])} for i in o["items"]]),
                     hide_index=True, width="stretch")
        st.markdown(f"Subtotal {rs(o['subtotal'])} · Discount {rs(o['discount_amount'])} · **Total {rs(o['total'])}**")
        left, right = st.columns(2)
        with left:
            st.markdown("**Payment**")
            p = o["payment"]
            if p is None:
                due = o["payment_due_at"]
                st.write("Not reported yet" + (f" · pay before "
                                                f"{pd.to_datetime(due).tz_convert('Asia/Karachi'):%d %b %H:%M}"
                                                if due and o["status"] == "awaiting_payment" else ""))
            else:
                st.write(f"{p['status'].replace('_', ' ')} · {p['method'].replace('_', ' ')} · ref {p['reference']}")
                if p.get("rejected_reason"):
                    st.write(f"Rejected: {p['rejected_reason']}")
        with right:
            st.markdown("**Delivery**")
            dv = o["delivery"]
            if dv is None:
                st.write("After payment is verified")
            else:
                st.write(f"{dv['status'].replace('_', ' ')} · attempt {dv['attempt']} · {dv['scheduled_date']}")
                if dv.get("rider_name"):
                    st.write(f"Rider {dv['rider_name']} ({dv['rider_phone']})")
                if dv.get("failure_reason"):
                    st.write(f"Last problem: {dv['failure_reason']}")
        if o.get("cancel_reason"):
            st.warning(o["cancel_reason"])
