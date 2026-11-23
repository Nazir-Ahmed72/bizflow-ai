from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import Backorder, Delivery, Order, OrderDraft, Payment
from app.db.session import session_scope
from app.tools.base import Abort, ErrorCode, ToolError, ToolOk, fail, tool
from app.tools.notify import notify_staff, record_message
from app.tools.orders import latest_payment_status, release_reservation

PKT = timezone(timedelta(hours=5), "PKT")
DISPATCH_CUTOFF_HOUR = 14
REVIEW_REMINDER_AFTER = timedelta(hours=2)
RECENT_CANCEL_WINDOW = timedelta(days=3)
LIVE_PAYMENT_STATUSES = ("pending_verification", "verified")
PAY_TO = {
    "bank": "Meezan Bank",
    "account_title": "BizFlow Distribution",
    "account_number": "0101 0105 4321 77",
    "jazzcash_easypaisa": "0300 0000 777",
}
Method = Literal["bank_transfer", "jazzcash", "easypaisa"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def local_time_text(moment: datetime) -> str:
    return moment.astimezone(PKT).strftime("%d %b %Y, %I:%M %p")


def delivery_date_for(paid_at: datetime) -> date:
    local = paid_at.astimezone(PKT)
    return local.date() if local.hour < DISPATCH_CUTOFF_HOUR else local.date() + timedelta(days=1)


def _day_words(delivery: date, paid_at: datetime, lang: str) -> str:
    today = delivery == paid_at.astimezone(PKT).date()
    words = {"en": ("today", "tomorrow"), "roman_ur": ("aaj", "kal"), "ur": ("آج", "کل")}.get(lang, ("today", "tomorrow"))
    return f"{words[0] if today else words[1]} ({delivery:%d %b})"


def rs_text(amount: Decimal) -> str:
    return f"Rs {int(amount):,}" if amount == amount.to_integral_value() else f"Rs {amount:,.2f}"


MESSAGES = {
    "verified": {
        "en": "Payment of {amount} received for order {order}. Your order is confirmed and will be delivered {day} "
              "to {address}. Thank you!",
        "roman_ur": "Order {order} ki payment {amount} mil gayi hai. Aap ka order confirm ho gaya hai aur {day} "
                    "{address} par deliver hoga. Shukriya!",
        "ur": "آرڈر {order} کی ادائیگی {amount} موصول ہو گئی ہے۔ آپ کا آرڈر کنفرم ہو گیا ہے اور {day} {address} پر "
              "ڈیلیور ہوگا۔ شکریہ!",
    },
    "rejected": {
        "en": "We could not confirm your payment for order {order} (reference {reference}): {reason}. Please check "
              "and send the correct transaction ID before {deadline}.",
        "roman_ur": "Order {order} ki payment (reference {reference}) confirm nahi ho saki: {reason}. Baraye "
                    "meharbani check kar ke sahi transaction ID {deadline} se pehle bhej dein.",
        "ur": "آرڈر {order} کی ادائیگی (ریفرنس {reference}) کنفرم نہیں ہو سکی: {reason}۔ براہ کرم چیک کر کے درست "
              "ٹرانزیکشن آئی ڈی {deadline} سے پہلے بھیج دیں۔",
    },
    "cancelled": {
        "en": "Order {order} has been cancelled because no confirmed payment was received in time. You can order "
              "again any time.",
        "roman_ur": "Order {order} cancel kar diya gaya hai kyunke waqt par payment confirm nahi hui. Aap kabhi bhi "
                    "dobara order kar sakte hain.",
        "ur": "آرڈر {order} منسوخ کر دیا گیا ہے کیونکہ وقت پر ادائیگی کنفرم نہیں ہوئی۔ آپ کسی بھی وقت دوبارہ آرڈر کر "
              "سکتے ہیں۔",
    },
}


def order_language(s: Session, order: Order) -> str:
    return s.scalar(select(OrderDraft.language).where(OrderDraft.id == order.draft_id)) or "en"


def order_thread(s: Session, order: Order) -> str:
    return s.scalar(select(OrderDraft.thread_id).where(OrderDraft.id == order.draft_id))


def tell_customer(s: Session, order: Order, thread_id: str, template: dict[str, str], **values: str) -> str:
    lang = order_language(s, order)
    text = template.get(lang, template["en"]).format(order=order.order_number, **values)
    record_message(thread_id, "out", text, order.customer_id, session=s)
    return text


class PaymentSummary(BaseModel):
    payment_id: int
    order_number: str
    customer_name: str
    thread_id: str
    amount: Decimal
    method: str
    reference: str
    status: str
    submitted_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    rejected_reason: Optional[str]
    order_status: str
    payment_due_at: Optional[datetime]
    delivery_date: Optional[date]


def _summary(p: Payment) -> PaymentSummary:
    return PaymentSummary(
        payment_id=p.id, order_number=p.order.order_number, customer_name=p.customer.name, thread_id=p.thread_id,
        amount=p.amount, method=p.method, reference=p.reference, status=p.status, submitted_at=p.submitted_at,
        reviewed_by=p.reviewed_by, reviewed_at=p.reviewed_at, rejected_reason=p.rejected_reason,
        order_status=p.order.status, payment_due_at=p.order.payment_due_at, delivery_date=p.order.delivery_date,
    )


def _load_payment(s: Session, payment_id: int, lock: bool = False) -> Optional[Payment]:
    q = (select(Payment).where(Payment.id == payment_id)
         .options(selectinload(Payment.order).selectinload(Order.items), selectinload(Payment.customer)))
    if lock:
        q = q.with_for_update(of=Payment)
    return s.scalar(q)


def _lock_order(s: Session, order_id: int) -> Order:
    return s.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.items))
                    .with_for_update(of=Order).execution_options(populate_existing=True))


