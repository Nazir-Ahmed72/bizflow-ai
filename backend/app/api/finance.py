from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from app.api.staff import _ok
from app.tools import (
    approve_supplier_invoice, cancel_unpaid_orders, list_payments, list_supplier_invoices, pay_supplier_invoice,
    record_supplier_invoice, reject_payment, reject_supplier_invoice, verify_payment,
)

router = APIRouter(prefix="/staff", tags=["staff: payments"])


class VerifyBody(BaseModel):
    verified_by: str = Field(min_length=1, max_length=80, examples=["Accountant Sana"])


class RejectPaymentBody(BaseModel):
    rejected_by: str = Field(min_length=1, max_length=80, examples=["Accountant Sana"])
    reason: str = Field(min_length=1, max_length=300, examples=["No transfer with this reference in our bank statement"])


class InvoiceBody(BaseModel):
    po_number: str = Field(examples=["PO-000001"])
    invoice_number: str = Field(min_length=1, max_length=40, examples=["MS-2291"])
    amount: Decimal = Field(gt=0, examples=["37200.00"])
    recorded_by: str = Field(min_length=1, max_length=80, examples=["Clerk Hamza"])
    invoice_date: Optional[date] = None


class ApproveInvoiceBody(BaseModel):
    approved_by: str = Field(min_length=1, max_length=80, examples=["Manager Asif"])
    override_reason: Optional[str] = Field(default=None, max_length=500)


class RejectInvoiceBody(BaseModel):
    rejected_by: str = Field(min_length=1, max_length=80, examples=["Manager Asif"])
    reason: str = Field(min_length=1, max_length=500, examples=["Billed 62 cartons, we received 60"])


class PayInvoiceBody(BaseModel):
    paid_by: str = Field(min_length=1, max_length=80, examples=["Accountant Sana"])
    payment_reference: str = Field(min_length=3, max_length=60, examples=["IBFT-889211"])


@router.get("/payments")
def payments(status: Optional[str] = None, limit: int = 50) -> dict:
    return _ok(list_payments(status=status, limit=min(max(limit, 1), 200)))


PAYMENT_PATH = Path(description="Payment number (1), order number (ORD-000005) or the customer's transaction ID",
                    examples=["ORD-000005"])


@router.post("/payments/{payment}/verify")
def verify(body: VerifyBody, payment: str = PAYMENT_PATH) -> dict:
    return _ok(verify_payment(payment=payment, verified_by=body.verified_by))


@router.post("/payments/{payment}/reject")
def reject(body: RejectPaymentBody, payment: str = PAYMENT_PATH) -> dict:
    return _ok(reject_payment(payment=payment, rejected_by=body.rejected_by, reason=body.reason))


@router.post("/payments/cancel-unpaid")
def cancel_unpaid() -> dict:
    return _ok(cancel_unpaid_orders())


@router.get("/supplier-invoices")
def supplier_invoices(status: Optional[str] = None, overdue_only: bool = False, limit: int = 50) -> dict:
    return _ok(list_supplier_invoices(status=status, overdue_only=overdue_only, limit=min(max(limit, 1), 200)))


@router.post("/supplier-invoices")
def record_invoice(body: InvoiceBody) -> dict:
    return _ok(record_supplier_invoice(**body.model_dump()))


@router.post("/supplier-invoices/{invoice_id}/approve")
def approve_invoice(invoice_id: int, body: ApproveInvoiceBody) -> dict:
    return _ok(approve_supplier_invoice(invoice_id=invoice_id, **body.model_dump()))


@router.post("/supplier-invoices/{invoice_id}/reject")
def reject_invoice(invoice_id: int, body: RejectInvoiceBody) -> dict:
    return _ok(reject_supplier_invoice(invoice_id=invoice_id, **body.model_dump()))


@router.post("/supplier-invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: int, body: PayInvoiceBody) -> dict:
    return _ok(pay_supplier_invoice(invoice_id=invoice_id, **body.model_dump()))
