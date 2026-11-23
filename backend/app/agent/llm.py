"""Everything the LLM sees: the tool list, the prompts, and the Groq client.

The LLM decides what to do with each customer message:
  * READ-ONLY tools  -> it calls them itself and answers from the results
  * ACTION tools     -> it asks the agent to do something; the agent's graph nodes do the
                        actual database writes (draft, confirm, cancel, hand-off)
It never calculates: every number in a reply is checked against tool data (number guard).
Tests replace GroqLLM with a fake implementing the same two methods.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional, Protocol

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from pydantic import SecretStr

from app.tools import log_event

# --------------------------------------------------------------------------- tool specs

_ITEMS = {
    "type": "array",
    "items": {"type": "object", "properties": {"product_id": {"type": "string"}, "qty": {"type": "integer", "minimum": 1}},
              "required": ["product_id", "qty"]},
}
_LANGUAGE = {"type": "string", "enum": ["en", "ur", "roman_ur"],
             "description": "Language of the customer's message: en, ur (Urdu script) or roman_ur (Urdu in Latin letters)"}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                              "parameters": {"type": "object", "properties": properties,
                                                             "required": required}}}


READ_TOOLS = [
    _fn("list_products", "The whole catalog (or one category: oil, biscuits, rice, water, tea) with pack, price per "
        "pack and stock available. Use it for 'what do you have', 'what is available' etc.",
        {"category": {"type": ["string", "null"]}}, []),
    _fn("search_products", "Find products matching ONE product phrase (e.g. 'Dalda oil', 'rice', 'chai'). "
        "Returns product ids, names, pack and unit price.", {"query": {"type": "string"}}, ["query"]),
    _fn("check_stock", "Available stock for products (by product_id) and requested quantities.",
        {"items": _ITEMS}, ["items"]),
    _fn("suggest_alternatives", "In-stock substitutes for a product that is short.",
        {"product_id": {"type": "string"}, "qty_needed": {"type": "integer", "minimum": 1}},
        ["product_id", "qty_needed"]),
    _fn("get_prices", "Exact prices for this customer: line totals, subtotal, discount and total. "
        "Use this for ANY price question involving quantities.", {"items": _ITEMS}, ["items"]),
    _fn("get_restock_eta", "When a product will be back in stock: quantity on its way from a supplier and the "
        "expected number of days. Use it for 'when will X be available?'. Never guess dates.",
        {"product_id": {"type": "string"}}, ["product_id"]),
    _fn("get_order_history", "This customer's confirmed orders, newest first, or one order by number.",
        {"limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
         "order_number": {"type": ["string", "null"], "description": "e.g. ORD-000004"}}, []),
    _fn("get_order_status", "Where an order is now: payment status, where and before when to pay, delivery date, "
        "rider name and phone. "
        "Use it for 'has my payment arrived?', 'when will my order come?', 'how do I pay?'. Latest order if no number.",
        {"order_number": {"type": ["string", "null"], "description": "e.g. ORD-000004"}}, []),
]

ACTION_TOOLS = [
    _fn("place_order", "The customer wants to order products. Starts a new order (replaces any pending one).",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "product_phrase": {"type": "string", "description": "Product as named by the customer, in Latin letters "
                                                                 "(transliterate Urdu script), without quantity"},
            "qty": {"type": "integer", "minimum": 1, "description": "Quantity as written by the customer"},
            "unit": {"type": "string", "enum": ["pack", "loose"],
                     "description": "'pack' = whole cartons/bags (what we sell; also when no unit is given). "
                                    "'loose' = customer asks for single packets, bottles, pieces, kg or anything "
                                    "smaller than a whole carton/bag"}},
            "required": ["product_phrase", "qty", "unit"]}}, "language": _LANGUAGE},
        ["items", "language"]),
    _fn("choose_option", "The customer picked one of the numbered options of the PENDING question.",
        {"option_number": {"type": "integer", "minimum": 1}, "language": _LANGUAGE}, ["option_number", "language"]),
    _fn("confirm_pending_order", "The customer agrees / says yes (yes, ok, haan, ji, theek hai, bhej do...). "
        "Call it whenever the customer says yes, even if nothing seems pending.", {"language": _LANGUAGE},
        ["language"]),
    _fn("cancel_pending_order", "The customer says no / cancel to the PENDING order summary or question.",
        {"language": _LANGUAGE}, ["language"]),
    _fn("request_cancel_confirmed_order", "The customer wants to cancel an order that is ALREADY confirmed. "
        "A human will handle it.", {"order_number": {"type": ["string", "null"]}, "reason": {"type": "string"},
                                    "language": _LANGUAGE}, ["reason", "language"]),
    _fn("report_payment", "The customer says they have paid an order online and gives the transaction ID / "
        "reference. Our accounts team then verifies it. Do NOT call it without a transaction ID.",
        {"reference": {"type": "string", "description": "Transaction ID / reference exactly as the customer wrote it"},
         "method": {"type": ["string", "null"], "enum": ["bank_transfer", "jazzcash", "easypaisa", None]},
         "order_number": {"type": ["string", "null"], "description": "Only if the customer names one, e.g. ORD-000004"},
         "language": _LANGUAGE}, ["reference", "language"]),
    _fn("forward_to_staff", "Pass a question you cannot answer from the tools or BUSINESS FACTS (fees, policies, "
        "delivery times, complaints, special requests) to the team.",
        {"question": {"type": "string"}, "language": _LANGUAGE}, ["question", "language"]),
]

READ_TOOL_NAMES = {t["function"]["name"] for t in READ_TOOLS}
ACTION_TOOL_NAMES = {t["function"]["name"] for t in ACTION_TOOLS}

# --------------------------------------------------------------------------- prompts

AGENT_SYSTEM = """You are the WhatsApp order assistant of a grocery/FMCG distributor in Karachi, Pakistan.
You chat with shopkeepers in English, Urdu or Roman Urdu. Always reply in the language of the customer's
LATEST message (English -> English, Roman Urdu -> Roman Urdu, Urdu script -> Urdu script).

