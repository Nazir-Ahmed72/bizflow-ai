from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.staff import _ok
from app.tools import (
    complete_delivery, fail_delivery, list_deliveries, list_riders, pickup_delivery, run_dispatch, set_rider_duty,
)

staff_delivery_router = APIRouter(prefix="/staff", tags=["staff: delivery"])
riders_router = APIRouter(prefix="/riders", tags=["riders (rider app)"])


class DispatchBody(BaseModel):
    today: Optional[date] = None


class DutyBody(BaseModel):
    on_duty: bool


class FailBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300, examples=["shop closed"])


@staff_delivery_router.post("/dispatch")
def dispatch(body: Optional[DispatchBody] = None) -> dict:
    return _ok(run_dispatch(today=body.today if body else None))


@staff_delivery_router.get("/deliveries")
def deliveries(status: Optional[str] = None, scheduled_date: Optional[date] = None, limit: int = 50) -> dict:
    return _ok(list_deliveries(status=status, scheduled_date=scheduled_date, limit=min(max(limit, 1), 200)))


@staff_delivery_router.get("/riders")
def riders() -> dict:
    return _ok(list_riders())


@riders_router.get("/{rider_id}/deliveries")
def my_deliveries(rider_id: int) -> dict:
    result = _ok(list_deliveries(rider_id=rider_id, limit=200))
    result["deliveries"] = [d for d in result["deliveries"] if d["status"] in ("assigned", "out_for_delivery")]
    result["count"] = len(result["deliveries"])
    return result


@riders_router.post("/{rider_id}/duty")
def duty(rider_id: int, body: DutyBody) -> dict:
    return _ok(set_rider_duty(rider_id=rider_id, on_duty=body.on_duty))


@riders_router.post("/{rider_id}/deliveries/{delivery_id}/pickup")
def pickup(rider_id: int, delivery_id: int) -> dict:
    return _ok(pickup_delivery(delivery_id=delivery_id, rider_id=rider_id))


@riders_router.post("/{rider_id}/deliveries/{delivery_id}/delivered")
def delivered(rider_id: int, delivery_id: int) -> dict:
    return _ok(complete_delivery(delivery_id=delivery_id, rider_id=rider_id))


@riders_router.post("/{rider_id}/deliveries/{delivery_id}/failed")
def failed(rider_id: int, delivery_id: int, body: FailBody) -> dict:
    return _ok(fail_delivery(delivery_id=delivery_id, rider_id=rider_id, reason=body.reason))
