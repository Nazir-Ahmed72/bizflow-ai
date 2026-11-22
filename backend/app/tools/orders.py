"""Order tools.

Writes (only called by the agent's graph nodes, never directly by the LLM):
    create_draft, cancel_draft, confirm_order (the only place an order is created / stock reserved),
    generate_invoice
Read-only (the LLM may call it): get_order_history
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.db.models import Invoice, Order, OrderDraft, OrderItem
from app.db.session import session_scope
from app.tools.base import (
    Abort, ErrorCode, OrderLineIn, OrderLines, PricedLine, Pricing, ToolError, ToolOk, fail, tool,
)
from app.tools.catalog import compute_pricing

DRAFT_TTL = timedelta(hours=2)
Language = Literal["en", "ur", "roman_ur"]


def idempotency_key_for(draft_id: uuid.UUID | str) -> str:
    return f"{draft_id}:confirm"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- create_draft

class CreateDraftInput(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    customer_id: int = Field(gt=0)
    items: OrderLines
    language: Language = "en"


class CreateDraftOutput(ToolOk, Pricing):
    draft_id: uuid.UUID
    idempotency_key: str
    expires_at: datetime
    superseded_draft_ids: list[uuid.UUID]


@tool(CreateDraftInput)
def create_draft(p: CreateDraftInput) -> Union[CreateDraftOutput, ToolError]:
    """Price the items (deterministically) and store a pending draft that expires in 2 hours.

    Any older pending draft of the same customer is cancelled (superseded), so a "yes"
    can only ever confirm the latest summary.
    """
    with session_scope() as s:
        pricing = compute_pricing(s, p.customer_id, p.items)
        if isinstance(pricing, ToolError):
            return pricing
        superseded = list(s.scalars(
            select(OrderDraft).where(OrderDraft.customer_id == p.customer_id, OrderDraft.status == "pending")
            .with_for_update()
        ))
        for d in superseded:
            d.status = "cancelled"
        now = _now()
        draft = OrderDraft(
            id=uuid.uuid4(), thread_id=p.thread_id, customer_id=p.customer_id,
            items=[line.model_dump(mode="json") for line in pricing.lines],
            subtotal=pricing.subtotal, discount_pct=pricing.discount_pct,
            discount_amount=pricing.discount_amount, total=pricing.total,
            language=p.language, status="pending", summary_sent_at=now, expires_at=now + DRAFT_TTL,
        )
        s.add(draft)
    return CreateDraftOutput(
        draft_id=draft.id, idempotency_key=idempotency_key_for(draft.id), expires_at=draft.expires_at,
        superseded_draft_ids=[d.id for d in superseded], **pricing.model_dump(),
    )


# --------------------------------------------------------------------------- cancel_draft

class CancelDraftInput(BaseModel):
    draft_id: uuid.UUID


class CancelDraftOutput(ToolOk):
    draft_id: uuid.UUID
    status: str


@tool(CancelDraftInput)
def cancel_draft(p: CancelDraftInput) -> Union[CancelDraftOutput, ToolError]:
    """Customer said no / cancel. Only pending drafts can be cancelled."""
    with session_scope() as s:
        draft = s.get(OrderDraft, p.draft_id, with_for_update=True)
        if draft is None:
            return fail(ErrorCode.DRAFT_NOT_FOUND, f"Draft {p.draft_id} not found.")
        if draft.status != "pending":
            return fail(ErrorCode.DRAFT_NOT_PENDING, f"Draft is already {draft.status}.", status=draft.status)
        draft.status = "cancelled"
        return CancelDraftOutput(draft_id=draft.id, status=draft.status)


# --------------------------------------------------------------------------- confirm_order

class ConfirmOrderInput(BaseModel):
    draft_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=100)


class ConfirmOrderOutput(ToolOk):
    already_confirmed: bool  # True -> duplicate "yes": nothing created, send no message
    order_id: int
    order_number: str
    draft_id: uuid.UUID
    lines: list[PricedLine]
    subtotal: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    total: Decimal


def _order_output(order: Order, already: bool) -> ConfirmOrderOutput:
    return ConfirmOrderOutput(
        already_confirmed=already, order_id=order.id, order_number=order.order_number, draft_id=order.draft_id,
        lines=[
            PricedLine(product_id=i.product_id, name=i.product_name, pack="", unit="", qty=i.qty,
                       unit_price=i.unit_price, line_total=i.line_total)
            for i in sorted(order.items, key=lambda i: i.id)
        ],
        subtotal=order.subtotal, discount_pct=order.discount_pct,
        discount_amount=order.discount_amount, total=order.total,
    )


def _find_order(s, key: str) -> Optional[Order]:
    return s.scalar(select(Order).where(Order.idempotency_key == key))


@tool(ConfirmOrderInput)
def confirm_order(p: ConfirmOrderInput) -> Union[ConfirmOrderOutput, ToolError]:
    """Turn a pending draft into an order, atomically.

    One transaction: lock draft -> idempotency check -> expiry check -> re-price and compare ->
    reserve each line with a guarded UPDATE (rowcount must be 1) -> insert order + items ->
    mark draft confirmed. Any failure rolls back everything (no partial reservation).
    """
    if p.idempotency_key != idempotency_key_for(p.draft_id):
        return fail(ErrorCode.INVALID_IDEMPOTENCY_KEY, "Idempotency key must be '<draft_id>:confirm'.")

    try:
        with session_scope() as s:
            existing = _find_order(s, p.idempotency_key)
            if existing:
                return _order_output(existing, already=True)

            draft = s.get(OrderDraft, p.draft_id, with_for_update=True)
            if draft is None:
                raise Abort(fail(ErrorCode.DRAFT_NOT_FOUND, f"Draft {p.draft_id} not found."))

            # Another request may have confirmed it while we waited for the lock.
            existing = _find_order(s, p.idempotency_key)
            if existing:
                return _order_output(existing, already=True)

            if draft.status != "pending":
                raise Abort(fail(ErrorCode.DRAFT_NOT_PENDING, f"Draft is {draft.status}, not pending.",
                                 status=draft.status))
            if draft.expires_at is not None and draft.expires_at <= _now():
                draft.status = "expired"  # normal return -> session_scope commits this
                return fail(ErrorCode.DRAFT_EXPIRED, "This order summary has expired (older than 2 hours).")

            # Re-check prices against the summary the customer saw.
            lines = [OrderLineIn(product_id=i["product_id"], qty=i["qty"]) for i in draft.items]
            pricing = compute_pricing(s, draft.customer_id, lines)
            if isinstance(pricing, ToolError):
                raise Abort(pricing)
            seen = {i["product_id"]: Decimal(i["unit_price"]) for i in draft.items}
            if (any(seen[pl.product_id] != pl.unit_price for pl in pricing.lines)
                    or pricing.discount_pct != draft.discount_pct or pricing.total != draft.total):
                raise Abort(fail(
                    ErrorCode.PRICE_CHANGED, "Prices changed since the summary; a new summary is needed.",
                    old_total=str(draft.total), new_total=str(pricing.total),
                ))

            # Atomic reservation, fixed order to avoid deadlocks.
            for pl in sorted(pricing.lines, key=lambda x: x.product_id):
                res = s.execute(
                    text("UPDATE inventory SET reserved = reserved + :qty, updated_at = now() "
                         "WHERE product_id = :pid AND physical - reserved >= :qty"),
                    {"qty": pl.qty, "pid": pl.product_id},
                )
                if res.rowcount != 1:
                    available = s.execute(
                        text("SELECT physical - reserved FROM inventory WHERE product_id = :pid"),
                        {"pid": pl.product_id},
                    ).scalar()
                    raise Abort(fail(
                        ErrorCode.INSUFFICIENT_STOCK,
                        f"Not enough stock for {pl.name}: requested {pl.qty}, available {available}.",
                        product_id=pl.product_id, requested=pl.qty, available=available,
                    ))

            order_id = s.execute(text("SELECT nextval(pg_get_serial_sequence('orders', 'id'))")).scalar_one()
            order = Order(
                id=order_id, order_number=f"ORD-{order_id:06d}", draft_id=draft.id,
                customer_id=draft.customer_id, idempotency_key=p.idempotency_key,
                subtotal=pricing.subtotal, discount_pct=pricing.discount_pct,
                discount_amount=pricing.discount_amount, total=pricing.total, status="confirmed",
                items=[
                    OrderItem(product_id=pl.product_id, product_name=pl.name, qty=pl.qty,
                              unit_price=pl.unit_price, line_total=pl.line_total)
                    for pl in pricing.lines
                ],
            )
            s.add(order)
            draft.status = "confirmed"
            s.flush()
            return _order_output(order, already=False)
    except Abort as a:
        return a.error
    except IntegrityError:
        # Lost a race on the unique idempotency key: the other request created the order.
        with session_scope() as s:
            existing = _find_order(s, p.idempotency_key)
            if existing:
                return _order_output(existing, already=True)
        raise


# --------------------------------------------------------------------------- generate_invoice


class GenerateInvoiceInput(BaseModel):
    order_id: int = Field(gt=0)


class InvoiceLine(BaseModel):
    product_id: str
    name: str
    qty: int
    unit_price: Decimal
    line_total: Decimal


class GenerateInvoiceOutput(ToolOk):
    already_existed: bool
    invoice_number: str
    order_id: int
    order_number: str
    customer_name: str
    customer_address: str
    lines: list[InvoiceLine]
    subtotal: Decimal
    discount_amount: Decimal
    total: Decimal
    issued_at: datetime


def _output(order: Order, inv: Invoice, already: bool) -> GenerateInvoiceOutput:
    return GenerateInvoiceOutput(
        already_existed=already, invoice_number=inv.invoice_number, order_id=order.id,
        order_number=order.order_number, customer_name=order.customer.name,
        customer_address=order.customer.address,
        lines=[InvoiceLine(product_id=i.product_id, name=i.product_name, qty=i.qty,
                           unit_price=i.unit_price, line_total=i.line_total)
               for i in sorted(order.items, key=lambda i: i.id)],
        subtotal=inv.subtotal, discount_amount=inv.discount_amount, total=inv.total, issued_at=inv.issued_at,
    )


@tool(GenerateInvoiceInput)
def generate_invoice(p: GenerateInvoiceInput) -> Union[GenerateInvoiceOutput, ToolError]:
    """Issue the invoice for a confirmed order; returns the existing one if already issued."""
    try:
        with session_scope() as s:
            order = s.get(Order, p.order_id)
            if order is None:
                return fail(ErrorCode.ORDER_NOT_FOUND, f"Order {p.order_id} not found.")
            if order.invoice is not None:
                return _output(order, order.invoice, already=True)
            inv = Invoice(
                invoice_number=f"INV-{order.id:06d}", order_id=order.id, subtotal=order.subtotal,
                discount_amount=order.discount_amount, total=order.total,
            )
            s.add(inv)
            s.flush()
            s.refresh(inv)  # load server-side issued_at
            return _output(order, inv, already=False)
    except IntegrityError:  # concurrent call created it first
        with session_scope() as s:
            order = s.get(Order, p.order_id)
            inv = s.scalar(select(Invoice).where(Invoice.order_id == p.order_id))
            return _output(order, inv, already=True)


# --------------------------------------------------------------------------- get_order_history (read-only)

class GetOrderHistoryInput(BaseModel):
    customer_id: int = Field(gt=0)
    limit: int = Field(default=5, ge=1, le=20)
    order_number: Optional[str] = Field(default=None, max_length=20)


class HistoryLine(BaseModel):
    product_id: str
    name: str
    qty: int
    line_total: Decimal


class HistoryOrder(BaseModel):
    order_number: str
    status: str
    created_at: datetime
    subtotal: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    total: Decimal
    invoice_number: Optional[str]
    lines: list[HistoryLine]


class GetOrderHistoryOutput(ToolOk):
    customer_id: int
    count: int  # so the LLM never has to count
    orders: list[HistoryOrder]  # newest first


@tool(GetOrderHistoryInput)
def get_order_history(p: GetOrderHistoryInput) -> Union[GetOrderHistoryOutput, ToolError]:
    """A customer's confirmed orders, newest first (optionally one order by its number)."""
    with session_scope() as s:
        q = (select(Order).where(Order.customer_id == p.customer_id)
             .options(selectinload(Order.items), selectinload(Order.invoice))
             .order_by(Order.id.desc()).limit(p.limit))
        if p.order_number:
            q = q.where(Order.order_number == p.order_number.strip().upper())
        orders = list(s.scalars(q))
        history = [
            HistoryOrder(order_number=o.order_number, status=o.status, created_at=o.created_at,
                         subtotal=o.subtotal, discount_pct=o.discount_pct, discount_amount=o.discount_amount,
                         total=o.total,
                         invoice_number=o.invoice.invoice_number if o.invoice else None,
                         lines=[HistoryLine(product_id=i.product_id, name=i.product_name, qty=i.qty,
                                            line_total=i.line_total) for i in sorted(o.items, key=lambda i: i.id)])
            for o in orders]
        return GetOrderHistoryOutput(customer_id=p.customer_id, count=len(history), orders=history)