class SubmitPaymentInput(BaseModel):
    customer_id: int = Field(gt=0)
    thread_id: str = Field(min_length=1, max_length=64)
    method: Method = "bank_transfer"
    reference: str = Field(min_length=4, max_length=60)
    order_number: Optional[str] = Field(default=None, max_length=20)

    @field_validator("reference")
    @classmethod
    def _clean(cls, v: str) -> str:
        cleaned = "".join(v.split()).upper()
        if len(cleaned) < 4:
            raise ValueError("transaction reference is too short")
        return cleaned


class SubmitPaymentOutput(ToolOk):
    already_submitted: bool
    payment: PaymentSummary


@tool(SubmitPaymentInput)
def submit_payment(p: SubmitPaymentInput) -> Union[SubmitPaymentOutput, ToolError]:
    try:
        with session_scope() as s:
            q = select(Order).where(Order.customer_id == p.customer_id)
            if p.order_number:
                q = q.where(Order.order_number == p.order_number.strip().upper())
            else:
                q = q.where(Order.status == "awaiting_payment")
            order = s.scalar(q.order_by(Order.id.desc()).limit(1).with_for_update())
            if order is None and not p.order_number:
                order = s.scalar(select(Order).where(Order.customer_id == p.customer_id,
                                                     Order.status == "cancelled",
                                                     Order.cancelled_at >= _now() - RECENT_CANCEL_WINDOW)
                                 .order_by(Order.id.desc()).limit(1))
            if order is None:
                if p.order_number:
                    return fail(ErrorCode.ORDER_NOT_FOUND, f"No order {p.order_number} on this account.")
                return fail(ErrorCode.NO_UNPAID_ORDER, "This customer has no order waiting for payment.")
            live = s.scalar(select(Payment).where(Payment.order_id == order.id,
                                                  Payment.status.in_(LIVE_PAYMENT_STATUSES)))
            if live is not None and live.status == "pending_verification":
                return SubmitPaymentOutput(already_submitted=True, payment=_summary(live))
            if order.status == "cancelled":
                return fail(ErrorCode.ORDER_CANCELLED, f"Order {order.order_number} was cancelled.",
                            order_number=order.order_number)
            if live is not None or order.status != "awaiting_payment":
                return fail(ErrorCode.ALREADY_PAID, f"Order {order.order_number} is already paid.",
                            order_number=order.order_number,
                            delivery_date=str(order.delivery_date) if order.delivery_date else None)
            reused = s.scalar(select(Payment.id).where(Payment.method == p.method, Payment.reference == p.reference,
                                                       Payment.status != "rejected"))
            if reused is not None:
                return fail(ErrorCode.DUPLICATE_REFERENCE,
                            f"Transaction reference {p.reference} was already used for another payment.")
            payment = Payment(order_id=order.id, customer_id=order.customer_id, thread_id=p.thread_id,
                              amount=order.total, method=p.method, reference=p.reference,
                              status="pending_verification")
            s.add(payment)
            s.flush()
            s.refresh(payment)
            summary = _summary(payment)
    except IntegrityError:
        return fail(ErrorCode.DUPLICATE_REFERENCE,
                    f"Transaction reference {p.reference} was already used for another payment.")
    notify_staff("accounts", f"Verify payment #{summary.payment_id}: {summary.customer_name} says they paid "
                             f"{rs_text(summary.amount)} for {summary.order_number} by {summary.method}, "
                             f"reference {summary.reference}.")
    return SubmitPaymentOutput(already_submitted=False, payment=summary)


