from __future__ import annotations

import pandas as pd
import streamlit as st

from api import ApiError, get, post, run

st.title("Delivery")
st.caption("Dispatcher view and a simulated rider app. Riders are matched by area first, then by fewest deliveries "
           "today; each carries at most 3 at a time.")

try:
    riders = get("/staff/riders")["riders"]
    deliveries = get("/staff/deliveries", limit=200)["deliveries"]
except ApiError as exc:
    st.error(str(exc))
    st.stop()

by_status = {s: [d for d in deliveries if d["status"] == s]
             for s in ("queued", "assigned", "out_for_delivery", "delivered", "failed")}
a, b, c, d, e = st.columns(5)
a.metric("Riders on duty", sum(r["is_on_duty"] for r in riders))
b.metric("Waiting for a rider", len(by_status["queued"]))
c.metric("Assigned", len(by_status["assigned"]))
d.metric("On the way", len(by_status["out_for_delivery"]))
e.metric("Delivered", len(by_status["delivered"]))

if st.button("Run dispatch now", type="primary", icon=":material/route:"):
    result = run("Dispatch", post, "/staff/dispatch")
    if result:
        st.session_state["dispatch_note"] = (
            ", ".join(f"{x['order_number']} → {x['rider_name']}" for x in result["assigned"]) or "Nothing to assign"
        ) + (f" · {result['still_waiting_today']} still waiting (all riders full)"
             if result["still_waiting_today"] else "")
        st.rerun()
if st.session_state.get("dispatch_note"):
    st.info(st.session_state.pop("dispatch_note"))

st.markdown("#### Riders")
cols = st.columns(len(riders) or 1)
for col, r in zip(cols, riders):
    with col.container(border=True):
        st.markdown(f"**{r['name']}** · {r['area']}  \n{r['phone']}")
        st.progress(r["active_count"] / r["capacity"], text=f"{r['active_count']}/{r['capacity']} now · "
                                                             f"{r['deliveries_today']} today")
        label = "End shift" if r["is_on_duty"] else "Start shift"
        if st.button(label, key=f"duty{r['rider_id']}"):
            if run(label, post, f"/riders/{r['rider_id']}/duty", {"on_duty": not r["is_on_duty"]}):
                st.rerun()

st.markdown("#### Rider app (simulated)")
st.caption("What the assigned rider would tap on their phone.")
active = by_status["assigned"] + by_status["out_for_delivery"]
if not active:
    st.caption("No assigned deliveries. Verify a payment, then run dispatch.")
for dv in active:
    with st.container(border=True):
        left, right = st.columns([4, 2])
        left.markdown(f"**{dv['order_number']}** · {dv['customer_name']} · {dv['address']} ({dv['area']})  \n"
                      f"Rider **{dv['rider_name']}** · attempt {dv['attempt']} · `{dv['status']}`")
        base = f"/riders/{dv['rider_id']}/deliveries/{dv['delivery_id']}"
        with right:
            if dv["status"] == "assigned":
                if st.button("Picked up", key=f"p{dv['delivery_id']}", type="primary", icon=":material/package_2:"):
                    if run("Pickup", post, f"{base}/pickup"):
                        st.rerun()
            else:
                if st.button("Delivered", key=f"d{dv['delivery_id']}", type="primary", icon=":material/check:"):
                    if run("Delivered", post, f"{base}/delivered"):
                        st.rerun()
                reason = st.text_input("Problem", key=f"f{dv['delivery_id']}", placeholder="shop closed")
                if st.button("Could not deliver", key=f"x{dv['delivery_id']}", icon=":material/close:"):
                    if run("Failed delivery", post, f"{base}/failed", {"reason": reason or "shop closed"}):
                        st.rerun()

st.markdown("#### All deliveries")
if deliveries:
    st.dataframe(pd.DataFrame([{
        "#": x["delivery_id"], "Order": x["order_number"], "Shop": x["customer_name"], "Area": x["area"],
        "Date": x["scheduled_date"], "Attempt": x["attempt"], "Status": x["status"].replace("_", " "),
        "Rider": x["rider_name"] or "-", "Problem": x["failure_reason"] or "",
    } for x in reversed(deliveries)]), hide_index=True, width="stretch")
else:
    st.caption("No deliveries yet.")