class GetCustomerProfileInput(BaseModel):
    customer_id: int = Field(gt=0)


class UsualItem(BaseModel):
    product_id: str
    name: str
    times_ordered: int
    last_qty: int


class GetCustomerProfileOutput(ToolOk):
    customer_id: int
    orders_count: int
    last_order_number: Optional[str]
    last_order_at: Optional[datetime]
    usual_items: list[UsualItem]
    preferred_language: Optional[str]


@tool(GetCustomerProfileInput)
def get_customer_profile(p: GetCustomerProfileInput) -> Union[GetCustomerProfileOutput, ToolError]:
    """Long-term memory: what this customer usually orders, built from their confirmed orders."""
    with session_scope() as s:
        orders = list(s.scalars(select(Order).where(Order.customer_id == p.customer_id)
                                .options(selectinload(Order.items)).order_by(Order.id.desc()).limit(50)))
        language = s.scalar(select(OrderDraft.language).where(OrderDraft.customer_id == p.customer_id)
                            .order_by(OrderDraft.created_at.desc()).limit(1))
    stats: dict[str, dict] = {}
    for order in orders:
        for item in order.items:
            entry = stats.setdefault(item.product_id, {"name": item.product_name, "times": 0, "last_qty": item.qty})
            entry["times"] += 1
    usual = sorted(stats.items(), key=lambda kv: (-kv[1]["times"], kv[0]))[:3]
    return GetCustomerProfileOutput(
        customer_id=p.customer_id, orders_count=len(orders),
        last_order_number=orders[0].order_number if orders else None,
        last_order_at=orders[0].created_at if orders else None,
        usual_items=[UsualItem(product_id=pid, name=e["name"], times_ordered=e["times"], last_qty=e["last_qty"])
                     for pid, e in usual],
        preferred_language=language,
    )
