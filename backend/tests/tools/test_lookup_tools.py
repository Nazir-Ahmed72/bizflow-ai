"""identify_customer, search_products, check_stock, suggest_alternatives, get_prices, notify_staff."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.tools import (
    ErrorCode, check_stock, get_prices, identify_customer, list_products, log_context, notify_staff,
    search_products, suggest_alternatives,
)
from tests.conftest import ALI, BISMILLAH, KARACHI_MART, MADINA, UNKNOWN


# --------------------------------------------------------------------------- identify_customer

@pytest.mark.parametrize("phone", [ALI, "+923001234567", "0300-1234567"])
def test_identify_known_customer_any_format(phone):
    res = identify_customer(phone)
    assert res.success
    assert (res.name, res.phone, res.price_category, res.discount_pct) == (
        "Ali Traders", "+923001234567", "B", Decimal("2.00"))


def test_identify_unknown_customer():
    res = identify_customer(UNKNOWN)
    assert not res.success and res.error_code == ErrorCode.CUSTOMER_NOT_FOUND


def test_identify_invalid_phone():
    res = identify_customer("hello")
    assert not res.success and res.error_code == ErrorCode.INVALID_PHONE


def test_identify_empty_input_is_invalid_input():
    res = identify_customer("")
    assert not res.success and res.error_code == ErrorCode.INVALID_INPUT


# --------------------------------------------------------------------------- list_products

def test_list_products_whole_catalog_with_stock(sql):
    sql("UPDATE inventory SET reserved = physical WHERE product_id = 'P-008'")
    res = list_products()
    assert res.success and res.count == 8
    tapal = next(p for p in res.products if p.product_id == "P-008")
    assert (tapal.pack, tapal.available, tapal.in_stock) == ("carton of 12", 0, False)


def test_list_products_by_category():
    assert [p.product_id for p in list_products(category="Rice").products] == ["P-003", "P-004", "P-005"]


# --------------------------------------------------------------------------- search_products

@pytest.mark.parametrize("query, expected", [
    ("Dalda oil", ["P-001"]),
    ("dalda", ["P-001"]),
    ("Sooper biscuits", ["P-002"]),
    ("sooper", ["P-002"]),
    ("Nestle water 1.5L", ["P-006"]),
    ("tea", ["P-008"]),
    ("chai", ["P-008"]),
    ("basmati rice", ["P-003"]),
])
def test_search_unique_match(query, expected):
    res = search_products(query)
    assert res.success and not res.ambiguous
    assert [m.product_id for m in res.matches] == expected


def test_search_rice_is_ambiguous():
    res = search_products("rice")
    assert res.success and res.ambiguous
    assert [m.product_id for m in res.matches] == ["P-003", "P-004", "P-005"]


def test_search_water_is_ambiguous():
    res = search_products("pani")
    assert res.ambiguous and [m.product_id for m in res.matches] == ["P-006", "P-007"]


def test_search_no_match_is_empty_success():
    res = search_products("laptop")
    assert res.success and res.matches == [] and not res.ambiguous


# --------------------------------------------------------------------------- check_stock

def test_check_stock_sufficient():
    res = check_stock([{"product_id": "P-001", "qty": 10}, {"product_id": "p-002", "qty": 5}])
    assert res.success and res.all_available
    assert [(i.product_id, i.available, i.shortfall) for i in res.items] == [("P-001", 140, 0), ("P-002", 95, 0)]


def test_check_stock_short_nestle():
    res = check_stock(items=[{"product_id": "P-006", "qty": 30}])
    assert res.success and not res.all_available
    line = res.items[0]
    assert (line.available, line.sufficient, line.shortfall) == (18, False, 12)


def test_check_stock_merges_duplicate_lines():
    res = check_stock(items=[{"product_id": "P-008", "qty": 5}, {"product_id": "P-008", "qty": 5}])
    assert len(res.items) == 1 and res.items[0].requested == 10 and res.items[0].shortfall == 2


def test_check_stock_unknown_product():
    res = check_stock(items=[{"product_id": "P-999", "qty": 1}])
    assert not res.success and res.error_code == ErrorCode.UNKNOWN_PRODUCT


@pytest.mark.parametrize("items", [[], [{"product_id": "P-001", "qty": 0}], [{"product_id": "P-001", "qty": -3}]])
def test_check_stock_rejects_bad_input(items):
    res = check_stock(items=items)
    assert not res.success and res.error_code == ErrorCode.INVALID_INPUT


def test_check_stock_respects_reservations(sql):
    sql("UPDATE inventory SET reserved = 10 WHERE product_id = 'P-006'")
    assert check_stock(items=[{"product_id": "P-006", "qty": 10}]).items[0].available == 8


# --------------------------------------------------------------------------- suggest_alternatives

def test_suggest_aquafina_for_nestle():
    res = suggest_alternatives("P-006", 12)
    assert res.success
    assert res.available_of_requested == 18
    assert [(a.product_id, a.available, a.can_cover) for a in res.alternatives] == [("P-007", 64, True)]


def test_suggest_rice_alternatives_full_cover_first():
    res = suggest_alternatives("P-003", 40)
    assert [a.product_id for a in res.alternatives] == ["P-004", "P-005"]  # 42 covers 40, 30 doesn't
    assert [a.can_cover for a in res.alternatives] == [True, False]


def test_suggest_none_for_tea():
    res = suggest_alternatives("P-008", 5)
    assert res.success and res.alternatives == []


def test_suggest_skips_out_of_stock(sql):
    sql("UPDATE inventory SET reserved = physical WHERE product_id = 'P-007'")
    assert suggest_alternatives("P-006", 12).alternatives == []


def test_suggest_unknown_product():
    assert suggest_alternatives("P-999", 1).error_code == ErrorCode.UNKNOWN_PRODUCT


# --------------------------------------------------------------------------- get_prices (scenario totals)

@pytest.mark.parametrize("phone, items, subtotal, discount, total", [
    (ALI, [("P-001", 10), ("P-002", 5)], "63500.00", "1270.00", "62230.00"),       # scenario 1
    (BISMILLAH, [("P-003", 20)], "49000.00", "1470.00", "47530.00"),               # scenario 2
    (KARACHI_MART, [("P-006", 18), ("P-007", 12)], "21240.00", "0.00", "21240.00"),  # scenario 3
    (ALI, [("P-001", 5)], "27000.00", "540.00", "26460.00"),                       # scenario 4
    (MADINA, [("P-001", 10), ("P-002", 5)], "63500.00", "1270.00", "62230.00"),    # scenario 6
])
def test_get_prices_scenarios(customer_id, phone, items, subtotal, discount, total):
    res = get_prices(customer_id(phone), [{"product_id": p, "qty": q} for p, q in items])
    assert res.success, res
    assert (res.subtotal, res.discount_amount, res.total) == (Decimal(subtotal), Decimal(discount), Decimal(total))
    assert all(line.line_total == line.unit_price * line.qty for line in res.lines)


def test_get_prices_rounds_half_up(customer_id, sql):
    sql("UPDATE products SET unit_price = 10.25 WHERE id = 'P-007'")  # 2% of 10.25 = 0.205 -> 0.21
    res = get_prices(customer_id(ALI), [{"product_id": "P-007", "qty": 1}])
    assert (res.discount_amount, res.total) == (Decimal("0.21"), Decimal("10.04"))


def test_get_prices_unknown_customer():
    res = get_prices(99999, [{"product_id": "P-001", "qty": 1}])
    assert res.error_code == ErrorCode.CUSTOMER_NOT_FOUND


def test_get_prices_unknown_product(customer_id):
    res = get_prices(customer_id(ALI), [{"product_id": "P-001", "qty": 1}, {"product_id": "X-1", "qty": 1}])
    assert res.error_code == ErrorCode.UNKNOWN_PRODUCT and res.details["product_ids"] == ["X-1"]


# --------------------------------------------------------------------------- notify_staff + logging

def test_notify_staff_records_thread(sql):
    with log_context(thread_id="+923339876543", run_id="run-1"):
        res = notify_staff("sales", "Unknown number ordered 5 cartons of tea")
    assert res.success and res.thread_id == "+923339876543"
    rows = sql("SELECT role, thread_id, status FROM staff_notifications")
    assert rows == [("sales", "+923339876543", "open")]


def test_notify_staff_invalid_role():
    assert notify_staff("ceo", "x").error_code == ErrorCode.INVALID_INPUT


def test_tool_calls_are_logged(sql):
    with log_context(thread_id="t-log", run_id="r-log"):
        identify_customer(ALI)
        identify_customer(UNKNOWN)
    rows = sql("SELECT step, kind, success, error_code, duration_ms, run_id FROM workflow_logs "
               "WHERE thread_id = 't-log' ORDER BY id")
    assert [(r[0], r[1], r[2], r[3], r[5]) for r in rows] == [
        ("identify_customer", "tool", True, None, "r-log"),
        ("identify_customer", "tool", False, "CUSTOMER_NOT_FOUND", "r-log"),
    ]
    assert all(r[4] is not None and r[4] >= 0 for r in rows)