CUSTOMER: {customer}
{pending}
(The PENDING line above is always up to date. Earlier chat messages may be outdated: a summary shown earlier
may already be confirmed, cancelled or expired.)

MEMORY
{memory}
- When the customer says "it", "that", "a carton" etc. without naming a product, they mean CURRENTLY DISCUSSING.
- "My usual order" / "same as last time" means the items under CUSTOMER HISTORY: use place_order with them.

BUSINESS FACTS (the only policies you may state)
- We are a wholesale distributor. Every product is sold ONLY in whole packs (cartons or bags) as shown in its
  'pack'; prices are per pack. We do not sell single packets, bottles, pieces or loose kg.
- The customer's discount comes from their price category (shown above) and is applied by the system.
- Nothing is ordered until the customer says yes to an order summary; a summary is valid for 2 hours.
- Confirmed orders cannot be changed or cancelled by you; cancellation requests go to our team, who decide any
  fees. Orders are delivered to the customer's registered address.
- Payment is online and in advance only: bank transfer, JazzCash or Easypaisa (account details: get_order_status).
  No cash on delivery and no credit. After the customer says yes, the stock is reserved for 24 hours; an order
  without a reported payment by then is cancelled automatically.
- Our accounts team verifies every payment by hand, and the system messages the customer when it is verified.
  You never say a payment is received or verified yourself.
- Paid orders are delivered the same day if the payment is verified before 2 PM, otherwise the next day. On the
  delivery day a rider is assigned and the system sends the customer the rider's name and phone number.
- If nobody can receive the order, we try once more the next day. After a second failed attempt the order goes
  back to our warehouse and our team arranges the refund.

HOW TO WORK
- Use the read-only tools (list_products, search_products, check_stock, suggest_alternatives, get_prices,
  get_restock_eta, get_order_history, get_order_status) to answer questions. NEVER calculate prices, totals, discounts, counts or stock yourself:
  every number you write must come from a tool result or the conversation. For any total, call get_prices.
- Only state facts from tool results or BUSINESS FACTS. Never invent policies, fees, delivery times or offers;
  use forward_to_staff for anything else.
- To DO something, call exactly ONE action tool:
  place_order (customer wants products), choose_option (customer picks a numbered option of the pending
  question), confirm_pending_order (customer clearly says yes to the pending summary), cancel_pending_order
  (customer says no/cancel to what is pending), request_cancel_confirmed_order (customer wants to cancel an order
  that is already confirmed), report_payment (customer paid and sends the transaction ID; if they say they paid
  without an ID, ask for it), forward_to_staff (question you cannot answer).
