"""notify_staff tool + the conversation transcript (messages table)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal, Optional, Union

from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Message, StaffNotification, WorkflowLog
from app.db.session import get_engine, session_scope
from app.tools.base import ToolError, ToolOk, current_log_context, tool

LOCK_TIMEOUT = "30s"


class ConversationBusy(Exception):
    pass


@contextmanager
def conversation_lock(thread_id: str) -> Iterator[None]:
    with get_engine().connect() as conn:
        try:
            conn.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
            conn.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": thread_id})
        except Exception as exc:
            raise ConversationBusy(thread_id) from exc
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": thread_id})
            conn.execute(text("RESET lock_timeout"))
            conn.commit()


class NotifyStaffInput(BaseModel):
    role: Literal["sales", "warehouse", "manager", "accounts"]
    reason: str = Field(min_length=1, max_length=1000)


class NotifyStaffOutput(ToolOk):
    notification_id: int
    role: str
    thread_id: Optional[str]


@tool(NotifyStaffInput)
def notify_staff(p: NotifyStaffInput) -> Union[NotifyStaffOutput, ToolError]:
    """Create a staff notification (unknown number, cancel request, errors). Tagged with the current thread."""
    thread_id = current_log_context().thread_id
    with session_scope() as s:
        n = StaffNotification(role=p.role, reason=p.reason, thread_id=thread_id, status="open")
        s.add(n)
        s.flush()
        return NotifyStaffOutput(notification_id=n.id, role=n.role, thread_id=thread_id)


def record_message(thread_id: str, direction: Literal["in", "out"], text: str,
                   customer_id: Optional[int] = None, session: Optional[Session] = None) -> int:
    if session is not None:
        m = Message(thread_id=thread_id, direction=direction, text=text, customer_id=customer_id)
        session.add(m)
        session.flush()
        return m.id
    with session_scope() as s:
        return record_message(thread_id, direction, text, customer_id, session=s)


def recent_messages(thread_id: str, limit: int = 10, after_id: int = 0) -> list[dict]:
    """Last `limit` messages of a conversation with id > after_id, oldest first: [{id, direction, text}]."""
    with session_scope() as s:
        rows = list(s.scalars(select(Message).where(Message.thread_id == thread_id, Message.id > after_id)
                              .order_by(Message.id.desc()).limit(limit)))
    return [{"id": m.id, "direction": m.direction, "text": m.text} for m in reversed(rows)]


def workflow_trace(thread_id: str, run_id: Optional[str] = None, limit: int = 300) -> dict:
    with session_scope() as s:
        if run_id is None:
            run_id = s.scalar(select(WorkflowLog.run_id).where(WorkflowLog.thread_id == thread_id,
                                                               WorkflowLog.run_id.is_not(None))
                              .order_by(WorkflowLog.id.desc()).limit(1))
        rows = list(s.scalars(select(WorkflowLog).where(WorkflowLog.thread_id == thread_id,
                                                        WorkflowLog.run_id == run_id)
                              .order_by(WorkflowLog.id).limit(limit))) if run_id else []
        steps = [{"id": r.id, "kind": r.kind, "step": r.step, "input": r.input, "output": r.output,
                  "success": r.success, "error_code": r.error_code, "duration_ms": r.duration_ms,
                  "created_at": r.created_at} for r in rows]
    return {"thread_id": thread_id, "run_id": run_id, "steps": steps}


def list_notifications(status: Optional[str] = "open", limit: int = 50) -> list[dict]:
    with session_scope() as s:
        q = select(StaffNotification).order_by(StaffNotification.id.desc()).limit(limit)
        if status:
            q = q.where(StaffNotification.status == status)
        return [{"id": n.id, "role": n.role, "reason": n.reason, "thread_id": n.thread_id, "status": n.status,
                 "created_at": n.created_at} for n in s.scalars(q)]


def resolve_notification(notification_id: int) -> Optional[dict]:
    with session_scope() as s:
        n = s.get(StaffNotification, notification_id, with_for_update=True)
        if n is None:
            return None
        n.status = "resolved"
        return {"id": n.id, "role": n.role, "reason": n.reason, "status": n.status}
