"""Safety rules around the LLM: what happens when it decides wrongly, invents numbers, or the world changes."""
from __future__ import annotations

from app.agent.service import OrderAgent
from tests.conftest import ALI, KARACHI_MART
from tests.scenarios.conftest import FakeLLM, act, answer, items, one

ORDER = "send 10 cartons of dalda"
PLACE = act("place_order", items=items(("dalda", 10)), language="en")


def test_confirmation_check_blocks_a_wrong_confirm(make_agent, sql):
    """The LLM calls confirm on 'create an order for 1 packet' -> the check says NO -> nothing is confirmed."""
    tricky = "ok create an order for 1 packet of dalda oil"
    agent, fake = make_agent({
        ORDER: [PLACE],
        tricky: [act("confirm_pending_order", language="en"),          # wrong decision
                 answer("We only sell whole cartons. Reply yes or no to the pending summary.")],
    }, accepts=lambda message: message != tricky)

    agent.handle_message(ALI, ORDER)
    reply = agent.handle_message(ALI, tricky)
    assert reply.waiting_for == "confirmation" and reply.status == "answered"
    assert sql("SELECT count(*) FROM orders") == [(0,)]
    assert sql("SELECT status FROM order_drafts") == [("pending",)]
    rejected = fake.decide_calls[-1]["messages"][-1].content  # the LLM was told why
    assert rejected.startswith("REJECTED")


def test_loose_units_are_refused_in_code(make_agent, sql):
    agent, fake = make_agent({
        ORDER: [PLACE],
        "1 packet of dalda": [act("place_order", items=items(("dalda", 1), unit="loose"), language="en")],
    })
    agent.handle_message(ALI, ORDER)
    reply = agent.handle_message(ALI, "1 packet of dalda")
    assert reply.status == "loose_units" and reply.waiting_for == "confirmation"  # pending summary kept
    assert fake.write_calls[-1]["facts"]["items"] == [{"product": "dalda", "qty": 1, "sold_as": "carton of 12"}]
    assert sql("SELECT count(*), min(status) FROM order_drafts") == [(1, "pending")]  # no new draft


def test_cancel_pending_order(make_agent, sql, stock):
    agent, fake = make_agent({ORDER: [PLACE], "no": [act("cancel_pending_order", language="en")]})
    agent.handle_message(ALI, ORDER)
    reply = agent.handle_message(ALI, "no")
    assert reply.status == "cancelled" and reply.waiting_for is None
    assert sql("SELECT status FROM order_drafts") == [("cancelled",)]
    assert sql("SELECT count(*) FROM orders") == [(0,)] and stock("P-001") == (140, 0)


def test_cancel_with_nothing_pending_never_escalates(make_agent, sql):
    agent, fake = make_agent({ORDER: [PLACE], "yes": [act("confirm_pending_order", language="en")],
                              "no": [act("cancel_pending_order", language="en")]})
    agent.handle_message(ALI, ORDER)
    agent.handle_message(ALI, "yes")
    reply = agent.handle_message(ALI, "no")
    assert reply.status == "done"
    assert sql("SELECT count(*) FROM staff_notifications") == [(0,)]
    assert sql("SELECT status FROM orders") == [("confirmed",)]


def test_cancel_confirmed_order_goes_to_a_human(make_agent, sql):
    msg = "cancel ORD-000001 please"
    agent, fake = make_agent({
        ORDER: [PLACE], "yes": [act("confirm_pending_order", language="en")],
        msg: [act("request_cancel_confirmed_order", order_number="ORD-000001", reason="changed mind", language="en")],
    })
    agent.handle_message(ALI, ORDER)
    agent.handle_message(ALI, "yes")
    reply = agent.handle_message(ALI, msg)
    assert reply.status == "handed_off"
    assert sql("SELECT status FROM orders") == [("confirmed",)]          # NOT cancelled by the bot
    (row,) = sql("SELECT role, reason FROM staff_notifications")
    assert row[0] == "sales" and "ORD-000001" in row[1]


