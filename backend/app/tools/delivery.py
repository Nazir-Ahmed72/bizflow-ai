from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union

from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.db.models import Delivery, Order, Rider
from app.db.session import session_scope
from app.tools.base import Abort, ErrorCode, ToolError, ToolOk, fail, tool
from app.tools.notify import notify_staff
from app.tools.orders import release_reservation
from app.tools.payments import PKT, order_thread, rs_text, tell_customer

MAX_ATTEMPTS = 2

MESSAGES = {
    "assigned": {
        "en": "Your order {order} will be delivered today by our rider {rider} ({phone}).",
        "roman_ur": "Aap ka order {order} aaj hamara rider {rider} ({phone}) deliver karega.",
        "ur": "آپ کا آرڈر {order} آج ہمارا رائیڈر {rider} ({phone}) ڈیلیور کرے گا۔",
    },
    "picked_up": {
        "en": "Your order {order} is on its way with {rider} ({phone}).",
        "roman_ur": "Aap ka order {order} rawana ho gaya hai, rider {rider} ({phone}) aa raha hai.",
        "ur": "آپ کا آرڈر {order} روانہ ہو گیا ہے، رائیڈر {rider} ({phone}) آ رہا ہے۔",
    },
    "delivered": {
        "en": "Order {order} has been delivered. Thank you for your business!",
        "roman_ur": "Order {order} deliver ho gaya hai. Shukriya!",
        "ur": "آرڈر {order} ڈیلیور ہو گیا ہے۔ شکریہ!",
    },
    "retry": {
        "en": "We could not deliver order {order} today ({reason}). We will try again on {day}.",
        "roman_ur": "Order {order} aaj deliver nahi ho saka ({reason}). Hum {day} ko dobara koshish karenge.",
        "ur": "آرڈر {order} آج ڈیلیور نہیں ہو سکا ({reason})۔ ہم {day} کو دوبارہ کوشش کریں گے۔",
    },
    "returned": {
        "en": "We could not deliver order {order} after two attempts, so it has gone back to our warehouse. Our team "
              "will contact you about the refund of {amount}.",
        "roman_ur": "Order {order} do dafa koshish ke baad bhi deliver nahi ho saka, is liye warehouse wapas aa gaya "
                    "hai. Hamari team {amount} ki wapsi ke liye aap se rabta karegi.",
        "ur": "آرڈر {order} دو کوششوں کے بعد بھی ڈیلیور نہیں ہو سکا، اس لیے گودام واپس آ گیا ہے۔ ہماری ٹیم {amount} کی "
              "واپسی کے لیے آپ سے رابطہ کرے گی۔",
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def today_pkt() -> date:
    return _now().astimezone(PKT).date()


class DeliverySummary(BaseModel):
    delivery_id: int
    order_number: str
    order_status: str
    customer_name: str
    address: str
    area: Optional[str]
    attempt: int
    status: str
    scheduled_date: date
    rider_id: Optional[int]
    rider_name: Optional[str]
    rider_phone: Optional[str]
    failure_reason: Optional[str]


def _summary(d: Delivery) -> DeliverySummary:
    c = d.order.customer
    return DeliverySummary(
        delivery_id=d.id, order_number=d.order.order_number, order_status=d.order.status, customer_name=c.name,
        address=c.address, area=c.area, attempt=d.attempt, status=d.status, scheduled_date=d.scheduled_date,
        rider_id=d.rider_id, rider_name=d.rider.name if d.rider else None,
        rider_phone=d.rider.phone if d.rider else None, failure_reason=d.failure_reason,
    )


def _options():
    return (selectinload(Delivery.order).selectinload(Order.customer),
            selectinload(Delivery.order).selectinload(Order.items), selectinload(Delivery.rider))


def _load(s: Session, delivery_id: int) -> Optional[Delivery]:
    return s.scalar(select(Delivery).where(Delivery.id == delivery_id).options(*_options())
                    .with_for_update(of=Delivery))


def _free_rider_slot(s: Session, rider_id: int) -> None:
    res = s.execute(text("UPDATE riders SET active_count = active_count - 1 WHERE id = :id AND active_count > 0"),
                    {"id": rider_id})
    if res.rowcount != 1:
        raise Abort(fail(ErrorCode.INTERNAL_ERROR, f"Rider {rider_id} has no active delivery to release."))


def _rider_candidates(s: Session, area: Optional[str], today: date) -> list[int]:
    return list(s.execute(text(
        "SELECT r.id FROM riders r "
        "WHERE r.is_on_duty AND r.active_count < r.capacity "
        "ORDER BY (r.area = :area) DESC, "
        "(SELECT count(*) FROM deliveries d WHERE d.rider_id = r.id AND d.scheduled_date = :today), r.id"),
        {"area": area or "", "today": today}).scalars())


def _claim(s: Session, rider_id: int) -> bool:
    res = s.execute(text("UPDATE riders SET active_count = active_count + 1 "
                         "WHERE id = :id AND is_on_duty AND active_count < capacity"), {"id": rider_id})
    return res.rowcount == 1


class DispatchInput(BaseModel):
    today: Optional[date] = None


class DispatchOutput(ToolOk):
    assigned: list[DeliverySummary]
    still_waiting_today: int


@tool(DispatchInput)
def run_dispatch(p: DispatchInput) -> Union[DispatchOutput, ToolError]:
    today = p.today or today_pkt()
    assigned: list[DeliverySummary] = []
    with session_scope() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext('dispatch'))"))
        queue = list(s.scalars(select(Delivery).where(Delivery.status == "queued", Delivery.scheduled_date <= today)
                               .options(*_options()).order_by(Delivery.scheduled_date, Delivery.queued_at,
                                                              Delivery.id)))
        waiting = len(queue)
        for d in queue:
            rider_id = next((r for r in _rider_candidates(s, d.order.customer.area, today) if _claim(s, r)), None)
            if rider_id is None:
                break
            d.status, d.rider_id, d.assigned_at = "assigned", rider_id, _now()
            s.flush()
            s.refresh(d, ["rider"])
            tell_customer(s, d.order, order_thread(s, d.order), MESSAGES["assigned"], rider=d.rider.name,
                          phone=d.rider.phone)
            assigned.append(_summary(d))
            waiting -= 1
    if waiting:
        notify_staff("warehouse", f"{waiting} paid order(s) for today are waiting: every rider on duty is full. "
                                  f"They will be assigned as soon as a rider finishes a delivery.")
    return DispatchOutput(assigned=assigned, still_waiting_today=waiting)


class RiderStepInput(BaseModel):
    delivery_id: int = Field(gt=0)
    rider_id: int = Field(gt=0)


class DeliveryOutput(ToolOk):
    delivery: DeliverySummary


def _rider_owned(s: Session, p: RiderStepInput, allowed: tuple[str, ...]) -> Union[Delivery, ToolError]:
    d = _load(s, p.delivery_id)
    if d is None:
        return fail(ErrorCode.DELIVERY_NOT_FOUND, f"Delivery {p.delivery_id} not found.")
    if d.rider_id != p.rider_id:
        return fail(ErrorCode.NOT_YOUR_DELIVERY, f"Delivery {p.delivery_id} is not assigned to rider {p.rider_id}.")
    if d.status not in allowed:
        return fail(ErrorCode.INVALID_TRANSITION,
                    f"Delivery {p.delivery_id} is '{d.status}'; it must be {' or '.join(allowed)}.", status=d.status)
    return d


@tool(RiderStepInput)
def pickup_delivery(p: RiderStepInput) -> Union[DeliveryOutput, ToolError]:
    with session_scope() as s:
        d = _rider_owned(s, p, ("assigned",))
        if isinstance(d, ToolError):
            return d
        d.status, d.picked_up_at = "out_for_delivery", _now()
        d.order.status = "out_for_delivery"
        tell_customer(s, d.order, order_thread(s, d.order), MESSAGES["picked_up"], rider=d.rider.name,
                      phone=d.rider.phone)
        return DeliveryOutput(delivery=_summary(d))


@tool(RiderStepInput)
def complete_delivery(p: RiderStepInput) -> Union[DeliveryOutput, ToolError]:
    try:
        with session_scope() as s:
            d = _rider_owned(s, p, ("out_for_delivery",))
            if isinstance(d, ToolError):
                return d
            release_reservation(s, d.order, shipped=True)
            d.status, d.delivered_at = "delivered", _now()
            d.order.status = "delivered"
            _free_rider_slot(s, p.rider_id)
            tell_customer(s, d.order, order_thread(s, d.order), MESSAGES["delivered"])
            summary = _summary(d)
    except Abort as a:
        return a.error
    run_dispatch()
    return DeliveryOutput(delivery=summary)


class FailInput(RiderStepInput):
    reason: str = Field(min_length=1, max_length=300)


class FailOutput(ToolOk):
    delivery: DeliverySummary
    retry_on: Optional[date]
    returned: bool


@tool(FailInput)
def fail_delivery(p: FailInput) -> Union[FailOutput, ToolError]:
    retry_on: Optional[date] = None
    try:
        with session_scope() as s:
            d = _rider_owned(s, p, ("out_for_delivery",))
            if isinstance(d, ToolError):
                return d
            d.status, d.failed_at, d.failure_reason = "failed", _now(), p.reason
            _free_rider_slot(s, p.rider_id)
            order, thread = d.order, order_thread(s, d.order)
            if d.attempt < MAX_ATTEMPTS:
                retry_on = d.scheduled_date + timedelta(days=1)
                order.status, order.delivery_date = "ready_to_dispatch", retry_on
                s.add(Delivery(order_id=order.id, attempt=d.attempt + 1, status="queued", scheduled_date=retry_on))
                tell_customer(s, order, thread, MESSAGES["retry"], reason=p.reason, day=f"{retry_on:%d %b}")
            else:
                release_reservation(s, order)
                order.status, order.cancel_reason = "returned", f"delivery failed twice: {p.reason}"
                tell_customer(s, order, thread, MESSAGES["returned"], amount=rs_text(order.total))
            s.flush()
            summary = _summary(d)
    except Abort as a:
        return a.error
    if retry_on is None:
        notify_staff("accounts", f"{summary.order_number} ({summary.customer_name}) could not be delivered twice and "
                                 f"is back in the warehouse. The customer paid in advance: arrange the refund.")
    run_dispatch()
    return FailOutput(delivery=summary, retry_on=retry_on, returned=retry_on is None)


class RiderSummary(BaseModel):
    rider_id: int
    name: str
    phone: str
    area: str
    capacity: int
    active_count: int
    is_on_duty: bool
    deliveries_today: int


class ListRidersInput(BaseModel):
    today: Optional[date] = None


class ListRidersOutput(ToolOk):
    riders: list[RiderSummary]


@tool(ListRidersInput)
def list_riders(p: ListRidersInput) -> Union[ListRidersOutput, ToolError]:
    today = p.today or today_pkt()
    with session_scope() as s:
        counts = dict(s.execute(select(Delivery.rider_id, func.count()).where(Delivery.scheduled_date == today,
                                                                             Delivery.rider_id.is_not(None))
                                .group_by(Delivery.rider_id)).all())
        rows = [RiderSummary(rider_id=r.id, name=r.name, phone=r.phone, area=r.area, capacity=r.capacity,
                             active_count=r.active_count, is_on_duty=r.is_on_duty,
                             deliveries_today=counts.get(r.id, 0))
                for r in s.scalars(select(Rider).order_by(Rider.id))]
    return ListRidersOutput(riders=rows)


class DutyInput(BaseModel):
    rider_id: int = Field(gt=0)
    on_duty: bool


class DutyOutput(ToolOk):
    rider_id: int
    is_on_duty: bool
    assigned_now: list[DeliverySummary]


@tool(DutyInput)
def set_rider_duty(p: DutyInput) -> Union[DutyOutput, ToolError]:
    with session_scope() as s:
        rider = s.get(Rider, p.rider_id, with_for_update=True)
        if rider is None:
            return fail(ErrorCode.RIDER_NOT_FOUND, f"Rider {p.rider_id} not found.")
        if not p.on_duty and rider.active_count > 0:
            return fail(ErrorCode.RIDER_BUSY, f"{rider.name} still has {rider.active_count} delivery(ies) to finish.")
        rider.is_on_duty = p.on_duty
    assigned = run_dispatch().assigned if p.on_duty else []
    return DutyOutput(rider_id=p.rider_id, is_on_duty=p.on_duty, assigned_now=assigned)


class ListDeliveriesInput(BaseModel):
    status: Optional[str] = None
    rider_id: Optional[int] = None
    scheduled_date: Optional[date] = None
    limit: int = Field(default=50, ge=1, le=200)


class ListDeliveriesOutput(ToolOk):
    count: int
    deliveries: list[DeliverySummary]


@tool(ListDeliveriesInput)
def list_deliveries(p: ListDeliveriesInput) -> Union[ListDeliveriesOutput, ToolError]:
    with session_scope() as s:
        q = select(Delivery).options(*_options()).order_by(Delivery.scheduled_date, Delivery.id).limit(p.limit)
        if p.status:
            q = q.where(Delivery.status == p.status)
        if p.rider_id:
            q = q.where(Delivery.rider_id == p.rider_id)
        if p.scheduled_date:
            q = q.where(Delivery.scheduled_date == p.scheduled_date)
        rows = [_summary(d) for d in s.scalars(q)]
    return ListDeliveriesOutput(count=len(rows), deliveries=rows)
