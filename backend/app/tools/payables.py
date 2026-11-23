from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Union

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import PurchaseOrder, SupplierInvoice
from app.db.session import session_scope
from app.tools.base import ErrorCode, ToolError, ToolOk, fail, tool
from app.tools.catalog import money
from app.tools.notify import notify_staff, record_message
from app.tools.payments import PKT
from app.tools.procurement import supplier_thread


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(PKT).date()


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


class SupplierInvoiceSummary(BaseModel):
    invoice_id: int
    invoice_number: str
    supplier_id: int
    supplier_name: str
    po_number: str
    amount: Decimal
    expected_amount: Decimal
    difference: Decimal
    match_status: str
    invoice_date: date
    due_date: date
    days_until_due: int
    overdue: bool
    status: str
    recorded_by: str
    approved_by: Optional[str]
    override_reason: Optional[str]
    rejected_reason: Optional[str]
    paid_by: Optional[str]
    paid_at: Optional[datetime]
    payment_reference: Optional[str]


def _summary(inv: SupplierInvoice) -> SupplierInvoiceSummary:
    days = (inv.due_date - _today()).days
    return SupplierInvoiceSummary(
        invoice_id=inv.id, invoice_number=inv.invoice_number, supplier_id=inv.supplier_id,
        supplier_name=inv.supplier.name, po_number=inv.purchase_order.po_number, amount=inv.amount,
        expected_amount=inv.expected_amount, difference=inv.difference, match_status=inv.match_status,
        invoice_date=inv.invoice_date, due_date=inv.due_date, days_until_due=days,
        overdue=days < 0 and inv.status in ("awaiting_approval", "approved"), status=inv.status,
        recorded_by=inv.recorded_by, approved_by=inv.approved_by, override_reason=inv.override_reason,
        rejected_reason=inv.rejected_reason, paid_by=inv.paid_by, paid_at=inv.paid_at,
        payment_reference=inv.payment_reference,
    )


def _load(s: Session, invoice_id: int, lock: bool = False) -> Optional[SupplierInvoice]:
    q = (select(SupplierInvoice).where(SupplierInvoice.id == invoice_id)
         .options(selectinload(SupplierInvoice.supplier), selectinload(SupplierInvoice.purchase_order)))
    if lock:
        q = q.with_for_update(of=SupplierInvoice)
    return s.scalar(q)


class InvoiceOutput(ToolOk):
    invoice: SupplierInvoiceSummary


class RecordInvoiceInput(BaseModel):
    po_number: str = Field(min_length=1, max_length=20)
    invoice_number: str = Field(min_length=1, max_length=40)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    recorded_by: str = Field(min_length=1, max_length=80)
    invoice_date: Optional[date] = None


@tool(RecordInvoiceInput)
def record_supplier_invoice(p: RecordInvoiceInput) -> Union[InvoiceOutput, ToolError]:
    try:
        with session_scope() as s:
            po = s.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == p.po_number.strip().upper())
                          .options(selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier))
                          .with_for_update(of=PurchaseOrder))
            if po is None:
                return fail(ErrorCode.PO_NOT_FOUND, f"Purchase order {p.po_number} not found.")
            if po.status not in ("partially_received", "received"):
                return fail(ErrorCode.INVALID_TRANSITION,
                            f"{po.po_number} is '{po.status}': a supplier can only bill goods we have received.",
                            status=po.status)
            received_value = money(sum((i.unit_cost * i.qty_received for i in po.items), Decimal("0")))
            already_billed = s.scalar(select(func.coalesce(func.sum(SupplierInvoice.expected_amount), 0))
                                      .where(SupplierInvoice.purchase_order_id == po.id,
                                             SupplierInvoice.status != "rejected"))
            expected = max(money(received_value - Decimal(already_billed)), Decimal("0.00"))
            invoice_date = p.invoice_date or _today()
            inv = SupplierInvoice(
                supplier_id=po.supplier_id, purchase_order_id=po.id, invoice_number=p.invoice_number.strip().upper(),
                amount=money(p.amount), expected_amount=expected, difference=money(p.amount) - expected,
                match_status="matched" if money(p.amount) == expected else "mismatch", invoice_date=invoice_date,
                due_date=invoice_date + timedelta(days=po.supplier.payment_terms_days), status="awaiting_approval",
                recorded_by=p.recorded_by,
            )
            s.add(inv)
            s.flush()
            s.refresh(inv)
            summary = _summary(inv)
    except IntegrityError:
        return fail(ErrorCode.DUPLICATE_INVOICE,
                    f"Invoice {p.invoice_number} from this supplier is already recorded (it must not be paid twice).")
    check = ("matches what we received" if summary.match_status == "matched" else
             f"does NOT match: billed Rs {summary.amount:,.2f}, received goods worth Rs {summary.expected_amount:,.2f} "
             f"(difference Rs {summary.difference:,.2f})")
    notify_staff("manager", f"Supplier invoice {summary.invoice_number} from {summary.supplier_name} for "
                            f"{summary.po_number} {check}. Due {summary.due_date:%d %b %Y}. Needs approval.")
    return InvoiceOutput(invoice=summary)


