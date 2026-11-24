from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Union

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    OPEN_PO_STATUSES, Backorder, Inventory, Product, PurchaseOrder, PurchaseOrderItem, Supplier, SupplierProduct,
)
from app.db.session import session_scope
from app.tools.base import ErrorCode, ToolError, ToolOk, fail, tool
from app.tools.catalog import money
from app.tools.notify import notify_staff, record_message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def supplier_thread(supplier_id: int) -> str:
    return f"supplier:{supplier_id}"


class POLine(BaseModel):
    product_id: str
    name: str
    qty_ordered: int
    qty_received: int
    unit_cost: Decimal
    line_total: Decimal


class POSummary(BaseModel):
    po_number: str
    status: str
    supplier_id: int
    supplier_name: str
    reason: str
    total_cost: Decimal
    created_at: datetime
    approved_by: Optional[str]
    rejected_reason: Optional[str]
    sent_at: Optional[datetime]
    expected_at: Optional[datetime]
    received_at: Optional[datetime]
    lines: list[POLine]


def _summary(s: Session, po: PurchaseOrder) -> POSummary:
    names = dict(s.execute(select(Product.id, Product.name)
                           .where(Product.id.in_([i.product_id for i in po.items]))).all())
    return POSummary(
        po_number=po.po_number, status=po.status, supplier_id=po.supplier_id, supplier_name=po.supplier.name,
        reason=po.reason, total_cost=po.total_cost, created_at=po.created_at, approved_by=po.approved_by,
        rejected_reason=po.rejected_reason, sent_at=po.sent_at, expected_at=po.expected_at,
        received_at=po.received_at,
        lines=[POLine(product_id=i.product_id, name=names.get(i.product_id, i.product_id), qty_ordered=i.qty_ordered,
                      qty_received=i.qty_received, unit_cost=i.unit_cost, line_total=i.line_total)
               for i in sorted(po.items, key=lambda i: i.product_id)],
    )


def _load_po(s: Session, po_number: str, lock: bool = False) -> Optional[PurchaseOrder]:
    q = (select(PurchaseOrder).where(PurchaseOrder.po_number == po_number.strip().upper())
         .options(selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier)))
    if lock:
        q = q.with_for_update()
    return s.scalar(q)


def _recalculate_total(po: PurchaseOrder) -> None:
    po.total_cost = money(sum((i.line_total for i in po.items), Decimal("0")))


def _choose_supplier(s: Session, product_id: str, exclude: set[int]) -> Optional[SupplierProduct]:
    return s.scalar(
        select(SupplierProduct).join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(SupplierProduct.product_id == product_id, SupplierProduct.is_active.is_(True),
               Supplier.is_active.is_(True), SupplierProduct.supplier_id.not_in(sorted(exclude) or [0]))
        .order_by(SupplierProduct.priority).limit(1)
    )


def _inventory_positions(s: Session, product_ids: Optional[list[str]]) -> list:
    on_order = (
        select(PurchaseOrderItem.product_id,
               func.sum(PurchaseOrderItem.qty_ordered - PurchaseOrderItem.qty_received).label("on_order"))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .where(PurchaseOrder.status.in_(OPEN_PO_STATUSES))
        .group_by(PurchaseOrderItem.product_id).subquery()
    )
    q = (select(Product.id, Product.name, Inventory.physical, Inventory.reserved, Inventory.reorder_level,
                Inventory.max_level, func.coalesce(on_order.c.on_order, 0).label("on_order"))
         .join(Inventory, Inventory.product_id == Product.id)
         .outerjoin(on_order, on_order.c.product_id == Product.id)
         .where(Product.is_active.is_(True)).order_by(Product.id))
    if product_ids:
        q = q.where(Product.id.in_(product_ids))
    return s.execute(q).all()


class ReorderCheckInput(BaseModel):
    reason: str = Field(min_length=1, max_length=300)
    product_ids: Optional[list[str]] = None
    exclude_supplier_ids: list[int] = Field(default_factory=list)


class ReorderDecision(BaseModel):
    product_id: str
    name: str
    physical: int
    reserved: int
    on_order: int
    position: int
    reorder_level: int
    max_level: int
    action: str
    qty: int = 0
    supplier_name: Optional[str] = None
    po_number: Optional[str] = None


class ReorderCheckOutput(ToolOk):
    decisions: list[ReorderDecision]
    purchase_orders: list[POSummary]


