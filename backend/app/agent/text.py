"""Money formatting, the number guard, and BACKUP templates.

Replies are written by the LLM from facts. A reply is accepted only if every number in it
appears in the facts (number guard). If the LLM fails or the guard rejects its text, the
backup template for that message kind is sent instead.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

# --------------------------------------------------------------------------- formatting

MONEY_KEYS = {"unit_price", "line_total", "subtotal", "discount_amount", "total"}


def rs(amount: Any) -> str:
    d = Decimal(str(amount))
    return f"Rs {int(d):,}" if d == d.to_integral_value() else f"Rs {d:,.2f}"


def pct(value: Any) -> str:
    return f"{format(Decimal(str(value)).normalize(), 'f')}%"


def display(facts: Any) -> Any:
    """Copy of facts with money as 'Rs 5,400' and percentages as '2%' (what the LLM should copy)."""
    if isinstance(facts, dict):
        out = {}
        for k, v in facts.items():
            if k in MONEY_KEYS and v is not None:
                out[k] = rs(v)
            elif k == "discount_pct" and v is not None:
                out[k] = pct(v)
            else:
                out[k] = display(v)
        return out
    if isinstance(facts, list):
        return [display(v) for v in facts]
    return facts


_UNITS = {"en": {"carton": ("carton", "cartons"), "bag": ("bag", "bags")},
          "roman_ur": {"carton": ("carton", "carton"), "bag": ("bori", "boriyan")},
          "ur": {"carton": ("کارٹن", "کارٹن"), "bag": ("بوری", "بوریاں")}}


def unit_word(unit: str, qty: int, lang: str) -> str:
    singular, plural = _UNITS.get(lang, _UNITS["en"]).get(unit, (unit, unit + "s"))
    return singular if qty == 1 else plural


def stock_option_label(opt: dict, lang: str = "en") -> str:
    """One numbered stock option, e.g. 'Take 18 cartons Nestle Pure Life 1.5L + 12 cartons Aquafina 1.5L'."""
    lang = lang if lang in _UNITS else "en"
    k = opt["kind"]
    u = lambda q, unit: unit_word(unit, q, lang)  # noqa: E731
    if k == "partial":
        return {"en": f"Take {opt['qty']} {u(opt['qty'], opt['unit'])} {opt['name']} only",
                "roman_ur": f"Sirf {opt['qty']} {u(opt['qty'], opt['unit'])} {opt['name']} le lein",
                "ur": f"صرف {opt['qty']} {u(opt['qty'], opt['unit'])} {opt['name']} لے لیں"}[lang]
    if k == "substitute":
        both = (f"{opt['qty']} {u(opt['qty'], opt['unit'])} {opt['name']} + "
                f"{opt['alt_qty']} {u(opt['alt_qty'], opt['alt_unit'])} {opt['alt_name']}")
        return {"en": f"Take {both}", "roman_ur": f"{both} le lein", "ur": f"{both} لے لیں"}[lang]
    if k == "substitute_all":
        alt = f"{opt['alt_qty']} {u(opt['alt_qty'], opt['alt_unit'])} {opt['alt_name']}"
        return {"en": f"Take {alt} instead", "roman_ur": f"Is ki jagah {alt} le lein", "ur": f"اس کی جگہ {alt} لے لیں"}[lang]
    if k == "remove_item":
        return {"en": f"Remove {opt['name']} and keep the rest", "roman_ur": f"{opt['name']} hata dein, baqi rakhein",
                "ur": f"{opt['name']} ہٹا دیں، باقی رکھیں"}[lang]
    return {"en": "Cancel the order", "roman_ur": "Order cancel kar dein", "ur": "آرڈر منسوخ کر دیں"}[lang]


# --------------------------------------------------------------------------- what each reply must say

TASKS = {
    "summary": "Order summary: list each line (product, qty, unit price, line total), then subtotal and discount "
               "(skip both if discount is 0) and the total. Ask them to reply yes to confirm or no to cancel; "
               "say it is valid for 2 hours.",
    "product_choice": "Ask which product they mean for the phrase; list the numbered options (name, pack, price) "
                      "and ask them to reply with the number.",
    "stock_choice": "Say only 'available' units of the product are in stock (they asked for 'requested'); list the "
                    "numbered options exactly and ask them to reply with the number.",
    "confirmed": "Tell them the order is confirmed: order number, invoice number, total, delivery address. Thank them.",
    "cancelled": "Tell them the pending order has been cancelled and they can order again any time.",
    "unknown_customer": "Their number is not registered, so no order can be placed; the sales team has been informed "
                        "and will contact them.",
    "not_found": "Say these products could not be found and ask them to check the names and send again.",
    "no_pending": "There is no order waiting for confirmation; they can send a new order.",
    "changed": "Stock or prices changed since the last summary; an updated summary follows.",
    "expired": "The order summary expired (valid 2 hours); ask them to send the order again.",
    "cancel_handoff": "Confirmed orders cannot be cancelled by the bot; their cancellation request for the order has "
                      "been passed to the team, who will contact them.",
    "order_not_found": "No order with that number was found on their account.",
    "loose_units": "Explain that we are a wholesaler and sell only whole packs; for each item say how it is sold "
                   "('sold_as'). Offer to order whole packs instead. Do not quote any price.",
    "forwarded": "Say their question has been passed to our team, who will reply soon.",
    "nothing_to_cancel": "Say there is no pending order or question to cancel. If they want to cancel an order that "
                         "is already confirmed, they can say so (with the order number) and the team will handle it.",
    "error": "Something went wrong on our side; the team has been informed.",
}

# --------------------------------------------------------------------------- backup templates (English only)


def _summary(f: dict) -> str:
    lines = "\n".join(f"{i}. {l['name']} x {l['qty']} {unit_word(l['unit'], l['qty'], 'en')} @ "
                      f"{rs(l['unit_price'])} = {rs(l['line_total'])}" for i, l in enumerate(f["lines"], 1))
    money = (f"Subtotal: {rs(f['subtotal'])}\nDiscount ({pct(f['discount_pct'])}): -{rs(f['discount_amount'])}\n"
             if Decimal(str(f["discount_amount"])) > 0 else "")
    return (f"{f['customer_name']}, here is your order summary:\n{lines}\n{money}Total: {rs(f['total'])}\n"
            "Reply YES to confirm or NO to cancel. This summary is valid for 2 hours.")


def _numbered(items: Iterable[str]) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(items, 1))


BACKUP = {
    "summary": _summary,
    "product_choice": lambda f: (f"Which one do you mean by '{f['phrase']}' ({f['qty']})?\n" + _numbered(
        f"{o['name']} ({o['pack']}) - {rs(o['unit_price'])}" for o in f["options"]) + "\nReply with the number."),
    "stock_choice": lambda f: (
        (f"Sorry, {f['name']} is out of stock." if f["available"] <= 0 else
         f"Sorry, only {f['available']} of {f['name']} are available (you asked for {f['requested']}).")
        + "\n" + _numbered(o["label"] for o in f["options"]) + "\nReply with the number."),
    "confirmed": lambda f: (f"Thank you! Your order {f['order_number']} is confirmed. Invoice {f['invoice_number']}, "
                            f"total {rs(f['total'])}. Delivery to: {f['address']}."),
    "cancelled": lambda f: "Okay, your pending order has been cancelled. You can order again any time.",
    "unknown_customer": lambda f: ("This number is not registered with us, so I can't place an order. "
                                   "Our sales team has been informed and will contact you."),
    "not_found": lambda f: ("Sorry, I couldn't find: " + ", ".join(f"'{p}'" for p in f["phrases"])
                            + ". Please check the names and send again."),
    "no_pending": lambda f: "You don't have an order waiting for confirmation. Send your order any time.",
    "changed": lambda f: "Stock or prices changed since your last summary. Here is an updated one.",
    "expired": lambda f: "This order summary has expired (valid for 2 hours). Please send your order again.",
    "cancel_handoff": lambda f: (f"Confirmed orders can't be cancelled here. I've passed your request for "
                                 f"{f.get('order_number') or 'your order'} to our team; they will contact you."),
    "order_not_found": lambda f: f"I couldn't find order {f.get('order_number')} on your account.",
    "loose_units": lambda f: ("We are a wholesaler and sell only whole packs: " + "; ".join(
        f"{i['product']} is sold per {i['sold_as']}" for i in f["items"]) + ". Would you like whole packs instead?"),
    "forwarded": lambda f: "I've passed your question to our team; they will reply soon.",
    "nothing_to_cancel": lambda f: ("There is nothing pending to cancel. If you want to cancel a confirmed order, "
                                    "tell me the order number and our team will handle it."),
    "error": lambda f: "Sorry, something went wrong on our side. Our team has been informed.",
}


def backup(kind: str, facts: dict[str, Any]) -> str:
    return BACKUP[kind](facts)


# --------------------------------------------------------------------------- number guard

_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _ascii_digits(text: str) -> str:
    return "".join(str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch for ch in text)


def numbers_in(text: str) -> set[str]:
    out = set()
    for raw in _NUM.findall(_ascii_digits(text)):
        try:
            out.add(format(Decimal(raw.replace(",", "")).normalize(), "f"))  # "5,400.00" == "5400"
        except InvalidOperation:
            continue
    return out


def number_guard(allowed_text: str, candidate: str, required: Iterable[Any] = ()) -> tuple[bool, str]:
    """Candidate may only contain numbers found in allowed_text, and must contain every required one."""
    if not candidate or not candidate.strip():
        return False, "empty reply"
    if len(candidate) > 2500:
        return False, "reply too long"
    extra = numbers_in(candidate) - numbers_in(allowed_text)
    if extra:
        return False, f"numbers not backed by data: {sorted(extra)}"
    need = set().union(*(numbers_in(str(r)) for r in required)) if required else set()
    missing = need - numbers_in(candidate)
    if missing:
        return False, f"required numbers missing: {sorted(missing)}"
    return True, "ok"
