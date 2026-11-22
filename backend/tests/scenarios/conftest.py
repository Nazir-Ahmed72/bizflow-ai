"""Scenario tests: the real graph, tools, Neon test DB and Postgres checkpointer, with a SCRIPTED fake LLM.

The fake replaces only the model's judgement: for each customer message, the test scripts what the
LLM would decide (call an action tool, call a read tool, or answer in text). Everything after that
decision - drafts, prices, stock, confirmation, idempotency, guards - is the real code.
"""
from __future__ import annotations

import itertools
import json
import re
from typing import Callable, Optional

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.llm import CONFIRM_CHECK_SYSTEM, SUMMARY_SYSTEM

_ids = itertools.count(1)


def act(name: str, **args) -> AIMessage:
    """Scripted decision: the LLM calls a tool (action or read-only)."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}",
                                             "type": "tool_call"}])


def answer(text: str) -> AIMessage:
    """Scripted decision: the LLM replies in text."""
    return AIMessage(content=text)


def items(*pairs, unit: str = "pack") -> list[dict]:
    return [{"product_phrase": p, "qty": q, "unit": unit} for p, q in pairs]


class FakeLLM:
    """Implements OrderLLM. decide() pops the next scripted decision for the current customer message.

    write() (used to phrase replies from facts) returns "[<language>] <FACTS json>": every number comes
    straight from the facts, so the number guard passes and tests can assert on totals and language.
    """

    def __init__(self, script: dict[str, list[AIMessage]],
                 accepts: Callable[[str], bool] = lambda message: True):
        self.script = {k: list(v) for k, v in script.items()}
        self.accepts = accepts          # answer of the confirmation check for a given message
        self.summary_text = "Customer chatted about Dalda oil and asked questions."
        self.decide_calls: list[dict] = []
        self.write_calls: list[dict] = []

    def decide(self, system, messages, tools):
        customer_msg = next(m.content for m in reversed(messages)
                            if isinstance(m, HumanMessage) and not str(m.content).startswith("(system)"))
        self.decide_calls.append({"message": customer_msg, "system": system, "messages": list(messages)})
        queue = self.script.get(customer_msg)
        if not queue:
            raise AssertionError(f"no scripted decision left for customer message {customer_msg!r}")
        return queue.pop(0)

    def write(self, system, prompt):
        if system == CONFIRM_CHECK_SYSTEM:
            message = prompt.split("CUSTOMER MESSAGE: ", 1)[1]
            self.write_calls.append({"kind": "confirm_check", "message": message})
            return "YES" if self.accepts(message) else "NO"
        if system == SUMMARY_SYSTEM:
            self.write_calls.append({"kind": "summary", "prompt": prompt})
            return self.summary_text
        language = re.search(r"Language: (\w+)", system).group(1)
        task = prompt.split("TASK: ", 1)[1].split("\n", 1)[0]
        facts = prompt.split("FACTS: ", 1)[1]
        self.write_calls.append({"kind": "reply", "language": language, "task": task, "facts": json.loads(facts)})
        return f"[{language}] {facts}"


@pytest.fixture(scope="session")
def checkpointer(migrated_db):
    from app.agent.service import postgres_checkpointer

    with postgres_checkpointer() as saver:
        yield saver


class _CheckedAgent:
    """OrderAgent wrapper: if a turn ends in the bot's error reply, fail with the error logs attached."""

    def __init__(self, agent, sql):
        self._agent, self._sql = agent, sql

    def handle_message(self, phone: str, text: str):
        reply = self._agent.handle_message(phone, text)
        if reply.status == "error":
            errors = self._sql("SELECT kind, step, error_code, left(output::text, 400) FROM workflow_logs "
                               "WHERE NOT success ORDER BY id DESC LIMIT 5")
            pytest.fail(f"bot replied with an error to {text!r}: {reply.messages}\nfailed logs: {errors}")
        return reply

    def __getattr__(self, name):
        return getattr(self._agent, name)


@pytest.fixture
def make_agent(fresh_db, checkpointer, sql):
    """make_agent(script, accepts=...) -> (agent, fake). Fresh DB + conversation state per test."""
    from app.agent.service import OrderAgent

    def make(script: dict[str, list[AIMessage]], accepts: Optional[Callable[[str], bool]] = None,
             allow_errors: bool = False):
        fake = FakeLLM(script, accepts or (lambda message: True))
        agent = OrderAgent(fake, checkpointer)
        return (agent if allow_errors else _CheckedAgent(agent, sql)), fake

    return make


def one(reply) -> str:
    """The single message sent for a turn."""
    assert len(reply.messages) == 1, reply.messages
    return reply.messages[0]
