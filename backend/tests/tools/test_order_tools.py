"""create_draft, cancel_draft, confirm_order, generate_invoice: idempotency, atomicity, revalidation."""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.tools import (
    ErrorCode, cancel_draft, confirm_order, create_draft, generate_invoice, get_customer_profile, get_order_history,
    idempotency_key_for,
)
from tests.conftest import ALI, KARACHI_MART


def _draft(customer_id, items, thread="t"):
    res = create_draft(thread_id=thread, customer_id=customer_id,
                       items=[{"product_id": p, "qty": q} for p, q in items])
    assert res.success, res
    return res


def _confirm(draft):
    return confirm_order(draft.draft_id, draft.idempotency_key)


# --------------------------------------------------------------------------- create / cancel draft

def test_create_draft_prices_and_expiry(customer_id, sql):
    before = datetime.now(timezone.utc)
    d = _draft(customer_id(ALI), [("P-001", 10), ("P-002", 5)])
    assert d.total == Decimal("62230.00")
    assert d.idempotency_key == f"{d.draft_id}:confirm"
    assert timedelta(hours=2) - timedelta(seconds=5) <= d.expires_at - before <= timedelta(hours=2, seconds=5)
    (row,) = sql("SELECT status, total, expires_at - summary_sent_at FROM order_drafts")
    assert row == ("pending", Decimal("62230.00"), timedelta(hours=2))


def test_create_draft_does_not_reserve_stock(customer_id, stock):
    _draft(customer_id(ALI), [("P-001", 10)])
    assert stock("P-001") == (140, 0)


def test_new_draft_supersedes_pending_one(customer_id, sql):
    cid = customer_id(ALI)
    first = _draft(cid, [("P-001", 1)])
    second = _draft(cid, [("P-001", 2)])
    assert second.superseded_draft_ids == [first.draft_id]
    assert _confirm(first).error_code == ErrorCode.DRAFT_NOT_PENDING
    assert _confirm(second).success


def test_cancel_draft(customer_id, sql):
    d = _draft(customer_id(ALI), [("P-001", 1)])
    assert cancel_draft(d.draft_id).status == "cancelled"
    assert cancel_draft(d.draft_id).error_code == ErrorCode.DRAFT_NOT_PENDING
    assert _confirm(d).error_code == ErrorCode.DRAFT_NOT_PENDING
    assert sql("SELECT count(*) FROM orders") == [(0,)]


# --------------------------------------------------------------------------- confirm_order

def test_confirm_creates_order_and_reserves_stock(customer_id, stock, sql):
    d = _draft(customer_id(ALI), [("P-001", 10), ("P-002", 5)])
    res = _confirm(d)
    assert res.success and not res.already_confirmed
    assert res.order_number == "ORD-000001" and res.total == Decimal("62230.00")
    assert stock("P-001") == (140, 10) and stock("P-002") == (95, 5)
    assert sql("SELECT product_id, qty, unit_price, line_total FROM order_items ORDER BY product_id") == [
        ("P-001", 10, Decimal("5400.00"), Decimal("54000.00")),
        ("P-002", 5, Decimal("1900.00"), Decimal("9500.00")),
    ]
    assert sql("SELECT status FROM order_drafts") == [("confirmed",)]


def test_duplicate_yes_creates_nothing(customer_id, stock, sql):
    d = _draft(customer_id(ALI), [("P-001", 5)])
    first, second = _confirm(d), _confirm(d)
    assert first.success and not first.already_confirmed
    assert second.success and second.already_confirmed and second.order_id == first.order_id
    assert sql("SELECT count(*) FROM orders") == [(1,)]
    assert stock("P-001") == (140, 5)  # reserved once


def test_wrong_idempotency_key(customer_id):
    d = _draft(customer_id(ALI), [("P-001", 1)])
    assert confirm_order(d.draft_id, "something-else").error_code == ErrorCode.INVALID_IDEMPOTENCY_KEY


def test_unknown_draft():
    did = uuid.uuid4()
    assert confirm_order(did, idempotency_key_for(did)).error_code == ErrorCode.DRAFT_NOT_FOUND


def test_expired_draft(customer_id, sql):
    d = _draft(customer_id(ALI), [("P-001", 1)])
    sql("UPDATE order_drafts SET expires_at = now() - interval '1 minute'")
    assert _confirm(d).error_code == ErrorCode.DRAFT_EXPIRED
    assert sql("SELECT status FROM order_drafts") == [("expired",)]
    assert sql("SELECT count(*) FROM orders") == [(0,)]


def test_price_change_blocks_confirmation(customer_id, stock, sql):
    d = _draft(customer_id(ALI), [("P-001", 10)])
    sql("UPDATE products SET unit_price = 5500 WHERE id = 'P-001'")
    res = _confirm(d)
    assert res.error_code == ErrorCode.PRICE_CHANGED
    assert res.details == {"old_total": "52920.00", "new_total": "53900.00"}
    assert stock("P-001") == (140, 0)
    assert sql("SELECT status FROM order_drafts") == [("pending",)]