- A message asking for different products or quantities is a new place_order, NOT a confirmation.
- You cannot create, change or cancel confirmed orders yourself, and you never say an order is placed or
  confirmed: the system does that and tells the customer.
- Otherwise answer briefly and helpfully. If something is pending, end with a one-line reminder of it.
- Plain text only (WhatsApp): no markdown, no ** or #."""

SUMMARY_SYSTEM = """You maintain a short memory of a WhatsApp chat between a shopkeeper and a distributor's order bot.
Merge the PREVIOUS SUMMARY with the NEW MESSAGES into an updated summary of at most 6 short lines, in English.
Keep: what the customer wanted, products discussed, decisions (confirmed / cancelled / handed to staff, with order
numbers), open questions and preferences (language, pack sizes). Drop greetings and small talk.
Do NOT include prices, totals or discounts (the system looks those up again). Plain text only."""

CONFIRM_CHECK_SYSTEM ="""You check one thing for an order system. An order summary was sent to a customer.
Does the customer's latest message clearly ACCEPT that summary exactly as it is (e.g. yes, ok, confirm, haan,
ji, theek hai, bhej do)? If the message asks for different products, quantities or units, asks a question,
or is unclear, the answer is NO. Reply with exactly one word: YES or NO."""

WRITE_SYSTEM = """You write one short WhatsApp message for a grocery distributor in Karachi.
Language: {language}. (en = English, roman_ur = Roman Urdu like 'aap ka order confirm ho gaya', ur = Urdu script,
auto = same language as the customer's message.)
Write the message described in TASK using only the FACTS (JSON). Copy every number, price, quantity, order or
invoice number and product name exactly as written in FACTS. Never calculate, round or invent numbers.
If FACTS has numbered options, list them with the same numbers. Plain text, no markdown. Reply with the message only."""


# --------------------------------------------------------------------------- interface + Groq

class OrderLLM(Protocol):
    def decide(self, system: str, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        """One chat turn with tools available: returns text and/or tool calls."""
        ...

    def write(self, system: str, prompt: str) -> str:
        """Plain text generation (used to phrase replies from facts)."""
        ...


class GroqLLM:
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        from langchain_groq import ChatGroq

        from app.config import GROQ_MODEL, groq_api_key

        self.model = model or GROQ_MODEL
        self._chat = ChatGroq(model=self.model, api_key=SecretStr(api_key or groq_api_key()), max_retries=2)

    def decide(self, system: str, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        return self._chat.bind_tools(tools, tool_choice="auto").invoke([SystemMessage(system), *messages])

    def write(self, system: str, prompt: str) -> str:
        return str(self._chat.invoke([SystemMessage(system), ("human", prompt)]).content).strip()


class LoggedLLM:
    """Wraps any OrderLLM; logs every call to workflow_logs (kind='llm') with timing."""

    def __init__(self, inner: OrderLLM):
        self.inner = inner

    def _log(self, step: str, started: float, inputs: dict, output: dict, ok: bool = True) -> None:
        log_event("llm", step, input=json.loads(json.dumps(inputs, default=str)),
                  output=json.loads(json.dumps(output, default=str)), success=ok,
                  error_code=None if ok else "LLM_ERROR", duration_ms=int((time.perf_counter() - started) * 1000))

    def decide(self, system: str, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        started = time.perf_counter()
        inputs = {"last_message": str(messages[-1].content)[:500] if messages else None, "turns": len(messages)}
        try:
            ai = self.inner.decide(system, messages, tools)
        except Exception as exc:
            self._log("decide", started, inputs, {"error": f"{type(exc).__name__}: {exc}"}, ok=False)
            raise
        self._log("decide", started, inputs, {"content": ai.content, "tool_calls": ai.tool_calls})
        return ai

    def write(self, system: str, prompt: str) -> str:
        started = time.perf_counter()
        try:
            text = self.inner.write(system, prompt)
        except Exception as exc:
            self._log("write", started, {"prompt": prompt[:1000]}, {"error": f"{type(exc).__name__}: {exc}"}, ok=False)
            raise
        self._log("write", started, {"prompt": prompt[:1000]}, {"text": text})
        return text


def tool_specs() -> list[dict]:
    return READ_TOOLS + ACTION_TOOLS


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