@tool(ReorderCheckInput)
def run_reorder_check(p: ReorderCheckInput) -> Union[ReorderCheckOutput, ToolError]:
    exclude = set(p.exclude_supplier_ids)
    decisions: list[ReorderDecision] = []
    touched: dict[int, PurchaseOrder] = {}
    created: set[int] = set()
    with session_scope() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext('reorder-check'))"))
        for row in _inventory_positions(s, p.product_ids):
            position = row.physical - row.reserved + int(row.on_order)
            base = dict(product_id=row.id, name=row.name, physical=row.physical, reserved=row.reserved,
                        on_order=int(row.on_order), position=position, reorder_level=row.reorder_level,
                        max_level=row.max_level)
            if position > row.reorder_level:
                decisions.append(ReorderDecision(**base, action="enough_stock"))
                continue
            offer = _choose_supplier(s, row.id, exclude)
            if offer is None:
                decisions.append(ReorderDecision(**base, action="no_supplier"))
                continue
            qty = max(row.max_level - position, offer.min_order_qty)
            po = touched.get(offer.supplier_id) or s.scalar(
                select(PurchaseOrder).where(PurchaseOrder.supplier_id == offer.supplier_id,
                                            PurchaseOrder.status == "draft")
                .options(selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier))
                .order_by(PurchaseOrder.id).limit(1).with_for_update())
            if po is None:
                po_id = s.execute(text("SELECT nextval(pg_get_serial_sequence('purchase_orders', 'id'))")).scalar_one()
                po = PurchaseOrder(id=po_id, po_number=f"PO-{po_id:06d}", supplier_id=offer.supplier_id,
                                   status="draft", reason=p.reason, total_cost=Decimal("0"), items=[])
                s.add(po)
                created.add(po_id)
            po.items.append(PurchaseOrderItem(product_id=row.id, qty_ordered=qty, unit_cost=offer.unit_cost,
                                              line_total=money(offer.unit_cost * qty)))
            _recalculate_total(po)
            touched[offer.supplier_id] = po
            s.flush()
            decisions.append(ReorderDecision(**base, action="drafted", qty=qty, supplier_name=po.supplier.name,
                                             po_number=po.po_number))
        summaries = [_summary(s, po) for po in touched.values()]
    for summary in summaries:
        lines = ", ".join(f"{l.name} x {l.qty_ordered}" for l in summary.lines)
        notify_staff("manager", f"Purchase order {summary.po_number} for {summary.supplier_name} needs approval: "
                                f"{lines} (total Rs {summary.total_cost:,.0f}). Reason: {p.reason}")
    return ReorderCheckOutput(decisions=decisions, purchase_orders=summaries)


class PONumberInput(BaseModel):
    po_number: str = Field(min_length=1, max_length=20)


class POOutput(ToolOk):
    purchase_order: POSummary


class ListPOsInput(BaseModel):
    status: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)


class ListPOsOutput(ToolOk):
    count: int
    purchase_orders: list[POSummary]


@tool(ListPOsInput)
def list_purchase_orders(p: ListPOsInput) -> Union[ListPOsOutput, ToolError]:
    with session_scope() as s:
        q = (select(PurchaseOrder).options(selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier))
             .order_by(PurchaseOrder.id.desc()).limit(p.limit))
        if p.status:
            q = q.where(PurchaseOrder.status == p.status)
        rows = [_summary(s, po) for po in s.scalars(q)]
    return ListPOsOutput(count=len(rows), purchase_orders=rows)


@tool(PONumberInput)
def get_purchase_order(p: PONumberInput) -> Union[POOutput, ToolError]:
    with session_scope() as s:
        po = _load_po(s, p.po_number)
        if po is None:
            return fail(ErrorCode.PO_NOT_FOUND, f"Purchase order {p.po_number} not found.")
        return POOutput(purchase_order=_summary(s, po))


def _transition(s: Session, po_number: str, allowed: tuple[str, ...], new_status: str, **values) -> Union[PurchaseOrder, ToolError]:
    po = _load_po(s, po_number, lock=True)
    if po is None:
        return fail(ErrorCode.PO_NOT_FOUND, f"Purchase order {po_number} not found.")
    if po.status not in allowed:
        return fail(ErrorCode.INVALID_TRANSITION,
                    f"{po.po_number} is '{po.status}'; it must be {' or '.join(allowed)} to become '{new_status}'.",
                    status=po.status)
    po.status = new_status
    for key, value in values.items():
        setattr(po, key, value)
    return po


class ApproveInput(BaseModel):
    po_number: str = Field(min_length=1, max_length=20)
    approved_by: str = Field(min_length=1, max_length=80)


@tool(ApproveInput)
def approve_purchase_order(p: ApproveInput) -> Union[POOutput, ToolError]:
    with session_scope() as s:
        po = _transition(s, p.po_number, ("draft",), "approved", approved_by=p.approved_by, approved_at=_now())
        if isinstance(po, ToolError):
            return po
        return POOutput(purchase_order=_summary(s, po))


