from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from tests.conftest import ALI
from tests.scenarios.conftest import act, answer, items, one


def test_focus_memory_remembers_the_product_being_discussed(make_agent):
    agent, fake = make_agent({
        "price of dalda?": [act("search_products", query="dalda"),
                            answer("Dalda Cooking Oil 1L is Rs 5,400 per carton of 12.")],
        "how many bottles in a carton?": [answer("A carton of Dalda has 12 bottles.")],
    })
    agent.handle_message(ALI, "price of dalda?")
    reply = agent.handle_message(ALI, "how many bottles in a carton?")

    assert one(reply) == "A carton of Dalda has 12 bottles."
    system = fake.decide_calls[-1]["system"]
    assert "CURRENTLY DISCUSSING: Dalda Cooking Oil 1L (P-001, carton of 12, Rs 5,400 per pack)" in system


def test_focus_memory_follows_the_order(make_agent):
    agent, fake = make_agent({
        "send 5 cartons of sooper": [act("place_order", items=items(("sooper", 5)), language="en")],
        "what is the pack size?": [answer("Sooper comes in a carton of 24.")],
    })
    agent.handle_message(ALI, "send 5 cartons of sooper")
    agent.handle_message(ALI, "what is the pack size?")
    assert "CURRENTLY DISCUSSING: Sooper Classic biscuits (P-002, carton of 24" in fake.decide_calls[-1]["system"]


def test_summary_memory_compresses_old_messages(make_agent, monkeypatch):
    monkeypatch.setattr("app.agent.graph.SUMMARY_TRIGGER", 4)
    monkeypatch.setattr("app.agent.graph.KEEP_RECENT", 2)
    agent, fake = make_agent({
        "hi": [answer("Hello! How can I help?")],
        "do you sell oil?": [answer("Yes, we sell Dalda oil.")],
        "and biscuits?": [answer("Yes, Sooper biscuits too.")],
    })
    for text in ("hi", "do you sell oil?", "and biscuits?"):
        agent.handle_message(ALI, text)

    summaries = [w for w in fake.write_calls if w["kind"] == "summary"]
    assert len(summaries) == 1
    assert "Customer: hi" in summaries[0]["prompt"] and "Customer: do you sell oil?" in summaries[0]["prompt"]

    last = fake.decide_calls[-1]
    assert "CONVERSATION SUMMARY (older messages):\nCustomer chatted about Dalda oil" in last["system"]
    assert [type(m) for m in last["messages"]] == [AIMessage, HumanMessage]
    assert [m.content for m in last["messages"]] == ["Yes, we sell Dalda oil.", "and biscuits?"]


def test_summary_with_invented_number_is_rejected(make_agent, monkeypatch, sql):
    monkeypatch.setattr("app.agent.graph.SUMMARY_TRIGGER", 4)
    monkeypatch.setattr("app.agent.graph.KEEP_RECENT", 2)
    agent, fake = make_agent({
        "hi": [answer("Hello!")], "oil?": [answer("We have Dalda.")], "ok": [answer("Anything else?")],
    })
    fake.summary_text = "Customer wants 99 cartons of oil."
    for text in ("hi", "oil?", "ok"):
        agent.handle_message(ALI, text)

    last = fake.decide_calls[-1]
    assert "CONVERSATION SUMMARY" not in last["system"]
    assert [m.content for m in last["messages"]] == ["Hello!", "oil?", "We have Dalda.", "ok"]
    assert sql("SELECT success FROM workflow_logs WHERE step = 'summary_numbers'") == [(False,)]


def test_long_term_profile_enables_usual_order(make_agent):
    first = "send 10 cartons of dalda"
    usual = "send my usual order"
    agent, fake = make_agent({
        first: [act("place_order", items=items(("dalda", 10)), language="en")],
        "yes": [act("confirm_pending_order", language="en")],
        usual: [act("place_order", items=items(("Dalda Cooking Oil 1L", 10)), language="en")],
    })
    assert "CUSTOMER HISTORY: no past orders." in (agent.handle_message(ALI, first) and fake.decide_calls[-1]["system"])
    agent.handle_message(ALI, "yes")

    reply = agent.handle_message(ALI, usual)
    system = fake.decide_calls[-1]["system"]
    assert ("CUSTOMER HISTORY: 1 past orders, last ORD-000001. "
            "Usually orders: Dalda Cooking Oil 1L (P-001, last time 10, ordered 1 times).") in system
    assert "Language used last time: en." in system
    assert reply.waiting_for == "confirmation" and "Rs 52,920" in one(reply)