class ListPaymentsInput(BaseModel):
    status: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)


class ListPaymentsOutput(ToolOk):
    count: int
    payments: list[PaymentSummary]


@tool(ListPaymentsInput)
def list_payments(p: ListPaymentsInput) -> Union[ListPaymentsOutput, ToolError]:
    with session_scope() as s:
        q = (select(Payment).options(selectinload(Payment.order), selectinload(Payment.customer))
             .order_by(Payment.id).limit(p.limit))
        if p.status:
            q = q.where(Payment.status == p.status)
        rows = [_summary(x) for x in s.scalars(q)]
    return ListPaymentsOutput(count=len(rows), payments=rows)


PaymentKey = Union[int, str]


class VerifyPaymentInput(BaseModel):
    payment: PaymentKey
    verified_by: str = Field(min_length=1, max_length=80)


class ReviewOutput(ToolOk):
    payment: PaymentSummary
    customer_message: str


def _matching_ids(s: Session, key: PaymentKey) -> set[int]:
    raw = "".join(str(key).split()).upper().lstrip("#")
    pending_first = (Payment.status != "pending_verification", Payment.id.desc())
    if raw.startswith("ORD-"):
        found = s.scalar(select(Payment.id).join(Order, Order.id == Payment.order_id)
                         .where(Order.order_number == raw).order_by(*pending_first).limit(1))
        return {found} if found else set()
    ids = set()
    by_reference = s.scalar(select(Payment.id).where(Payment.reference == raw).order_by(*pending_first).limit(1))
    if by_reference:
        ids.add(by_reference)
    if raw.isdigit() and int(raw) > 0 and s.get(Payment, int(raw)) is not None:
        ids.add(int(raw))
    return ids


def _waiting_list(s: Session) -> str:
    rows = s.execute(select(Payment.id, Order.order_number, Payment.reference).join(Order, Order.id == Payment.order_id)
                     .where(Payment.status == "pending_verification").order_by(Payment.id).limit(10)).all()
    if not rows:
        return "No payments are waiting for verification."
    return "Waiting for verification: " + "; ".join(f"payment {i} = {n}, transaction ID {r}" for i, n, r in rows)


def _pending_payment(s: Session, key: PaymentKey) -> Union[Payment, ToolError]:
    ids = _matching_ids(s, key)
    if not ids:
        return fail(ErrorCode.PAYMENT_NOT_FOUND,
                    f"No payment matches '{key}'. Use the payment number, the order number (ORD-000005) or the "
                    f"customer's transaction ID. {_waiting_list(s)}")
    if len(ids) > 1:
        return fail(ErrorCode.AMBIGUOUS_PAYMENT,
                    f"'{key}' is both a payment number and another payment's transaction ID. Use the order number "
                    f"instead. {_waiting_list(s)}", payment_ids=sorted(ids))
    payment = _load_payment(s, ids.pop(), lock=True)
    if payment.status != "pending_verification":
        return fail(ErrorCode.INVALID_TRANSITION,
                    f"Payment {payment.id} ({payment.order.order_number}) is already {payment.status}.",
                    status=payment.status)
    return payment


