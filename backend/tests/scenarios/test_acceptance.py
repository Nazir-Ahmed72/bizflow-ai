"""The 6 acceptance scenarios from the spec, end to end, with a scripted fake LLM (no API key needed)."""
from __future__ import annotations

from tests.conftest import ALI, BISMILLAH, KARACHI_MART, MADINA, UNKNOWN
from tests.scenarios.conftest import act, items, one


def _orders(sql, phone_e164: str):
    return sql("SELECT o.order_number, o.total::text FROM orders o JOIN customers c ON c.id = o.customer_id "
               "WHERE c.phone = :p ORDER BY o.id", p=phone_e164)


# --------------------------------------------------------------------------- 1

def test_s1_ali_dalda_and_sooper(make_agent, sql, stock):
    order_msg = "Send 10 cartons of Dalda oil and 5 cartons of Sooper biscuits"
    agent, fake = make_agent({
        order_msg: [act("place_order", items=items(("Dalda oil", 10), ("Sooper biscuits", 5)), language="en")],
        "yes": [act("confirm_pending_order", language="en")],
    })

    summary = agent.handle_message(ALI, order_msg)
    assert summary.waiting_for == "confirmation"
    text = one(summary)
    assert "Rs 63,500" in text and "Rs 1,270" in text and "Rs 62,230" in text
    # A summary is not an order: nothing created, nothing reserved yet.
    assert _orders(sql, "+923001234567") == []
    assert stock("P-001") == (140, 0)

    done = agent.handle_message(ALI, "yes")
    assert (done.status, done.order_number, done.invoice_number) == ("confirmed", "ORD-000001", "INV-000001")
    assert "ORD-000001" in one(done) and "Rs 62,230" in one(done)
    assert _orders(sql, "+923001234567") == [("ORD-000001", "62230.00")]
    assert stock("P-001") == (140, 10) and stock("P-002") == (95, 5)
    assert sql("SELECT count(*) FROM invoices") == [(1,)]


# --------------------------------------------------------------------------- 2

def test_s2_bismillah_rice_choice(make_agent, sql, stock):
    agent, fake = make_agent({
        "20 bags rice": [act("place_order", items=items(("rice", 20)), language="en")],
        "1": [act("choose_option", option_number=1, language="en")],
        "yes": [act("confirm_pending_order", language="en")],
    })

    ask = agent.handle_message(BISMILLAH, "20 bags rice")
    assert ask.waiting_for == "product_choice"
    options = [o["name"] for o in fake.write_calls[-1]["facts"]["options"]]
    assert options == ["Basmati Super Kernel 5kg", "Sella Rice 5kg", "Broken Rice 25kg"]
    assert sql("SELECT count(*) FROM order_drafts") == [(0,)]  # nothing priced until the product is clear

    summary = agent.handle_message(BISMILLAH, "1")
    assert summary.waiting_for == "confirmation" and "Rs 47,530" in one(summary)

    done = agent.handle_message(BISMILLAH, "yes")
    assert done.status == "confirmed"
    assert _orders(sql, "+923215550192") == [("ORD-000001", "47530.00")]
    assert stock("P-003") == (60, 20)


# --------------------------------------------------------------------------- 3

def test_s3_karachi_mart_short_stock_substitute(make_agent, sql, stock):
    msg = "30 cartons Nestle water 1.5L"
    agent, fake = make_agent({
        msg: [act("place_order", items=items(("Nestle water 1.5L", 30)), language="en")],
        "2": [act("choose_option", option_number=2, language="en")],
        "yes": [act("confirm_pending_order", language="en")],
    })

    offer = agent.handle_message(KARACHI_MART, msg)
    assert offer.waiting_for == "stock_choice"
    facts = fake.write_calls[-1]["facts"]
    assert (facts["requested"], facts["available"]) == (30, 18)
    labels = [o["label"] for o in facts["options"]]
    assert labels == ["Take 18 cartons Nestle Pure Life 1.5L only",
                      "Take 18 cartons Nestle Pure Life 1.5L + 12 cartons Aquafina 1.5L",
                      "Cancel the order"]

    summary = agent.handle_message(KARACHI_MART, "2")
    assert "Rs 21,240" in one(summary)

    done = agent.handle_message(KARACHI_MART, "yes")
    assert done.status == "confirmed"
    assert _orders(sql, "+923452201876") == [("ORD-000001", "21240.00")]
    assert sql("SELECT product_id, qty FROM order_items ORDER BY product_id") == [("P-006", 18), ("P-007", 12)]
    assert stock("P-006") == (18, 18) and stock("P-007") == (64, 12)


# --------------------------------------------------------------------------- 4

def test_s4_ali_more_dalda_duplicate_yes(make_agent, sql, stock):
    msg = "5 more cartons of Dalda please"
    agent, fake = make_agent({
        msg: [act("place_order", items=items(("Dalda", 5)), language="en")],
        "yes": [act("confirm_pending_order", language="en"), act("confirm_pending_order", language="en")],
    })

    assert "Rs 26,460" in one(agent.handle_message(ALI, msg))
    done = agent.handle_message(ALI, "yes")
    assert done.status == "confirmed" and done.order_number == "ORD-000001"

    again = agent.handle_message(ALI, "yes")
    assert again.messages == []                      # no second message
    assert again.status == "duplicate_ignored"
    assert _orders(sql, "+923001234567") == [("ORD-000001", "26460.00")]  # no second order
    assert stock("P-001") == (140, 5)                # reserved once
    assert sql("SELECT count(*) FROM messages WHERE direction = 'out'") == [(2,)]


# --------------------------------------------------------------------------- 5

def test_s5_unknown_number_handoff(make_agent, sql):
    agent, fake = make_agent({})  # the LLM is never asked to decide for an unknown number

    reply = agent.handle_message(UNKNOWN, "Send 5 cartons of tea")
    assert reply.status == "handoff" and len(reply.messages) == 1
    assert fake.decide_calls == []
    assert sql("SELECT count(*) FROM orders") == [(0,)]
    assert sql("SELECT count(*) FROM order_drafts") == [(0,)]
    assert sql("SELECT role, thread_id, status FROM staff_notifications") == [("sales", "+923339876543", "open")]


# --------------------------------------------------------------------------- 6

def test_s6_madina_roman_urdu(make_agent, sql):
    msg, yes = "bhai 10 carton dalda aur 5 sooper bhej do", "haan theek hai, bhej do"
    agent, fake = make_agent({
        msg: [act("place_order", items=items(("dalda", 10), ("sooper", 5)), language="roman_ur")],
        yes: [act("confirm_pending_order", language="roman_ur")],
    })

    summary = agent.handle_message(MADINA, msg)
    assert one(summary).startswith("[roman_ur]") and "Rs 62,230" in one(summary)

    done = agent.handle_message(MADINA, yes)
    assert done.status == "confirmed" and one(done).startswith("[roman_ur]")
    assert _orders(sql, "+923124478210") == [("ORD-000001", "62230.00")]
    assert sql("SELECT language FROM order_drafts") == [("roman_ur",)]
    assert [w["message"] for w in fake.write_calls if w["kind"] == "confirm_check"] == [yes]