class RejectInput(BaseModel):
    po_number: str = Field(min_length=1, max_length=20)
    rejected_by: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)
    try_next_supplier: bool = False


class RejectOutput(ToolOk):
    purchase_order: POSummary
    replacement_orders: list[POSummary]


@tool(RejectInput)
def reject_purchase_order(p: RejectInput) -> Union[RejectOutput, ToolError]:
    with session_scope() as s:
        po = _transition(s, p.po_number, ("draft", "approved"), "rejected", rejected_by=p.rejected_by,
                         rejected_reason=p.reason)
        if isinstance(po, ToolError):
            return po
        rejected = _summary(s, po)
    replacements: list[POSummary] = []
    if p.try_next_supplier:
        retry = run_reorder_check(reason=f"replaces {rejected.po_number} ({p.reason})",
                                  product_ids=[l.product_id for l in rejected.lines],
                                  exclude_supplier_ids=[rejected.supplier_id])
        if retry.success:
            replacements = retry.purchase_orders
    return RejectOutput(purchase_order=rejected, replacement_orders=replacements)


@tool(PONumberInput)
def send_purchase_order(p: PONumberInput) -> Union[POOutput, ToolError]:
    with session_scope() as s:
        po = _load_po(s, p.po_number)
        if po is None:
            return fail(ErrorCode.PO_NOT_FOUND, f"Purchase order {p.po_number} not found.")
        lead = s.scalar(select(func.max(SupplierProduct.lead_time_days))
                        .where(SupplierProduct.supplier_id == po.supplier_id,
                               SupplierProduct.product_id.in_([i.product_id for i in po.items]))) or 1
        now = _now()
        po = _transition(s, p.po_number, ("approved",), "sent", sent_at=now, expected_at=now + timedelta(days=lead))
        if isinstance(po, ToolError):
            return po
        summary = _summary(s, po)
    lines = "\n".join(f"- {l.name}: {l.qty_ordered} @ Rs {l.unit_cost:,.0f}" for l in summary.lines)
    record_message(supplier_thread(summary.supplier_id), "out",
                   f"Purchase order {summary.po_number} from BizFlow Distribution:\n{lines}\n"
                   f"Total Rs {summary.total_cost:,.0f}. Please deliver by {summary.expected_at:%d %b %Y}.")
    return POOutput(purchase_order=summary)


class ReceiveLine(BaseModel):
    product_id: str = Field(min_length=1, max_length=10)
    qty: int = Field(gt=0, le=100_000)

    @field_validator("product_id")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class ReceiveInput(BaseModel):
    po_number: str = Field(min_length=1, max_length=20)
    received_by: str = Field(min_length=1, max_length=80)
    items: list[ReceiveLine] = Field(min_length=1)


class ReceiveOutput(ToolOk):
    purchase_order: POSummary
    backorders_ready: list[int]


@tool(ReceiveInput)
def receive_purchase_order(p: ReceiveInput) -> Union[ReceiveOutput, ToolError]:
    with session_scope() as s:
        po = _load_po(s, p.po_number, lock=True)
        if po is None:
            return fail(ErrorCode.PO_NOT_FOUND, f"Purchase order {p.po_number} not found.")
        if po.status not in ("sent", "partially_received"):
            return fail(ErrorCode.INVALID_TRANSITION,
                        f"{po.po_number} is '{po.status}'; goods can only be received for a sent order.",
                        status=po.status)
        if po.approved_by and po.approved_by.strip().lower() == p.received_by.strip().lower():
            return fail(ErrorCode.SAME_PERSON, "The person who approved this order cannot also record receiving it.")
        lines = {i.product_id: i for i in po.items}
        for item in p.items:
            line = lines.get(item.product_id)
            if line is None:
                return fail(ErrorCode.UNKNOWN_PRODUCT, f"{item.product_id} is not on {po.po_number}.")
            if line.qty_received + item.qty > line.qty_ordered:
                return fail(ErrorCode.RECEIPT_EXCEEDS_ORDER,
                            f"{item.product_id}: ordered {line.qty_ordered}, already received {line.qty_received}, "
                            f"cannot receive {item.qty} more.", product_id=item.product_id)
        for item in p.items:
            lines[item.product_id].qty_received += item.qty
            s.execute(text("UPDATE inventory SET physical = physical + :q, updated_at = now() WHERE product_id = :p"),
                      {"q": item.qty, "p": item.product_id})
        done = all(i.qty_received == i.qty_ordered for i in po.items)
        po.status = "received" if done else "partially_received"
        if done:
            po.received_at = _now()
        s.flush()
        ready = _mark_backorders_ready(s, [i.product_id for i in p.items])
        summary = _summary(s, po)
    for backorder_id, thread_id, name, qty, customer in ready:
        record_message(thread_id, "out", f"Good news: {name} is back in stock. Reply to order your {qty} "
                                         f"carton(s)/bag(s) from your earlier order.")
        notify_staff("sales", f"Backorder #{backorder_id} for {customer} is ready: {name} x {qty}.")
    return ReceiveOutput(purchase_order=summary, backorders_ready=[r[0] for r in ready])


