"""OrderAgent: one call per inbound message -> the replies to send (possibly none),
plus the Postgres checkpointer that remembers each conversation (thread_id = phone)."""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.agent.graph import build_graph
from app.agent.llm import LoggedLLM, OrderLLM
from app.agent.text import backup
from app.config import psycopg_url
from app.phone import normalize_phone
from app.tools import conversation_lock, log_context, log_event, notify_staff, recent_messages, record_message

logger = logging.getLogger(__name__)


@contextmanager
def postgres_checkpointer(max_size: int = 4) -> Iterator[PostgresSaver]:
    """Conversation memory in Neon (its own checkpoint_* tables)."""
    with ConnectionPool(conninfo=psycopg_url(), max_size=max_size, open=True,
                        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                        check=ConnectionPool.check_connection) as pool:  # Neon drops idle connections
        saver = PostgresSaver(pool)
        saver.setup()
        yield saver


@dataclass
class AgentReply:
    thread_id: str
    messages: list[str] = field(default_factory=list)  # empty -> send nothing (e.g. duplicate "yes")
    waiting_for: Optional[str] = None                   # product_choice | stock_choice | confirmation | None
    status: Optional[str] = None
    order_number: Optional[str] = None
    invoice_number: Optional[str] = None


def thread_id_for(phone: str) -> str:
    try:
        return normalize_phone(phone)
    except ValueError:
        return phone.strip()[:64] or "unknown"


class OrderAgent:
    def __init__(self, llm: OrderLLM, checkpointer: Any):
        self.checkpointer = checkpointer
        self.graph = build_graph(LoggedLLM(llm), checkpointer)

    def handle_message(self, phone: str, text: str) -> AgentReply:
        thread_id = thread_id_for(phone)
        with conversation_lock(thread_id):
            return self._handle(thread_id, text)

    def conversation(self, phone: str, limit: int = 50) -> dict:
        thread_id = thread_id_for(phone)
        snapshot = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        waiting_for = snapshot.interrupts[0].value.get("awaiting") if snapshot.interrupts else None
        return {"thread_id": thread_id, "waiting_for": waiting_for,
                "messages": recent_messages(thread_id, limit=limit)}

    def _handle(self, thread_id: str, text: str) -> AgentReply:
        run_id = uuid.uuid4().hex[:12]
        config = {"configurable": {"thread_id": thread_id, "run_id": run_id}}

        with log_context(thread_id, run_id, buffered=True):
            snapshot = self.graph.get_state(config)
            values = snapshot.values or {}
            customer_id = (values.get("customer") or {}).get("customer_id")
            record_message(thread_id, "in", text, customer_id)

            resuming = bool(snapshot.interrupts)  # paused, waiting for this reply
            before = len(values.get("outbox") or []) if resuming else 0
            try:
                if resuming:
                    self.graph.invoke(Command(resume=text), config)
                else:
                    self.graph.invoke({"thread_id": thread_id, "message": text}, config)
            except Exception as exc:
                logger.exception("graph run failed for %s", thread_id)
                log_event("node", "graph_run", output={"error": f"{type(exc).__name__}: {exc}"},
                          success=False, error_code="GRAPH_ERROR")
                notify_staff("manager", f"Order bot crashed on {thread_id}: {type(exc).__name__}: {exc}")
                msg = backup("error", {})
                record_message(thread_id, "out", msg, customer_id)
                return AgentReply(thread_id=thread_id, messages=[msg], status="error")

            after = self.graph.get_state(config)
            state = after.values or {}
            messages = list((state.get("outbox") or [])[before:])
            waiting_for = None
            if after.interrupts:
                payload = after.interrupts[0].value
                if payload.get("message"):
                    messages.append(payload["message"])
                waiting_for = payload.get("awaiting")

            customer_id = (state.get("customer") or {}).get("customer_id")
            for m in messages:
                record_message(thread_id, "out", m, customer_id)
            return AgentReply(thread_id=thread_id, messages=messages, waiting_for=waiting_for,
                              status=state.get("status"),
                              order_number=(state.get("order") or {}).get("order_number"),
                              invoice_number=(state.get("invoice") or {}).get("invoice_number"))

    def reset_thread(self, phone: str) -> None:
        """Forget the conversation state for this phone (orders in the DB are untouched)."""
        self.checkpointer.delete_thread(thread_id_for(phone))
