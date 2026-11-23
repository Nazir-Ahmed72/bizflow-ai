from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.tools import (
    ErrorCode, approve_purchase_order, get_purchase_order, list_purchase_orders, receive_purchase_order,
    reject_purchase_order, run_reorder_check, send_purchase_order,
)

router = APIRouter(prefix="/staff", tags=["staff: purchasing"])

STATUS_FOR_ERROR = {
    ErrorCode.PO_NOT_FOUND: 404,
    ErrorCode.INVALID_TRANSITION: 409,
    ErrorCode.SAME_PERSON: 403,
    ErrorCode.RECEIPT_EXCEEDS_ORDER: 422,
    ErrorCode.UNKNOWN_PRODUCT: 422,
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.PAYMENT_NOT_FOUND: 404,
    ErrorCode.AMBIGUOUS_PAYMENT: 409,
    ErrorCode.DELIVERY_NOT_FOUND: 404,
    ErrorCode.RIDER_NOT_FOUND: 404,
    ErrorCode.NOT_YOUR_DELIVERY: 403,
    ErrorCode.RIDER_BUSY: 409,
    ErrorCode.INVOICE_NOT_FOUND: 404,
    ErrorCode.ORDER_CANCELLED: 409,
    ErrorCode.DUPLICATE_INVOICE: 409,
    ErrorCode.INVOICE_MISMATCH: 422,
}


def _ok(result: Any) -> dict:
    if not result.success:
        raise HTTPException(status_code=STATUS_FOR_ERROR.get(result.error_code, 500),
                            detail={"error_code": result.error_code, "message": result.message})
    return result.model_dump(mode="json")


class ApproveBody(BaseModel):
    approved_by: str = Field(min_length=1, max_length=80, examples=["Manager Asif"])


class RejectBody(BaseModel):
    rejected_by: str = Field(min_length=1, max_length=80, examples=["Manager Asif"])
    reason: str = Field(min_length=1, max_length=500, examples=["Supplier cannot deliver this week"])
    try_next_supplier: bool = True


class ReceiveItem(BaseModel):
    product_id: str = Field(examples=["P-006"])
    qty: int = Field(gt=0, examples=[62])


class ReceiveBody(BaseModel):
    received_by: str = Field(min_length=1, max_length=80, examples=["Storekeeper Bilal"])
    items: list[ReceiveItem] = Field(min_length=1)


class ReorderCheckBody(BaseModel):
    product_ids: Optional[list[str]] = None


@router.post("/reorder-check")
def reorder_check(body: Optional[ReorderCheckBody] = None) -> dict:
    return _ok(run_reorder_check(reason="manual reorder check", product_ids=(body.product_ids if body else None)))


@router.get("/purchase-orders")
def purchase_orders(status: Optional[str] = None, limit: int = 50) -> dict:
    return _ok(list_purchase_orders(status=status, limit=min(max(limit, 1), 200)))


@router.get("/purchase-orders/{po_number}")
def purchase_order(po_number: str) -> dict:
    return _ok(get_purchase_order(po_number))


@router.post("/purchase-orders/{po_number}/approve")
def approve(po_number: str, body: ApproveBody) -> dict:
    return _ok(approve_purchase_order(po_number=po_number, approved_by=body.approved_by))


@router.post("/purchase-orders/{po_number}/reject")
def reject(po_number: str, body: RejectBody) -> dict:
    return _ok(reject_purchase_order(po_number=po_number, rejected_by=body.rejected_by, reason=body.reason,
                                     try_next_supplier=body.try_next_supplier))


@router.post("/purchase-orders/{po_number}/send")
def send(po_number: str) -> dict:
    return _ok(send_purchase_order(po_number))


@router.post("/purchase-orders/{po_number}/receive")
def receive(po_number: str, body: ReceiveBody) -> dict:
    return _ok(receive_purchase_order(po_number=po_number, received_by=body.received_by,
                                      items=[i.model_dump() for i in body.items]))