def test_insufficient_stock_rolls_back_everything(customer_id, stock, sql):
    # Dalda line would succeed, Nestle line fails -> nothing reserved, no order.
    d = _draft(customer_id(KARACHI_MART), [("P-001", 10), ("P-006", 30)])
    res = _confirm(d)
    assert res.error_code == ErrorCode.INSUFFICIENT_STOCK
    assert res.details == {"product_id": "P-006", "requested": 30, "available": 18}
    assert stock("P-001") == (140, 0) and stock("P-006") == (18, 0)
    assert sql("SELECT count(*) FROM orders") == [(0,)]
    assert sql("SELECT status FROM order_drafts") == [("pending",)]


def test_stock_taken_after_summary_is_caught(customer_id, stock, sql):
    d = _draft(customer_id(KARACHI_MART), [("P-006", 18)])
    sql("UPDATE inventory SET reserved = 5 WHERE product_id = 'P-006'")  # someone else reserved 5
    assert _confirm(d).error_code == ErrorCode.INSUFFICIENT_STOCK
    assert stock("P-006") == (18, 5)


def test_concurrent_duplicate_confirms_make_one_order(customer_id, stock, sql):
    d = _draft(customer_id(ALI), [("P-001", 5)])
    results = []
    threads = [threading.Thread(target=lambda: results.append(_confirm(d))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.success for r in results)
    assert sorted(r.already_confirmed for r in results) == [False, True, True, True]
    assert sql("SELECT count(*) FROM orders") == [(1,)]
    assert stock("P-001") == (140, 5)


def test_concurrent_orders_cannot_oversell(customer_id, stock, sql):
    # Tapal: 8 available. Two different customers each confirm 5 at the same time.
    d1 = _draft(customer_id(ALI), [("P-008", 5)], thread="a")
    d2 = _draft(customer_id(KARACHI_MART), [("P-008", 5)], thread="b")
    results = []
    threads = [threading.Thread(target=lambda d=d: results.append(_confirm(d))) for d in (d1, d2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.success for r in results) == [False, True]
    assert [r.error_code for r in results if not r.success] == [ErrorCode.INSUFFICIENT_STOCK]
    assert stock("P-008") == (8, 5)
    assert sql("SELECT count(*) FROM orders") == [(1,)]


# --------------------------------------------------------------------------- generate_invoice

def test_generate_invoice_is_idempotent(customer_id, sql):
    order = _confirm(_draft(customer_id(ALI), [("P-001", 10), ("P-002", 5)]))
    inv1 = generate_invoice(order.order_id)
    inv2 = generate_invoice(order.order_id)
    assert inv1.success and not inv1.already_existed
    assert inv2.success and inv2.already_existed and inv2.invoice_number == inv1.invoice_number
    assert inv1.invoice_number == "INV-000001" and inv1.order_number == "ORD-000001"
    assert (inv1.customer_name, inv1.customer_address) == ("Ali Traders", "Shop 4 Saddar")
    assert (inv1.subtotal, inv1.discount_amount, inv1.total) == (
        Decimal("63500.00"), Decimal("1270.00"), Decimal("62230.00"))
    assert len(inv1.lines) == 2
    assert sql("SELECT count(*) FROM invoices") == [(1,)]


def test_generate_invoice_unknown_order():
    assert generate_invoice(424242).error_code == ErrorCode.ORDER_NOT_FOUND


# --------------------------------------------------------------------------- get_order_history (read-only)

def test_order_history_newest_first_and_per_customer(customer_id):
    ali, km = customer_id(ALI), customer_id(KARACHI_MART)
    first = _confirm(_draft(ali, [("P-001", 10), ("P-002", 5)]))
    generate_invoice(first.order_id)
    _confirm(_draft(km, [("P-007", 3)], thread="km"))
    second = _confirm(_draft(ali, [("P-001", 5)]))

    res = get_order_history(customer_id=ali)
    assert res.success and res.count == 2
    assert [o.order_number for o in res.orders] == [second.order_number, first.order_number]
    assert res.orders[1].invoice_number == "INV-000001" and res.orders[0].invoice_number is None
    assert (res.orders[1].subtotal, res.orders[1].discount_amount, res.orders[1].total) == (
        Decimal("63500.00"), Decimal("1270.00"), Decimal("62230.00"))
    assert [(l.product_id, l.qty) for l in res.orders[1].lines] == [("P-001", 10), ("P-002", 5)]


def test_customer_profile_from_past_orders(customer_id):
    ali = customer_id(ALI)
    assert get_customer_profile(ali).orders_count == 0
    _confirm(_draft(ali, [("P-001", 10), ("P-002", 5)]))
    _confirm(_draft(ali, [("P-001", 6)]))

    profile = get_customer_profile(ali)
    assert (profile.orders_count, profile.last_order_number, profile.preferred_language) == (2, "ORD-000002", "en")
    assert [(u.product_id, u.times_ordered, u.last_qty) for u in profile.usual_items] == [
        ("P-001", 2, 6), ("P-002", 1, 5)]


def test_order_history_by_number_only_own_orders(customer_id):
    order = _confirm(_draft(customer_id(ALI), [("P-001", 1)]))
    assert get_order_history(customer_id=customer_id(ALI), order_number=order.order_number.lower()).count == 1
    assert get_order_history(customer_id=customer_id(KARACHI_MART), order_number=order.order_number).count == 0