class ApproveInvoiceInput(BaseModel):
    invoice_id: int = Field(gt=0)
    approved_by: str = Field(min_length=1, max_length=80)
    override_reason: Optional[str] = Field(default=None, max_length=500)


@tool(ApproveInvoiceInput)
def approve_supplier_invoice(p: ApproveInvoiceInput) -> Union[InvoiceOutput, ToolError]:
    with session_scope() as s:
        inv = _load(s, p.invoice_id, lock=True)
        if inv is None:
            return fail(ErrorCode.INVOICE_NOT_FOUND, f"Supplier invoice #{p.invoice_id} not found.")
        if inv.status != "awaiting_approval":
            return fail(ErrorCode.INVALID_TRANSITION, f"Invoice is '{inv.status}', not awaiting approval.",
                        status=inv.status)
        if _same(inv.recorded_by, p.approved_by):
            return fail(ErrorCode.SAME_PERSON, "The person who recorded this invoice cannot also approve it.")
        if inv.match_status == "mismatch" and not (p.override_reason or "").strip():
            return fail(ErrorCode.INVOICE_MISMATCH,
                        f"The invoice is Rs {inv.difference:,.2f} different from the goods received. Reject it and ask "
                        f"the supplier for a corrected invoice, or approve with an override_reason.",
                        difference=str(inv.difference))
        inv.status, inv.approved_by, inv.approved_at = "approved", p.approved_by, datetime.now(timezone.utc)
        inv.override_reason = p.override_reason
        return InvoiceOutput(invoice=_summary(inv))


class RejectInvoiceInput(BaseModel):
    invoice_id: int = Field(gt=0)
    rejected_by: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)


@tool(RejectInvoiceInput)
def reject_supplier_invoice(p: RejectInvoiceInput) -> Union[InvoiceOutput, ToolError]:
    with session_scope() as s:
        inv = _load(s, p.invoice_id, lock=True)
        if inv is None:
            return fail(ErrorCode.INVOICE_NOT_FOUND, f"Supplier invoice #{p.invoice_id} not found.")
        if inv.status != "awaiting_approval":
            return fail(ErrorCode.INVALID_TRANSITION, f"Invoice is '{inv.status}', not awaiting approval.",
                        status=inv.status)
        inv.status, inv.rejected_by, inv.rejected_reason = "rejected", p.rejected_by, p.reason
        summary = _summary(inv)
    record_message(supplier_thread(summary.supplier_id), "out",
                   f"Your invoice {summary.invoice_number} for {summary.po_number} was not accepted: {p.reason}. "
                   f"Please send a corrected invoice.")
    return InvoiceOutput(invoice=summary)


class PayInvoiceInput(BaseModel):
    invoice_id: int = Field(gt=0)
    paid_by: str = Field(min_length=1, max_length=80)
    payment_reference: str = Field(min_length=3, max_length=60)


@tool(PayInvoiceInput)
def pay_supplier_invoice(p: PayInvoiceInput) -> Union[InvoiceOutput, ToolError]:
    with session_scope() as s:
        inv = _load(s, p.invoice_id, lock=True)
        if inv is None:
            return fail(ErrorCode.INVOICE_NOT_FOUND, f"Supplier invoice #{p.invoice_id} not found.")
        if inv.status != "approved":
            return fail(ErrorCode.INVALID_TRANSITION, f"Invoice is '{inv.status}'; only approved invoices can be paid.",
                        status=inv.status)
        if _same(inv.approved_by, p.paid_by):
            return fail(ErrorCode.SAME_PERSON, "The person who approved this invoice cannot also record paying it.")
        inv.status, inv.paid_by, inv.paid_at = "paid", p.paid_by, datetime.now(timezone.utc)
        inv.payment_reference = p.payment_reference.strip()
        summary = _summary(inv)
    record_message(supplier_thread(summary.supplier_id), "out",
                   f"Payment of Rs {summary.amount:,.2f} for your invoice {summary.invoice_number} ({summary.po_number}) "
                   f"has been sent. Reference {summary.payment_reference}.")
    return InvoiceOutput(invoice=summary)


class ListInvoicesInput(BaseModel):
    status: Optional[str] = None
    overdue_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class ListInvoicesOutput(ToolOk):
    count: int
    total_open: Decimal
    invoices: list[SupplierInvoiceSummary]


@tool(ListInvoicesInput)
def list_supplier_invoices(p: ListInvoicesInput) -> Union[ListInvoicesOutput, ToolError]:
    with session_scope() as s:
        q = (select(SupplierInvoice)
             .options(selectinload(SupplierInvoice.supplier), selectinload(SupplierInvoice.purchase_order))
             .order_by(SupplierInvoice.due_date, SupplierInvoice.id).limit(p.limit))
        if p.status:
            q = q.where(SupplierInvoice.status == p.status)
        rows = [_summary(inv) for inv in s.scalars(q)]
    if p.overdue_only:
        rows = [r for r in rows if r.overdue]
    total_open = sum((r.amount for r in rows if r.status in ("awaiting_approval", "approved")), Decimal("0"))
    return ListInvoicesOutput(count=len(rows), total_open=money(total_open), invoices=rows)