def _mark_backorders_ready(s: Session, product_ids: list[str]) -> list[tuple]:
    ready = []
    rows = s.execute(text(
        "SELECT b.id, b.thread_id, b.product_id, b.qty, p.name, c.name AS customer, "
        "i.physical - i.reserved AS available "
        "FROM backorders b JOIN products p ON p.id = b.product_id JOIN customers c ON c.id = b.customer_id "
        "JOIN inventory i ON i.product_id = b.product_id "
        "WHERE b.status = 'waiting' AND b.product_id = ANY(:ids) ORDER BY b.id FOR UPDATE OF b"),
        {"ids": product_ids}).all()
    left: dict[str, int] = {}
    for r in rows:
        available = left.setdefault(r.product_id, r.available)
        if r.qty > available:
            continue
        left[r.product_id] = available - r.qty
        s.execute(text("UPDATE backorders SET status = 'ready', ready_at = now() WHERE id = :id"), {"id": r.id})
        ready.append((r.id, r.thread_id, r.name, r.qty, r.customer))
    return ready


class RestockEtaInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=10)

    @field_validator("product_id")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class RestockEtaOutput(ToolOk):
    product_id: str
    incoming_qty: int
    eta_days: Optional[int]
    po_number: Optional[str]
    po_status: Optional[str]


@tool(RestockEtaInput)
def get_restock_eta(p: RestockEtaInput) -> Union[RestockEtaOutput, ToolError]:
    with session_scope() as s:
        row = s.execute(
            select(PurchaseOrder.po_number, PurchaseOrder.status, PurchaseOrder.expected_at,
                   PurchaseOrderItem.qty_ordered - PurchaseOrderItem.qty_received, SupplierProduct.lead_time_days)
            .join(PurchaseOrderItem, PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
            .outerjoin(SupplierProduct, (SupplierProduct.supplier_id == PurchaseOrder.supplier_id)
                       & (SupplierProduct.product_id == PurchaseOrderItem.product_id))
            .where(PurchaseOrderItem.product_id == p.product_id, PurchaseOrder.status.in_(OPEN_PO_STATUSES))
            .order_by(PurchaseOrder.id).limit(1)).first()
    if row is None:
        return RestockEtaOutput(product_id=p.product_id, incoming_qty=0, eta_days=None, po_number=None, po_status=None)
    po_number, status, expected_at, incoming, lead = row
    if expected_at is not None:
        eta = max(0, math.ceil((expected_at - _now()).total_seconds() / 86400))
    else:
        eta = lead
    return RestockEtaOutput(product_id=p.product_id, incoming_qty=int(incoming), eta_days=eta, po_number=po_number,
                            po_status=status)


class BackorderLine(BaseModel):
    product_id: str
    qty: int = Field(gt=0)


class CreateBackordersInput(BaseModel):
    customer_id: int = Field(gt=0)
    order_id: int = Field(gt=0)
    thread_id: str = Field(min_length=1, max_length=64)
    items: list[BackorderLine] = Field(min_length=1)


class CreateBackordersOutput(ToolOk):
    backorder_ids: list[int]


@tool(CreateBackordersInput)
def create_backorders(p: CreateBackordersInput) -> Union[CreateBackordersOutput, ToolError]:
    with session_scope() as s:
        rows = [Backorder(customer_id=p.customer_id, order_id=p.order_id, thread_id=p.thread_id,
                          product_id=i.product_id, qty=i.qty, status="waiting") for i in p.items]
        s.add_all(rows)
        s.flush()
        return CreateBackordersOutput(backorder_ids=[r.id for r in rows])


def inventory_report() -> list[dict]:
    with session_scope() as s:
        extra = {pid: (cat, price, pack) for pid, cat, price, pack in
                 s.execute(select(Product.id, Product.category, Product.unit_price, Product.pack)).all()}
        report = []
        for row in _inventory_positions(s, None):
            category, price, pack = extra[row.id]
            position = row.physical - row.reserved + int(row.on_order)
            report.append({
                "product_id": row.id, "name": row.name, "category": category, "pack": pack, "unit_price": price,
                "physical": row.physical, "reserved": row.reserved, "available": row.physical - row.reserved,
                "on_order": int(row.on_order), "position": position, "reorder_level": row.reorder_level,
                "max_level": row.max_level, "needs_reorder": position <= row.reorder_level,
                "stock_value": price * row.physical,
            })
    return report
