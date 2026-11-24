from __future__ import annotations

import pandas as pd
import streamlit as st

from api import ApiError, get, post, rs, run

st.title("Payments & inventory")
st.caption("For the accounts team and the manager. Every action here calls the same staff API a real back-office "
           "app would use.")

with st.sidebar:
    st.markdown("**Acting as**")
    accountant = st.text_input("Accountant", "Accountant Sana")
    manager = st.text_input("Manager", "Manager Asif")
    storekeeper = st.text_input("Storekeeper", "Storekeeper Bilal")
    clerk = st.text_input("Clerk", "Clerk Hamza")


def load(path: str, **params):
    try:
        return get(path, **params)
    except ApiError as exc:
        st.error(str(exc))
        st.stop()


payments_tab, stock_tab, po_tab, bills_tab, notes_tab = st.tabs(
    [":material/payments: Customer payments", ":material/inventory_2: Inventory",
     ":material/shopping_cart: Purchase orders", ":material/request_quote: Supplier bills",
     ":material/notifications: Notifications"])

with payments_tab:
    pending = load("/staff/payments", status="pending_verification")["payments"]
    st.markdown(f"#### Waiting for verification ({len(pending)})")
    st.caption("Check each transaction ID against the bank statement, then verify or reject. Verifying messages "
               "the customer and sends the order to dispatch.")
    if not pending:
        st.success("Nothing waiting.", icon=":material/task_alt:")
    for p in pending:
        with st.container(border=True):
            a, b, c = st.columns([3, 2, 3])
            a.markdown(f"**{p['order_number']}** · {p['customer_name']}  \n"
                       f"{p['method'].replace('_', ' ')} · ref `{p['reference']}`")
            b.metric("Amount", rs(p["amount"]))
            with c:
                if st.button("Verify", key=f"v{p['payment_id']}", type="primary", icon=":material/check:"):
                    done = run("Verify payment", post, f"/staff/payments/{p['payment_id']}/verify",
                               {"verified_by": accountant})
                    if done:
                        assigned = done.get("dispatch", {}).get("assigned", [])
                        st.session_state["pay_note"] = (
                            f"{p['order_number']} verified. "
                            + (f"Rider {assigned[0]['rider_name']} assigned." if assigned else
                               f"Delivery scheduled for {done['payment']['delivery_date']}."))
                        st.rerun()
                reason = st.text_input("Reason if rejecting", key=f"r{p['payment_id']}",
                                       placeholder="not found in bank statement")
                if st.button("Reject", key=f"x{p['payment_id']}", icon=":material/close:"):
                    if run("Reject payment", post, f"/staff/payments/{p['payment_id']}/reject",
                           {"rejected_by": accountant, "reason": reason or "not found in bank statement"}):
                        st.rerun()
    if st.session_state.get("pay_note"):
        st.success(st.session_state.pop("pay_note"))
    st.divider()
    if st.button("Run the 24-hour payment deadline now", icon=":material/timer:"):
        result = run("Payment deadline", post, "/staff/payments/cancel-unpaid")
        if result:
            st.info(f"Cancelled {len(result['cancelled'])} unpaid order(s); "
                    f"{result['waiting_for_review']} claim(s) waiting over 2 hours.")
    history = load("/staff/payments")["payments"]
    if history:
        st.markdown("#### All payments")
        st.dataframe(pd.DataFrame([{"#": p["payment_id"], "Order": p["order_number"], "Customer": p["customer_name"],
                                    "Amount": rs(p["amount"]), "Reference": p["reference"],
                                    "Status": p["status"].replace("_", " "), "Checked by": p["reviewed_by"] or "-",
                                    "Order status": p["order_status"].replace("_", " ")} for p in history]),
                     hide_index=True, width="stretch")

with stock_tab:
    products = load("/staff/inventory")["products"]
    df = pd.DataFrame(products)
    low = df[df["needs_reorder"]]
    a, b, c, d = st.columns(4)
    a.metric("Products", len(df))
    b.metric("Units on shelves", int(df["physical"].sum()))
    c.metric("Reserved for orders", int(df["reserved"].sum()))
    d.metric("At or below reorder point", len(low))
    st.metric("Stock value (at selling price)", rs(sum(float(v) for v in df["stock_value"])))
    st.markdown("#### Stock by product")
    st.bar_chart(df.set_index("name")[["available", "reserved", "on_order"]], height=320, stack=True,
                 y_label="packs (cartons / bags)")
    st.dataframe(pd.DataFrame([{
        "ID": p["product_id"], "Product": p["name"], "Pack": p["pack"], "Price": rs(p["unit_price"]),
        "Physical": p["physical"], "Reserved": p["reserved"], "Available": p["available"],
        "On order": p["on_order"], "Reorder at": p["reorder_level"], "Max": p["max_level"],
        "Reorder?": "yes" if p["needs_reorder"] else "",
    } for p in products]), hide_index=True, width="stretch")
    st.caption("Available = physical − reserved. Reorder when available + on order ≤ reorder point.")
    if st.button("Run reorder check now", icon=":material/autorenew:"):
        result = run("Reorder check", post, "/staff/reorder-check")
        if result:
            st.info(f"Drafted {len(result['purchase_orders'])} purchase order(s). See the Purchase orders tab.")

