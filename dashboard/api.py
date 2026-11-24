from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import quote

import httpx
import streamlit as st

BASE_URL = os.getenv("BIZFLOW_API", "http://127.0.0.1:8000")

SHOPS = {
    "Ali Traders (Saddar, 2% discount)": "+923001234567",
    "Bismillah Store (Gulshan, 3% discount)": "+923215550192",
    "Karachi Mart (Tariq Road, no discount)": "+923452201876",
    "Madina General Store (Liaquatabad, 2% discount)": "+923124478210",
    "Unregistered number": "+923339876543",
}


class ApiError(Exception):
    pass


def _call(method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = httpx.request(method, BASE_URL + path, timeout=180, **kwargs)
    except httpx.HTTPError as exc:
        raise ApiError(f"Cannot reach the BizFlow API at {BASE_URL} ({exc}). Is uvicorn running?") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        if isinstance(detail, dict):
            detail = f"{detail.get('error_code')}: {detail.get('message')}"
        raise ApiError(f"HTTP {response.status_code}: {detail}")
    return response.json()


def get(path: str, **params: Any) -> Any:
    return _call("GET", path, params={k: v for k, v in params.items() if v is not None})


def post(path: str, body: Optional[dict] = None) -> Any:
    return _call("POST", path, json=body)


def phone_path(phone: str) -> str:
    return quote(phone, safe="")


def rs(value: Any) -> str:
    if value is None:
        return "-"
    amount = Decimal(str(value))
    return f"Rs {amount:,.0f}" if amount == amount.to_integral_value() else f"Rs {amount:,.2f}"


def run(label: str, fn, *args: Any, **kwargs: Any) -> Optional[Any]:
    try:
        result = fn(*args, **kwargs)
    except ApiError as exc:
        st.error(f"{label} failed. {exc}")
        return None
    st.toast(f"{label}: done")
    return result


def api_online() -> bool:
    try:
        get("/health")
    except ApiError as exc:
        st.error(str(exc), icon=":material/cloud_off:")
        return False
    st.caption(f":material/cloud_done: API online at {BASE_URL}")
    return True