def test_invented_number_never_reaches_the_customer(make_agent):
    q = "how much is 3 cartons of tapal?"
    agent, fake = make_agent({q: [
        answer("3 cartons of Tapal cost Rs 42,000, total Rs 41,160."),        # LLM calculated: rejected
        act("get_prices", items=[{"product_id": "P-008", "qty": 3}]),         # retry: asks the tool
        answer("3 cartons of Tapal: Rs 42,000, discount Rs 840, total Rs 41,160."),
    ]})
    reply = agent.handle_message(ALI, q)
    assert one(reply) == "3 cartons of Tapal: Rs 42,000, discount Rs 840, total Rs 41,160."
    assert fake.decide_calls[1]["messages"][-1].content.startswith("(system) Your reply has numbers not backed")


def test_invented_number_twice_gives_safe_error(make_agent):
    q = "what is the total for my usual order?"
    agent, fake = make_agent({q: [answer("Your usual order is Rs 99,999."),
                                  answer("Sorry, it is Rs 88,888.")]}, allow_errors=True)
    reply = agent.handle_message(ALI, q)
    assert "99,999" not in one(reply) and "88,888" not in one(reply)


def test_order_history_answer_uses_tool_data(make_agent):
    q = "list my orders"
    agent, fake = make_agent({
        ORDER: [PLACE], "yes": [act("confirm_pending_order", language="en")],
        q: [act("get_order_history"), answer("Your orders: ORD-000001, total Rs 52,920 (discount Rs 1,080).")],
    })
    agent.handle_message(ALI, ORDER)
    agent.handle_message(ALI, "yes")
    reply = agent.handle_message(ALI, q)
    assert one(reply) == "Your orders: ORD-000001, total Rs 52,920 (discount Rs 1,080)."


def test_price_change_after_summary_gives_new_summary(make_agent, sql):
    agent, fake = make_agent({ORDER: [PLACE], "yes": [act("confirm_pending_order", language="en"),
                                                     act("confirm_pending_order", language="en")]})
    assert "Rs 52,920" in one(agent.handle_message(ALI, ORDER))
    sql("UPDATE products SET unit_price = 5500 WHERE id = 'P-001'")  # price rises before the customer says yes

    reply = agent.handle_message(ALI, "yes")
    assert reply.waiting_for == "confirmation" and sql("SELECT count(*) FROM orders") == [(0,)]
    assert len(reply.messages) == 2 and "Rs 53,900" in reply.messages[1]  # "prices changed" + new summary
    assert sorted(sql("SELECT status FROM order_drafts")) == [("cancelled",), ("pending",)]

    done = agent.handle_message(ALI, "yes")
    assert done.status == "confirmed" and sql("SELECT total::text FROM orders") == [("53900.00",)]


def test_stock_taken_after_summary_offers_options(make_agent, sql):
    msg = "18 cartons nestle"
    agent, fake = make_agent({msg: [act("place_order", items=items(("nestle", 18)), language="en")],
                              "yes": [act("confirm_pending_order", language="en")]})
    agent.handle_message(KARACHI_MART, msg)
    sql("UPDATE inventory SET reserved = 10 WHERE product_id = 'P-006'")  # someone else took 10
    reply = agent.handle_message(KARACHI_MART, "yes")
    assert reply.waiting_for == "stock_choice" and sql("SELECT count(*) FROM orders") == [(0,)]
    assert fake.write_calls[-1]["facts"]["available"] == 8


def test_conversation_survives_a_restart(make_agent, checkpointer, sql):
    """State is in Postgres: a brand-new agent (e.g. after a server restart) continues the conversation."""
    agent, _ = make_agent({ORDER: [PLACE]})
    agent.handle_message(ALI, ORDER)

    fresh = OrderAgent(FakeLLM({"yes": [act("confirm_pending_order", language="en")]}), checkpointer)
    assert fresh.handle_message(ALI, "yes").status == "confirmed"
    assert sql("SELECT count(*) FROM orders") == [(1,)]


def test_every_step_is_logged(make_agent, sql):
    agent, _ = make_agent({ORDER: [PLACE]})
    agent.handle_message(ALI, ORDER)
    kinds = dict(sql("SELECT kind, count(*) FROM workflow_logs WHERE thread_id = '+923001234567' GROUP BY kind"))
    assert set(kinds) == {"node", "llm", "tool", "guard"}
    assert sql("SELECT count(DISTINCT run_id) FROM workflow_logs WHERE thread_id = '+923001234567'") == [(1,)]
    assert sql("SELECT count(*) FROM workflow_logs WHERE kind = 'tool' AND duration_ms IS NULL") == [(0,)]