@tool(VerifyPaymentInput)
def verify_payment(p: VerifyPaymentInput) -> Union[ReviewOutput, ToolError]:
    with session_scope() as s:
        payment = _pending_payment(s, p.payment)
        if isinstance(payment, ToolError):
            return payment
        order = _lock_order(s, payment.order_id)
        if order.status != "awaiting_payment":
            return fail(ErrorCode.ORDER_CANCELLED if order.status == "cancelled" else ErrorCode.INVALID_TRANSITION,
                        f"Order {order.order_number} is {order.status}; this payment needs manual handling.",
                        order_status=order.status)
        now = _now()
        payment.status, payment.reviewed_by, payment.reviewed_at = "verified", p.verified_by, now
        order.status, order.paid_at, order.delivery_date = "ready_to_dispatch", now, delivery_date_for(now)
        s.add(Delivery(order_id=order.id, attempt=1, status="queued", scheduled_date=order.delivery_date))
        text = tell_customer(s, order, payment.thread_id, MESSAGES["verified"], amount=rs_text(order.total),
                              day=_day_words(order.delivery_date, now, order_language(s, order)),
                              address=payment.customer.address)
        s.flush()
        summary = _summary(payment)
    notify_staff("warehouse", f"{summary.order_number} for {summary.customer_name} is paid: dispatch on "
                              f"{summary.delivery_date:%d %b %Y}.")
    return ReviewOutput(payment=summary, customer_message=text)


class RejectPaymentInput(BaseModel):
    payment: PaymentKey
    rejected_by: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=300)


@tool(RejectPaymentInput)
def reject_payment(p: RejectPaymentInput) -> Union[ReviewOutput, ToolError]:
    try:
        with session_scope() as s:
            payment = _pending_payment(s, p.payment)
            if isinstance(payment, ToolError):
                return payment
            order = _lock_order(s, payment.order_id)
            now = _now()
            payment.status, payment.reviewed_by, payment.reviewed_at = "rejected", p.rejected_by, now
            payment.rejected_reason = p.reason
            if order.status == "awaiting_payment" and order.payment_due_at and order.payment_due_at <= now:
                _cancel(s, order, f"payment rejected after the deadline: {p.reason}")
                text = tell_customer(s, order, payment.thread_id, MESSAGES["cancelled"])
            else:
                text = tell_customer(s, order, payment.thread_id, MESSAGES["rejected"], reference=payment.reference,
                                      reason=p.reason, deadline=local_time_text(order.payment_due_at or now))
            s.flush()
            summary = _summary(payment)
    except Abort as a:
        return a.error
    return ReviewOutput(payment=summary, customer_message=text)


def _cancel(s: Session, order: Order, reason: str) -> None:
    release_reservation(s, order)
    order.status, order.cancelled_at, order.cancel_reason = "cancelled", _now(), reason
    for b in s.scalars(select(Backorder).where(Backorder.order_id == order.id, Backorder.status == "waiting")):
        b.status = "cancelled"


class CancelUnpaidInput(BaseModel):
    now: Optional[datetime] = None


class CancelledOrder(BaseModel):
    order_number: str
    customer_id: int
    payment_due_at: datetime


class CancelUnpaidOutput(ToolOk):
    cancelled: list[CancelledOrder]
    waiting_for_review: int
    reminder_sent: bool


@tool(CancelUnpaidInput)
def cancel_unpaid_orders(p: CancelUnpaidInput) -> Union[CancelUnpaidOutput, ToolError]:
    now = p.now or _now()
    live = exists().where(Payment.order_id == Order.id, Payment.status.in_(LIVE_PAYMENT_STATUSES))
    cancelled: list[CancelledOrder] = []
    try:
        with session_scope() as s:
            candidates = list(s.scalars(select(Order.id).where(Order.status == "awaiting_payment",
                                                               Order.payment_due_at <= now, ~live)
                                        .order_by(Order.id)))
            for order_id in candidates:
                order = _lock_order(s, order_id)
                still_live = s.scalar(select(Payment.id).where(Payment.order_id == order_id,
                                                               Payment.status.in_(LIVE_PAYMENT_STATUSES)))
                if order.status != "awaiting_payment" or still_live is not None:
                    continue
                _cancel(s, order, "no payment within 24 hours")
                tell_customer(s, order, order_thread(s, order), MESSAGES["cancelled"])
                cancelled.append(CancelledOrder(order_number=order.order_number, customer_id=order.customer_id,
                                                payment_due_at=order.payment_due_at))
            stale = list(s.scalars(select(Payment).where(Payment.status == "pending_verification",
                                                         Payment.submitted_at <= now - REVIEW_REMINDER_AFTER)
                                   .options(selectinload(Payment.order))))
            stale_text = ", ".join(f"#{x.id} ({x.order.order_number})" for x in stale)
    except Abort as a:
        return a.error
    if stale:
        notify_staff("accounts", f"Reminder: {len(stale)} payment(s) waiting for verification for more than "
                                 f"2 hours: {stale_text}. Customers cannot get their goods until you verify.")
    return CancelUnpaidOutput(cancelled=cancelled, waiting_for_review=len(stale), reminder_sent=bool(stale))