with po_tab:
    pos = load("/staff/purchase-orders")["purchase_orders"]
    if not pos:
        st.info("No purchase orders yet. Run the reorder check on the Inventory tab.")
    for po in pos:
        lines = ", ".join(f"{l['name']} × {l['qty_ordered']}" + (f" (received {l['qty_received']})"
                                                                  if l["qty_received"] else "") for l in po["lines"])
        with st.container(border=True):
            a, b = st.columns([4, 2])
            a.markdown(f"**{po['po_number']}** · {po['supplier_name']} · `{po['status']}`  \n{lines}  \n"
                       f"{rs(po['total_cost'])} · {po['reason']}")
            base = f"/staff/purchase-orders/{po['po_number']}"
            with b:
                if po["status"] == "draft":
                    if st.button("Approve", key=f"a{po['po_number']}", type="primary"):
                        if run("Approve", post, f"{base}/approve", {"approved_by": manager}):
                            st.rerun()
                    if st.button("Reject, try next supplier", key=f"j{po['po_number']}"):
                        if run("Reject", post, f"{base}/reject", {"rejected_by": manager, "reason": "rejected by manager",
                                                                   "try_next_supplier": True}):
                            st.rerun()
                elif po["status"] == "approved":
                    if st.button("Send to supplier", key=f"s{po['po_number']}", type="primary"):
                        if run("Send", post, f"{base}/send"):
                            st.rerun()
                elif po["status"] in ("sent", "partially_received"):
                    if st.button(f"Receive all ({storekeeper})", key=f"g{po['po_number']}", type="primary"):
                        items = [{"product_id": l["product_id"], "qty": l["qty_ordered"] - l["qty_received"]}
                                 for l in po["lines"] if l["qty_ordered"] > l["qty_received"]]
                        if run("Receive", post, f"{base}/receive", {"received_by": storekeeper, "items": items}):
                            st.rerun()

with bills_tab:
    st.markdown("#### Record a supplier's invoice")
    received = [po for po in pos if po["status"] in ("received", "partially_received")]
    if received:
        with st.form("bill"):
            po_number = st.selectbox("Purchase order", [po["po_number"] for po in received])
            number = st.text_input("Supplier's invoice number", "INV-1001")
            amount = st.number_input("Amount billed (Rs)", min_value=1.0, step=100.0,
                                     value=float(next(po["total_cost"] for po in received)))
            if st.form_submit_button("Record invoice"):
                result = run("Record invoice", post, "/staff/supplier-invoices",
                             {"po_number": po_number, "invoice_number": number, "amount": str(amount),
                              "recorded_by": clerk})
                if result:
                    inv = result["invoice"]
                    (st.success if inv["match_status"] == "matched" else st.warning)(
                        f"Three-way match: {inv['match_status']} (billed {rs(inv['amount'])}, goods received worth "
                        f"{rs(inv['expected_amount'])}).")
    else:
        st.caption("Receive a purchase order first: suppliers can only bill goods we received.")
    bills = load("/staff/supplier-invoices")
    st.markdown(f"#### Bills · open total {rs(bills['total_open'])}")
    for inv in bills["invoices"]:
        with st.container(border=True):
            a, b = st.columns([4, 2])
            flag = " :red[overdue]" if inv["overdue"] else ""
            a.markdown(f"**{inv['invoice_number']}** · {inv['supplier_name']} · {inv['po_number']} · "
                       f"`{inv['status']}`{flag}  \nBilled {rs(inv['amount'])} · received worth "
                       f"{rs(inv['expected_amount'])} · match **{inv['match_status']}** · due {inv['due_date']}")
            with b:
                if inv["status"] == "awaiting_approval":
                    note = st.text_input("Override reason (mismatch)", key=f"o{inv['invoice_id']}")
                    if st.button("Approve", key=f"ia{inv['invoice_id']}", type="primary"):
                        if run("Approve bill", post, f"/staff/supplier-invoices/{inv['invoice_id']}/approve",
                               {"approved_by": manager, "override_reason": note or None}):
                            st.rerun()
                elif inv["status"] == "approved":
                    if st.button(f"Mark paid ({accountant})", key=f"ip{inv['invoice_id']}", type="primary"):
                        if run("Pay bill", post, f"/staff/supplier-invoices/{inv['invoice_id']}/pay",
                               {"paid_by": accountant, "payment_reference": f"IBFT-{inv['invoice_id']:05d}"}):
                            st.rerun()

with notes_tab:
    notes = load("/staff/notifications", status="open")["notifications"]
    st.markdown(f"#### Open notifications ({len(notes)})")
    for n in notes:
        with st.container(border=True):
            a, b = st.columns([5, 1])
            a.markdown(f"`{n['role']}` · {n['reason']}" + (f"  \nthread {n['thread_id']}" if n["thread_id"] else ""))
            if b.button("Resolve", key=f"n{n['id']}"):
                if run("Resolve", post, f"/staff/notifications/{n['id']}/resolve"):
                    st.rerun()
