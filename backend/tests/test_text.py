"""Formatting, backup templates and the number guard (no DB, no LLM)."""
import pytest

from app.agent.text import backup, display, number_guard, rs

SUMMARY = {"customer_name": "Ali Traders", "subtotal": "63500.00", "discount_pct": "2.00",
           "discount_amount": "1270.00", "total": "62230.00",
           "lines": [{"name": "Dalda Cooking Oil 1L", "unit": "carton", "qty": 10, "unit_price": "5400.00",
                      "line_total": "54000.00"}]}


def test_rs_formatting():
    assert (rs("62230.00"), rs("1234.5"), rs(0)) == ("Rs 62,230", "Rs 1,234.50", "Rs 0")


def test_display_formats_money_for_the_llm():
    d = display(SUMMARY)
    assert (d["total"], d["discount_pct"], d["lines"][0]["unit_price"], d["lines"][0]["qty"]) == (
        "Rs 62,230", "2%", "Rs 5,400", 10)


def test_backup_summary_has_total():
    assert "Total: Rs 62,230" in backup("summary", SUMMARY)


def test_guard_accepts_reply_using_only_facts():
    reply = "Ali Traders: 10 cartons Dalda = Rs 54,000. Discount 2% (Rs 1,270). Total Rs 62,230. Reply yes."
    assert number_guard(str(display(SUMMARY)), reply, required=["62230.00"]) == (True, "ok")


def test_guard_rejects_invented_number():
    ok, reason = number_guard(str(display(SUMMARY)), "Total Rs 62,320", required=["62230.00"])
    assert not ok and "62320" in reason


def test_guard_rejects_missing_total():
    ok, reason = number_guard(str(display(SUMMARY)), "Your order is ready, reply yes.", required=["62230.00"])
    assert not ok and "missing" in reason


def test_guard_handles_urdu_digits():
    assert number_guard("Total Rs 62,230", "کل رقم ۶۲۲۳۰ روپے", required=["62230"])[0]