class OrderStatusInput(BaseModel):
    customer_id: int = Field(gt=0)
    order_number: Optional[str] = Field(default=None, max_length=20)


class OrderStatusOutput(ToolOk):
    found: bool
    order_number: Optional[str] = None
    status: Optional[str] = None
    total: Optional[Decimal] = None
    payment_status: Optional[str] = None
    pay_before: Optional[str] = None
    pay_to: Optional[dict] = None
    delivery_date: Optional[str] = None
    delivery_attempt: Optional[int] = None
    rider_name: Optional[str] = None
    rider_phone: Optional[str] = None
    last_failure: Optional[str] = None
    cancel_reason: Optional[str] = None
    meaning: Optional[str] = None


STATUS_MEANING = {
    "awaiting_payment": "Stock is reserved. We are waiting for the customer's online payment (pay_to, use the order "
                        "number as reference) before pay_before.",
    "awaiting_payment+pending_verification": "The customer reported a payment; our accounts team is verifying it. "
                                             "The customer will get a message as soon as it is verified.",
    "awaiting_payment+rejected": "The last payment could not be verified; the customer should send the correct "
                                 "transaction ID before pay_before.",
    "ready_to_dispatch": "Paid and confirmed. It will be delivered on delivery_date; a rider is assigned that day.",
    "ready_to_dispatch+assigned": "Paid. Rider rider_name (rider_phone) will deliver it on delivery_date.",
    "ready_to_dispatch+retry": "The first delivery attempt failed (last_failure). We will try again on delivery_date.",
    "out_for_delivery": "On the way now with rider rider_name (rider_phone).",
    "delivered": "Delivered.",
    "returned": "Delivery failed twice, so the order went back to the warehouse; our team arranges the refund.",
    "cancelled": "The order was cancelled (see cancel_reason).",
}


@tool(OrderStatusInput)
def get_order_status(p: OrderStatusInput) -> Union[OrderStatusOutput, ToolError]:
    with session_scope() as s:
        q = select(Order).where(Order.customer_id == p.customer_id)
        if p.order_number:
            q = q.where(Order.order_number == p.order_number.strip().upper())
        order = s.scalar(q.order_by(Order.id.desc()).limit(1))
        if order is None:
            return OrderStatusOutput(found=False)
        payment_status = latest_payment_status(s, order.id)
        delivery = s.scalar(select(Delivery).where(Delivery.order_id == order.id)
                            .options(selectinload(Delivery.rider)).order_by(Delivery.id.desc()).limit(1))
        failed = s.scalar(select(Delivery.failure_reason).where(Delivery.order_id == order.id,
                                                                Delivery.status == "failed")
                          .order_by(Delivery.id.desc()).limit(1))
        key = order.status
        if order.status == "awaiting_payment" and payment_status in ("pending_verification", "rejected"):
            key = f"{order.status}+{payment_status}"
        elif order.status == "ready_to_dispatch" and delivery is not None:
            if delivery.status == "assigned":
                key = "ready_to_dispatch+assigned"
            elif delivery.attempt > 1:
                key = "ready_to_dispatch+retry"
        unpaid = order.status == "awaiting_payment"
        rider = delivery.rider if delivery is not None and delivery.status in ("assigned", "out_for_delivery") else None
        return OrderStatusOutput(
            found=True, order_number=order.order_number, status=order.status, total=order.total,
            payment_status=payment_status,
            pay_before=local_time_text(order.payment_due_at) if unpaid and order.payment_due_at else None,
            pay_to=PAY_TO if unpaid else None,
            delivery_date=f"{order.delivery_date:%d %b %Y}" if order.delivery_date else None,
            delivery_attempt=delivery.attempt if delivery is not None else None,
            rider_name=rider.name if rider else None, rider_phone=rider.phone if rider else None,
            last_failure=failed, cancel_reason=order.cancel_reason, meaning=STATUS_MEANING[key],
        )
