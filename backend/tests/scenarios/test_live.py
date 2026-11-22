from __future__ import annotations

import os

import pytest

from tests.conftest import ALI, BISMILLAH, KARACHI_MART, MADINA, UNKNOWN

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("RUN_LIVE_LLM") != "1", reason="live Groq run: set RUN_LIVE_LLM=1 (costs tokens)"),
]

SCENARIOS = [
    ("s1", ALI, ["Send 10 cartons of Dalda oil and 5 cartons of Sooper biscuits", "yes"], "62230.00"),
    ("s2", BISMILLAH, ["20 bags rice", "1", "yes"], "47530.00"),
    ("s3", KARACHI_MART, ["30 cartons Nestle water 1.5L", "2", "yes"], "21240.00"),
    ("s4", ALI, ["5 more cartons of Dalda please", "yes"], "26460.00"),
    ("s6", MADINA, ["bhai 10 carton dalda aur 5 sooper bhej do", "haan theek hai, bhej do"], "62230.00"),
]


@pytest.fixture
def live_agent(fresh_db, checkpointer):
    from app.agent.llm import GroqLLM
    from app.agent.service import OrderAgent

    return OrderAgent(GroqLLM(model=os.getenv("LIVE_MODEL") or None), checkpointer)


@pytest.mark.parametrize("name, phone, messages, total", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_live_scenario(live_agent, sql, name, phone, messages, total):
    replies = [live_agent.handle_message(phone, m) for m in messages]
    assert replies[-1].status == "confirmed", [r.messages for r in replies]
    assert sql("SELECT total::text FROM orders") == [(total,)]


def test_live_duplicate_yes_is_silent(live_agent, sql):
    live_agent.handle_message(ALI, "5 more cartons of Dalda please")
    live_agent.handle_message(ALI, "yes")
    again = live_agent.handle_message(ALI, "yes")
    assert again.messages == [] and sql("SELECT count(*) FROM orders") == [(1,)]


def test_live_unknown_number(live_agent, sql):
    reply = live_agent.handle_message(UNKNOWN, "Send 5 cartons of tea")
    assert reply.status == "handoff" and sql("SELECT count(*) FROM orders") == [(0,)]
