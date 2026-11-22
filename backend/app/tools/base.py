"""Shared tool plumbing: error result, @tool decorator, log context, workflow logging.

Every tool:
  * takes plain arguments, validated by a Pydantic input model
  * returns a Pydantic result with success=True, or ToolError(success=False, error_code, message)
  * never raises (unexpected exceptions become INTERNAL_ERROR)
  * is logged to workflow_logs (kind='tool') with input, output and duration
"""
from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Callable, Iterator, Literal, Optional, Type, TypeVar

from pydantic import AfterValidator, BaseModel, Field, ValidationError, field_validator

from app.db.models import WorkflowLog
from app.db.session import session_scope

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- results

class ErrorCode:
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_PHONE = "INVALID_PHONE"
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
    UNKNOWN_PRODUCT = "UNKNOWN_PRODUCT"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_NOT_PENDING = "DRAFT_NOT_PENDING"
    DRAFT_EXPIRED = "DRAFT_EXPIRED"
    INVALID_IDEMPOTENCY_KEY = "INVALID_IDEMPOTENCY_KEY"
    PRICE_CHANGED = "PRICE_CHANGED"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolOk(BaseModel):
    success: Literal[True] = True


class ToolError(BaseModel):
    success: Literal[False] = False
    error_code: str
    message: str
    details: Optional[dict[str, Any]] = None


def fail(code: str, message: str, **details: Any) -> ToolError:
    return ToolError(error_code=code, message=message, details=details or None)


class Abort(Exception):
    """Raise inside session_scope() to roll back and return a ToolError."""

    def __init__(self, error: ToolError):
        super().__init__(error.message)
        self.error = error


# --------------------------------------------------------------------------- log context

@dataclass(frozen=True)
class LogContext:
    thread_id: Optional[str] = None
    run_id: Optional[str] = None
    buffer: Optional[list] = None  # when set, logs are collected and written in one transaction at the end


_log_ctx: ContextVar[LogContext] = ContextVar("bizflow_log_ctx", default=LogContext())


@contextmanager
def log_context(thread_id: Optional[str] = None, run_id: Optional[str] = None,
                buffered: bool = False) -> Iterator[LogContext]:
    """Tag every log written inside this block with thread_id / run_id.

    buffered=True (used per inbound message): collect the rows and insert them all in ONE transaction
    when the block ends - one round trip to Neon instead of one per log. Nested contexts share the buffer.
    """
    parent = _log_ctx.get()
    buffer = [] if buffered else parent.buffer
    token = _log_ctx.set(LogContext(thread_id, run_id, buffer))
    try:
        yield _log_ctx.get()
    finally:
        _log_ctx.reset(token)
        if buffered:
            _write_logs(buffer)


def current_log_context() -> LogContext:
    return _log_ctx.get()


def _write_logs(rows: list[WorkflowLog]) -> None:
    if not rows:
        return
    try:
        with session_scope() as s:
            s.add_all(rows)
    except Exception:  # logging must never break the workflow
        logger.exception("failed to write %d workflow logs", len(rows))


def log_event(
    kind: str,
    step: str,
    *,
    input: Optional[dict[str, Any]] = None,
    output: Optional[dict[str, Any]] = None,
    success: bool = True,
    error_code: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Record one workflow_logs row (now, or at the end of a buffered log_context). Never raises."""
    ctx = _log_ctx.get()
    row = WorkflowLog(thread_id=ctx.thread_id, run_id=ctx.run_id, kind=kind, step=step, input=input,
                      output=output, success=success, error_code=error_code, duration_ms=duration_ms)
    if ctx.buffer is not None:
        ctx.buffer.append(row)
    else:
        _write_logs([row])


# --------------------------------------------------------------------------- decorator

R = TypeVar("R", bound=BaseModel)


def tool(input_model: Type[BaseModel]) -> Callable[[Callable[[Any], R]], Callable[..., R | ToolError]]:
    """Wrap fn(params: input_model) as a public tool fn(**kwargs) -> result | ToolError."""

    def decorator(fn: Callable[[Any], R]) -> Callable[..., R | ToolError]:
        field_names = list(input_model.model_fields)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> R | ToolError:
            started = time.perf_counter()
            raw = {**dict(zip(field_names, args)), **kwargs}
            try:
                params = input_model.model_validate(raw)
            except ValidationError as exc:
                result: BaseModel = fail(
                    ErrorCode.INVALID_INPUT, "Invalid input: " + "; ".join(
                        f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
                    ),
                )
                log_input: dict[str, Any] = {k: repr(v) for k, v in raw.items()}
            else:
                log_input = params.model_dump(mode="json")
                try:
                    result = fn(params)
                except Exception as exc:
                    logger.exception("tool %s crashed", fn.__name__)
                    result = fail(ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            log_event(
                "tool", fn.__name__,
                input=log_input,
                output=result.model_dump(mode="json"),
                success=bool(getattr(result, "success", False)),
                error_code=getattr(result, "error_code", None),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return result  # type: ignore[return-value]

        wrapper.input_model = input_model  # type: ignore[attr-defined]
        return wrapper

    return decorator


# --------------------------------------------------------------------------- shared models


class OrderLineIn(BaseModel):
    product_id: str = Field(min_length=1, max_length=10)
    qty: int = Field(gt=0, le=10_000)

    @field_validator("product_id")
    @classmethod
    def _normalise_id(cls, v: str) -> str:
        return v.strip().upper()


def _merge_duplicates(lines: list[OrderLineIn]) -> list[OrderLineIn]:
    """Same product twice -> one line with summed qty (order preserved)."""
    merged: dict[str, int] = {}
    for line in lines:
        merged[line.product_id] = merged.get(line.product_id, 0) + line.qty
    return [OrderLineIn(product_id=pid, qty=qty) for pid, qty in merged.items()]


OrderLines = Annotated[list[OrderLineIn], Field(min_length=1, max_length=50), AfterValidator(_merge_duplicates)]


class PricedLine(BaseModel):
    product_id: str
    name: str
    pack: str
    unit: str
    qty: int
    unit_price: Decimal
    line_total: Decimal


class Pricing(BaseModel):
    lines: list[PricedLine]
    subtotal: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    total: Decimal
