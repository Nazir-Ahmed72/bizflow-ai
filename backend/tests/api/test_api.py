from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.tools import conversation_lock
from tests.conftest import ALI, UNKNOWN
from tests.scenarios.conftest import FakeLLM, act, items

ORDER = "Send 10 cartons of Dalda oil and 5 cartons of Sooper biscuits"


@pytest.fixture
def api(fresh_db):
    def make(script):
        fake = FakeLLM(script)
        return TestClient(create_app(llm=fake)), fake

    return make


def test_health(api):
    client, _ = api({})
    with client:
        assert client.get("/health").json() == {"status": "ok"}


def test_chat_order_flow_over_http(api, sql):
    client, _ = api({ORDER: [act("place_order", items=items(("Dalda oil", 10), ("Sooper biscuits", 5)), language="en")],
                     "yes": [act("confirm_pending_order", language="en")]})
    with client:
        first = client.post("/chat", json={"phone": ALI, "message": ORDER})
        assert first.status_code == 200
        body = first.json()
        assert body["thread_id"] == "+923001234567" and body["waiting_for"] == "confirmation"
        assert "Rs 62,230" in body["messages"][0]

        done = client.post("/chat", json={"phone": ALI, "message": "yes"}).json()
        assert (done["status"], done["order_number"], done["invoice_number"]) == ("confirmed", "ORD-000001", "INV-000001")
        assert done["waiting_for"] is None
    assert sql("SELECT count(*) FROM orders") == [(1,)]


def test_unknown_number_over_http(api):
    client, fake = api({})
    with client:
        body = client.post("/chat", json={"phone": UNKNOWN, "message": "Send 5 cartons of tea"}).json()
    assert body["status"] == "handoff" and len(body["messages"]) == 1


@pytest.mark.parametrize("payload", [{"phone": ALI, "message": ""}, {"message": "hi"}, {"phone": ALI}, {}])
def test_bad_input_is_rejected(api, payload):
    client, _ = api({})
    with client:
        assert client.post("/chat", json=payload).status_code == 422


def test_conversation_endpoint(api):
    client, _ = api({ORDER: [act("place_order", items=items(("Dalda oil", 10), ("Sooper biscuits", 5)), language="en")]})
    with client:
        client.post("/chat", json={"phone": ALI, "message": ORDER})
        body = client.get("/conversations/0300-1234567").json()
    assert body["thread_id"] == "+923001234567" and body["waiting_for"] == "confirmation"
    assert [m["direction"] for m in body["messages"]] == ["in", "out"]
    assert body["messages"][0]["text"] == ORDER


def test_simultaneous_yes_makes_one_order_and_one_message(api, sql):
    client, _ = api({ORDER: [act("place_order", items=items(("Dalda oil", 10), ("Sooper biscuits", 5)), language="en")],
                     "yes": [act("confirm_pending_order", language="en"), act("confirm_pending_order", language="en")]})
    with client:
        client.post("/chat", json={"phone": ALI, "message": ORDER})
        agent = client.app.state.agent
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(agent.handle_message(ALI, "yes"))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert sorted(r.status for r in replies) == ["confirmed", "duplicate_ignored"]
    assert sum(len(r.messages) for r in replies) == 1
    assert sql("SELECT count(*) FROM orders") == [(1,)]


def test_conversation_lock_makes_messages_wait_in_line(migrated_db):
    events = []

    def first():
        with conversation_lock("+920000000001"):
            events.append("first in")
            time.sleep(1.5)
            events.append("first out")

    t = threading.Thread(target=first)
    t.start()
    time.sleep(0.5)
    with conversation_lock("+920000000001"):
        events.append("second in")
    t.join()
    assert events == ["first in", "first out", "second in"]


def test_busy_conversation_returns_409(api, monkeypatch):
    monkeypatch.setattr("app.tools.notify.LOCK_TIMEOUT", "500ms")
    client, _ = api({})
    held = threading.Event()
    release = threading.Event()

    def hold():
        with conversation_lock("+923001234567"):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(10)
    try:
        with client:
            response = client.post("/chat", json={"phone": ALI, "message": "hi"})
    finally:
        release.set()
        t.join()
    assert response.status_code == 409
