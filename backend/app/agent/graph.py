# pyright: reportTypedDictNotRequiredAccess=false, reportOptionalSubscript=false, reportArgumentType=false
# pyright: reportAttributeAccessIssue=false
"""The order graph (LangGraph).

Every customer message goes to the `agent` node first. There the LLM reads the message and either
  * calls READ-ONLY tools and answers (questions: prices, stock, "list my orders", ...), or
  * calls an ACTION tool, and the graph runs the matching deterministic steps (the only DB writes):

  new message -> ingest -> identify_customer --unknown--> handoff -> END
                                  |  (+ loads long-term profile)
                                  v
                               memory  (summarises old messages when the chat gets long)
                                  |
                                  v
      +----------------------->  agent  <-----------------------------+   (prompt = pending + profile + summary +
      |                                                                |    focus + recent messages)
      |   (LLM decides)            |                                   |
      |      place_order -> resolve_products -> check_stock -> price -> save_draft -> send_summary
      |      choose_option -> apply_choice                                                |
      |      confirm_pending_order -> revalidate -> confirm -> invoice -> END             |
      |      cancel_pending_order -> cancel -> END                                        |
      |      request_cancel_confirmed_order -> cancel_handoff (human)                     |
      |      plain answer --------------------------------------------------------+       |
      |                                                                            v       v
      +------------------------ memory <-----------------------------------  wait_for_reply
                                                                       (interrupt: pause until the next message)

Core rules enforced here (not by the LLM): numbers only from tools (number guard on every reply);
a summary is a draft, never an order; revalidate before confirm; duplicate "yes" -> already_confirmed ->
no message; success is reported only from successful confirm_order + generate_invoice results.
"""
from __future__ import annotations

import json
import time
from typing import Annotated, Any, Callable, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.llm import (
    ACTION_TOOL_NAMES, AGENT_SYSTEM, CONFIRM_CHECK_SYSTEM, READ_TOOL_NAMES, SUMMARY_SYSTEM, WRITE_SYSTEM, OrderLLM,
    dumps, tool_specs,
)
from app.agent.text import TASKS, backup, display, number_guard, stock_option_label
from app.tools import (
    ErrorCode, cancel_draft, check_stock, confirm_order, create_backorders, create_draft, generate_invoice,
    get_customer_profile, get_order_history, get_prices, get_restock_eta, identify_customer, list_products,
    log_context, log_event, notify_staff, recent_messages, run_reorder_check, search_products, suggest_alternatives,
)

MAX_TOOL_ROUNDS = 5
SUMMARY_TRIGGER = 16
KEEP_RECENT = 6
MAX_FOCUS = 3


# --------------------------------------------------------------------------- state

def merge_outbox(old: Optional[list[str]], new: Optional[list[str]]) -> list[str]:
    """Append messages; None resets (start of a new turn)."""
    return [] if new is None else (old or []) + list(new)


class OrderState(TypedDict, total=False):
    thread_id: str
    message: str                    # latest customer text
    language: str                   # en | ur | roman_ur (from the LLM's action calls)
    customer: Optional[dict]
    requested: list[dict]           # [{phrase, qty, product_id?, name?, unit?, missing?}]
    items: list[dict]               # resolved lines [{product_id, name, unit, qty}]
    awaiting: Optional[str]         # product_choice | stock_choice | confirmation | None
    question: Optional[str]         # sent when the graph pauses (interrupt payload)
    base_question: Optional[str]
    options: list[dict]
    choice_index: Optional[int]
    choice: Optional[int]           # option number picked by the LLM's choose_option
    pricing: Optional[dict]
    draft: Optional[dict]
    last_confirmed: Optional[dict]  # survives turns, for a duplicate "yes"
    order: Optional[dict]
    invoice: Optional[dict]
    cancel_request: Optional[dict]  # args of request_cancel_confirmed_order
    summary: Optional[str]
    summarized_until: int
    focus: list[dict]
    profile: Optional[dict]
    backorders: list[dict]
    status: str
    outbox: Annotated[list[str], merge_outbox]


ORDER_RESET: dict[str, Any] = {
    "requested": [], "items": [], "awaiting": None, "question": None, "base_question": None, "options": [],
    "choice_index": None, "choice": None, "pricing": None, "draft": None, "order": None, "invoice": None,
    "cancel_request": None, "backorders": [],
}


# --------------------------------------------------------------------------- helpers

def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def node(name: str) -> Callable:
    """Run a node inside the thread's log context and log it (kind='node')."""

    def decorator(fn: Callable[[OrderState], Any]) -> Callable:
        # No functools.wraps: LangGraph inspects __wrapped__ and would then not pass `config`.
        def wrapper(state: OrderState, config: RunnableConfig) -> Any:
            cfg = (config or {}).get("configurable", {})
            with log_context(state.get("thread_id") or cfg.get("thread_id"), cfg.get("run_id")):
                started = time.perf_counter()
                try:
                    result = fn(state)
                except GraphBubbleUp:  # interrupt(): not an error
                    raise
                except Exception as exc:
                    log_event("node", name, input={"message": state.get("message")},
                              output={"error": f"{type(exc).__name__}: {exc}"}, success=False,
                              error_code="NODE_ERROR", duration_ms=int((time.perf_counter() - started) * 1000))
                    raise
                update = (result.update if isinstance(result, Command) else result) or {}
                log_event("node", name, input={"message": state.get("message"), "awaiting": state.get("awaiting")},
                          output=_jsonable({"goto": getattr(result, "goto", None), "keys": sorted(update),
                                            "outbox": update.get("outbox")}),
                          duration_ms=int((time.perf_counter() - started) * 1000))
                return result

        wrapper.__name__ = name
        return wrapper

    return decorator


def guard(step: str, ok: bool, **details: Any) -> bool:
    log_event("guard", step, output=_jsonable(details) or None, success=ok, error_code=None if ok else "GUARD_FAILED")
    return ok


def _merge_lines(lines: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for line in lines:
        if line["qty"] > 0:
            if line["product_id"] in merged:
                merged[line["product_id"]]["qty"] += line["qty"]
            else:
                merged[line["product_id"]] = dict(line)
    return list(merged.values())


def _tool_lines(items: list[dict]) -> list[dict]:
    return [{"product_id": i["product_id"], "qty": i["qty"]} for i in items]


def _pending_text(state: OrderState) -> str:
    awaiting = state.get("awaiting")
    if awaiting == "confirmation" and state.get("draft"):
        d = display(state["draft"])
        lines = "; ".join(f"{l['name']} x {l['qty']} = {l['line_total']}" for l in d["lines"])
        return (f"PENDING: an order summary is waiting for the customer's yes/no. Lines: {lines}. Total {d['total']}.\n"
                f"If you answer a question instead, end with a short reminder that this summary (total {d['total']}) "
                "is waiting for yes or no.")
    if awaiting in ("product_choice", "stock_choice"):
        return (f"PENDING: you asked the customer to choose a numbered option:\n{state.get('base_question')}\n"
                "If they pick one, call choose_option with its number. If you answer a question instead, end with "
                "a short reminder to reply with an option number.")
    if state.get("last_confirmed"):
        return ("PENDING: nothing. The customer's last order was just confirmed. If they say yes/ok/haan again, "
                "you MUST call confirm_pending_order (the system silently ignores duplicates); do not reply in text.")
    return "PENDING: nothing."


def _memory_text(state: OrderState) -> str:
    parts = []
    profile = state.get("profile")
    if profile and profile.get("orders_count"):
        usual = "; ".join(f"{i['name']} ({i['product_id']}, last time {i['last_qty']}, ordered {i['times_ordered']} times)"
                          for i in profile["usual_items"])
        parts.append(f"CUSTOMER HISTORY: {profile['orders_count']} past orders, last {profile['last_order_number']}. "
                     f"Usually orders: {usual}.")
        if profile.get("preferred_language"):
            parts.append(f"Language used last time: {profile['preferred_language']}.")
    else:
        parts.append("CUSTOMER HISTORY: no past orders.")
    if state.get("summary"):
        parts.append(f"CONVERSATION SUMMARY (older messages):\n{state['summary']}")
    focus = state.get("focus") or []
    if focus:
        shown = display(focus)
        parts.append("CURRENTLY DISCUSSING: " + "; ".join(
            f"{f['name']} ({f['product_id']}" + (f", {f['pack']}" if f.get("pack") else "")
            + (f", {f['unit_price']} per pack" if f.get("unit_price") else "") + ")" for f in shown))
    return "\n".join(parts)


def _focus_from(tool_name: str, result: dict) -> list[dict]:
    if not result.get("success"):
        return []
    keep = ("product_id", "name", "pack", "unit_price")
    if tool_name == "search_products" and len(result.get("matches", [])) <= MAX_FOCUS:
        rows = result["matches"]
    elif tool_name == "list_products" and result.get("count", 0) <= MAX_FOCUS:
        rows = result["products"]
    elif tool_name == "get_prices":
        rows = result["lines"]
    elif tool_name == "check_stock":
        rows = result["items"]
    elif tool_name == "suggest_alternatives":
        rows = [{"product_id": result["product_id"], "name": result["name"]}, *result["alternatives"]]
    else:
        rows = []
    return [{k: r[k] for k in keep if k in r} for r in rows]


def _merge_focus(old: list[dict], new: list[dict]) -> list[dict]:
    merged = {f["product_id"]: f for f in old}
    for f in new:
        merged[f["product_id"]] = {**merged.get(f["product_id"], {}), **f}
    ordered = [merged[f["product_id"]] for f in new] + [f for pid, f in merged.items()
                                                       if pid not in {n["product_id"] for n in new}]
    return ordered[:MAX_FOCUS]


def clean_text(text: str) -> str:
    """WhatsApp plain text: drop markdown emphasis/headings the model sometimes adds."""
    text = text.replace("**", "").replace("__", "")
    return "\n".join(line.lstrip("#").lstrip() if line.lstrip().startswith("#") else line
                     for line in text.splitlines()).strip()


# --------------------------------------------------------------------------- graph

def build_graph(llm: OrderLLM, checkpointer=None):

    # ----------------------------------------------------------------- replies

    def say(state: OrderState, kind: str, facts: Optional[dict] = None, required: tuple = ()) -> str:
        """LLM writes the message from facts; number guard; backup template if rejected."""
        facts = facts or {}
        shown = display(facts)
        lang = state.get("language") or "auto"
        prompt = (f"TASK: {TASKS[kind]}\nCUSTOMER'S LATEST MESSAGE: {state.get('message', '')}\n"
                  f"FACTS: {dumps(shown)}")
        fallback = backup(kind, facts)
        try:
            text = clean_text(llm.write(WRITE_SYSTEM.format(language=lang), prompt))
        except Exception as exc:
            guard("reply_numbers", False, kind=kind, reason=f"LLM error: {exc}")
            return fallback
        ok, reason = number_guard(dumps(shown) + " " + fallback, text, required)
        guard("reply_numbers", ok, kind=kind, reason=reason)
        return text if ok else fallback

    def ask(awaiting: str, question: str, **extra: Any) -> Command:
        return Command(update={"awaiting": awaiting, "question": question, "base_question": question, **extra},
                       goto="wait_for_reply")

    def finish(state: OrderState, text: Optional[str], status: str, **extra: Any) -> Command:
        update = {"status": status, **extra}
        if text:
            update["outbox"] = [text]
        return Command(update=update, goto=END)

    def fail(state: OrderState, reason: str) -> Command:
        notify_staff("manager", f"Order bot error on {state.get('thread_id')}: {reason}")
        return finish(state, say(state, "error"), "error", **ORDER_RESET)

    def keep_waiting_or_end(state: OrderState, text: Optional[str], status: str) -> Command:
        """After answering: if an order is still pending, pause again (without resending the question)."""
        update: dict[str, Any] = {"status": status}
        if text:
            update["outbox"] = [text]
        if state.get("awaiting"):
            return Command(update={**update, "question": None}, goto="wait_for_reply")
        return Command(update=update, goto=END)

    # ----------------------------------------------------------------- entry

    @node("ingest")
    def ingest(state: OrderState):
        return Command(update={**ORDER_RESET, "status": "active", "outbox": None}, goto="identify_customer")

    @node("identify_customer")
    def identify(state: OrderState):
        res = identify_customer(state["thread_id"])
        if not res.success:
            return Command(update={"customer": None}, goto="handoff")
        profile = get_customer_profile(res.customer_id)
        return Command(update={"customer": {"customer_id": res.customer_id, "name": res.name, "address": res.address,
                                            "price_category": res.price_category,
                                            "discount_pct": str(res.discount_pct)},
                               "profile": profile.model_dump(mode="json") if profile.success else None},
                       goto="memory")

    @node("memory")
    def memory(state: OrderState):
        after = state.get("summarized_until") or 0
        rows = recent_messages(state["thread_id"], limit=200, after_id=after)
        if len(rows) <= SUMMARY_TRIGGER:
            return Command(goto="agent")
        old = rows[:-KEEP_RECENT]
        transcript = "\n".join(f"{'Customer' if m['direction'] == 'in' else 'Bot'}: {m['text']}" for m in old)
        previous = state.get("summary") or ""
        try:
            summary = clean_text(llm.write(SUMMARY_SYSTEM,
                                           f"PREVIOUS SUMMARY:\n{previous or '(none)'}\n\nNEW MESSAGES:\n{transcript}"))
        except Exception as exc:
            guard("summary_written", False, reason=str(exc))
            return Command(goto="agent")
        ok, reason = number_guard(previous + " " + transcript, summary)
        if not guard("summary_numbers", ok, reason=reason, summarized=len(old)):
            return Command(goto="agent")
        return Command(update={"summary": summary, "summarized_until": old[-1]["id"]}, goto="agent")

    @node("handoff")
    def handoff(state: OrderState):
        notify_staff("sales", f"Unregistered number {state['thread_id']} wrote: {state['message']!r}")
        return finish(state, say(state, "unknown_customer"), "handoff")

    # ----------------------------------------------------------------- the LLM decides

    def run_read_tool(name: str, args: dict, customer_id: int) -> dict:
        try:
            if name == "list_products":
                res = list_products(category=args.get("category"))
            elif name == "search_products":
                res = search_products(query=args.get("query", ""))
            elif name == "check_stock":
                res = check_stock(items=args.get("items", []))
            elif name == "suggest_alternatives":
                res = suggest_alternatives(product_id=args.get("product_id", ""), qty_needed=args.get("qty_needed", 1))
            elif name == "get_restock_eta":
                res = get_restock_eta(product_id=args.get("product_id", ""))
            elif name == "get_prices":
                res = get_prices(customer_id=customer_id, items=args.get("items", []))
            else:  # get_order_history
                res = get_order_history(customer_id=customer_id, limit=args.get("limit") or 5,
                                        order_number=args.get("order_number"))
            return res.model_dump(mode="json")
        except Exception as exc:
            return {"success": False, "error_code": "BAD_TOOL_CALL", "message": str(exc)}

    @node("agent")
    def agent(state: OrderState):
        touched: list[dict] = []
        result = agent_turn(state, touched)
        if not touched:
            return result
        focus = _merge_focus(state.get("focus") or [], touched)
        return Command(update={**(result.update or {}), "focus": focus}, goto=result.goto)

    def agent_turn(state: OrderState, touched: list[dict]) -> Command:
        customer = state["customer"]
        system = AGENT_SYSTEM.format(
            customer=f"{customer['name']}, price category {customer['price_category']} "
                     f"({display({'discount_pct': customer['discount_pct']})['discount_pct']} discount), "
                     f"delivery address {customer['address']}",
            pending=_pending_text(state), memory=_memory_text(state))
        history = recent_messages(state["thread_id"], limit=SUMMARY_TRIGGER, after_id=state.get("summarized_until") or 0)
        msgs: list = [HumanMessage(m["text"]) if m["direction"] == "in" else AIMessage(m["text"]) for m in history]
        if not msgs or not isinstance(msgs[-1], HumanMessage):
            msgs.append(HumanMessage(state["message"]))
        evidence = [system, *(m["text"] for m in history)]
        retried = False

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                ai = llm.decide(system, msgs, tool_specs())
            except Exception:  # e.g. provider rejected a malformed tool call: one retry
                ai = llm.decide(system, msgs, tool_specs())
            calls = ai.tool_calls or []
            action = next((c for c in calls if c["name"] in ACTION_TOOL_NAMES), None)
            if action and not (action["name"] == "confirm_pending_order" and state.get("awaiting") == "confirmation"
                               and not confirmation_is_explicit(state)):
                return route_action(state, action["name"], action.get("args") or {})
            if action:  # confirmation rejected: tell the LLM and let it choose again
                msgs.append(ai)
                for c in calls:
                    content = ("REJECTED: the customer's message does not clearly accept the pending summary. "
                               "Do not confirm. If they want different products/quantities use place_order; "
                               "otherwise ask them to reply yes or no." if c is action else
                               dumps(run_read_tool(c["name"], c.get("args") or {}, customer["customer_id"])
                                     if c["name"] in READ_TOOL_NAMES else {"success": False}))
                    msgs.append(ToolMessage(content, tool_call_id=c["id"]))
                continue
            if calls:
                msgs.append(ai)
                for c in calls:
                    result = (run_read_tool(c["name"], c.get("args") or {}, customer["customer_id"])
                              if c["name"] in READ_TOOL_NAMES else
                              {"success": False, "error_code": "UNKNOWN_TOOL", "message": c["name"]})
                    evidence.append(dumps(display(result)) + " " + dumps(result))
                    touched.extend(_focus_from(c["name"], result))
                    msgs.append(ToolMessage(dumps(result), tool_call_id=c["id"]))
                continue

            text = clean_text(str(ai.content))
            ok, reason = number_guard(" ".join(evidence), text)
            guard("answer_numbers", ok, reason=reason)
            if ok:
                return keep_waiting_or_end(state, text, "answered")
            if retried:
                break
            retried = True
            msgs += [ai, HumanMessage(f"(system) Your reply has {reason}. Use tools (e.g. get_prices) for every "
                                      f"number, or leave the number out. Rewrite the reply.")]
        guard("agent_gave_up", False)
        return keep_waiting_or_end(state, say(state, "error"), "error")

    def confirmation_is_explicit(state: OrderState) -> bool:
        """Second, narrow LLM check before a write: does the message really accept THIS summary?"""
        d = display(state["draft"])
        lines = "; ".join(f"{l['name']} x {l['qty']}" for l in d["lines"])
        try:
            answer = llm.write(CONFIRM_CHECK_SYSTEM,
                               f"SUMMARY: {lines}; total {d['total']}\nCUSTOMER MESSAGE: {state['message']}")
        except Exception:
            answer = "NO"
        ok = answer.strip().upper().startswith("YES")
        guard("confirmation_is_explicit", ok, message=state["message"], answer=answer.strip()[:20])
        return ok

    def route_action(state: OrderState, name: str, args: dict) -> Command:
        lang = args.get("language") or state.get("language") or "en"
        base = {"language": lang}
        here = {**state, **base}
        if name == "place_order":
            requested = [{"phrase": str(i.get("product_phrase", "")).strip(), "qty": int(i.get("qty", 0)),
                          "unit": i.get("unit") or "pack"} for i in args.get("items", [])]
            loose = [r for r in requested if r["unit"] == "loose"]
            if not guard("whole_packs_only", not loose, loose=loose):
                facts = {"items": [{"product": r["phrase"], "qty": r["qty"],
                                    "sold_as": next((m.pack for m in search_products(r["phrase"]).matches), "carton")}
                                   for r in loose]}
                return keep_waiting_or_end(here, say(here, "loose_units", facts), "loose_units")
            valid = [r for r in requested if r["phrase"] and r["qty"] >= 1]
            guard("order_quantities_positive", bool(valid) and len(valid) == len(requested), requested=requested)
            if not valid:
                return keep_waiting_or_end(here, say(here, "not_found", {"phrases": [r["phrase"] for r in requested]}),
                                           "not_found")
            if state.get("draft"):
                cancel_draft(state["draft"]["draft_id"])  # a new order replaces the pending one
            return Command(update={**base, **ORDER_RESET, "requested": valid}, goto="resolve_products")
        if name == "forward_to_staff":
            notify_staff("sales", f"{state['customer']['name']} ({state['thread_id']}) asks: "
                                  f"{args.get('question') or state['message']}")
            return keep_waiting_or_end(here, say(here, "forwarded"), "forwarded")
        if name == "choose_option":
            if state.get("awaiting") in ("product_choice", "stock_choice"):
                return Command(update={**base, "choice": args.get("option_number")}, goto="apply_choice")
            return keep_waiting_or_end(state, say({**state, **base}, "no_pending"), "done")
        if name == "confirm_pending_order":
            if state.get("awaiting") == "confirmation":
                return Command(update=base, goto="revalidate")
            last = state.get("last_confirmed")
            if not state.get("awaiting") and last:  # a later "yes": idempotent, stays silent
                res = confirm_order(last["draft_id"], last["idempotency_key"])
                if res.success and res.already_confirmed:
                    guard("duplicate_confirmation_suppressed", True, order_number=res.order_number)
                    return finish(state, None, "duplicate_ignored")
            return keep_waiting_or_end(state, say({**state, **base}, "no_pending"), "done")
        if name == "cancel_pending_order":
            if state.get("awaiting") or state.get("draft"):
                return Command(update=base, goto="cancel")
            # Nothing pending. Never escalate on our own: a confirmed order is only handed to staff when the
            # customer explicitly asks (request_cancel_confirmed_order).
            guard("nothing_pending_to_cancel", True)
            return keep_waiting_or_end(here, say(here, "nothing_to_cancel"), "done")
        # request_cancel_confirmed_order
        return Command(update={**base, "cancel_request": args}, goto="cancel_handoff")

    # ----------------------------------------------------------------- order steps (deterministic)

    @node("resolve_products")
    def resolve_products(state: OrderState):
        requested = [dict(r) for r in state["requested"]]
        for idx, r in enumerate(requested):
            if r.get("product_id") or r.get("missing"):
                continue
            res = search_products(r["phrase"])
            if not res.success:
                return fail(state, f"search_products failed: {res.message}")
            if not res.matches:
                r["missing"] = True
            elif not res.ambiguous:
                m = res.matches[0]
                r.update(product_id=m.product_id, name=m.name, unit=m.unit)
            else:
                options = [m.model_dump(mode="json") for m in res.matches]
                facts = {"phrase": r["phrase"], "qty": r["qty"], "options": options}
                return ask("product_choice", say(state, "product_choice", facts), requested=requested,
                           options=options, choice_index=idx)
        missing = [r["phrase"] for r in requested if r.get("missing")]
        if missing:
            return finish(state, say(state, "not_found", {"phrases": missing}), "not_found", **ORDER_RESET)
        items = _merge_lines([{"product_id": r["product_id"], "name": r["name"], "unit": r["unit"], "qty": r["qty"]}
                              for r in requested])
        return Command(update={"requested": requested, "items": items}, goto="check_stock")

    @node("apply_choice")
    def apply_choice(state: OrderState):
        options, n = state["options"], state.get("choice")
        if not guard("option_in_range", isinstance(n, int) and 1 <= n <= len(options), option=n, options=len(options)):
            return Command(update={"question": state["base_question"], "choice": None}, goto="wait_for_reply")
        chosen = options[n - 1]
        if state["awaiting"] == "product_choice":
            requested = [dict(r) for r in state["requested"]]
            requested[state["choice_index"]].update(product_id=chosen["product_id"], name=chosen["name"],
                                                    unit=chosen["unit"])
            return Command(update={"requested": requested, "awaiting": None, "options": [], "choice": None},
                           goto="resolve_products")
        if chosen["kind"] == "cancel":
            return Command(goto="cancel")
        update = {"items": chosen["items_after"], "awaiting": None, "options": [], "choice": None}
        if chosen["kind"] == "backorder":
            kept = [b for b in state.get("backorders") or [] if b["product_id"] != chosen["backorder"]["product_id"]]
            update["backorders"] = kept + [chosen["backorder"]]
        return Command(update=update, goto="check_stock")

    @node("check_stock")
    def check_stock_node(state: OrderState):
        items = state["items"]
        res = check_stock(_tool_lines(items))
        if not res.success:
            return fail(state, f"check_stock failed: {res.message}")
        short = next((line for line in res.items if not line.sufficient), None)
        if short is None:
            return Command(goto="price")

        item = next(i for i in items if i["product_id"] == short.product_id)
        others = [i for i in items if i["product_id"] != short.product_id]
        alts = suggest_alternatives(short.product_id, short.shortfall if short.available > 0 else short.requested)
        alt = next((a for a in alts.alternatives if a.can_cover), None) if alts.success else None
        base = {"name": item["name"], "unit": item["unit"]}
        options: list[dict] = []
        if short.available > 0:
            options.append({**base, "kind": "partial", "qty": short.available,
                            "items_after": others + [{**item, "qty": short.available}]})
            eta = get_restock_eta(short.product_id)
            if eta.success and eta.incoming_qty > 0 and eta.eta_days is not None:
                options.append({**base, "kind": "backorder", "qty": short.available,
                                "backorder_qty": short.shortfall, "eta_days": eta.eta_days,
                                "items_after": others + [{**item, "qty": short.available}],
                                "backorder": {"product_id": item["product_id"], "name": item["name"],
                                              "unit": item["unit"], "qty": short.shortfall,
                                              "eta_days": eta.eta_days}})
        if alt is not None:
            alt_line = {"product_id": alt.product_id, "name": alt.name, "unit": item["unit"]}
            if short.available > 0:
                options.append({**base, "kind": "substitute", "qty": short.available, "alt_name": alt.name,
                                "alt_unit": item["unit"], "alt_qty": short.shortfall,
                                "items_after": _merge_lines(others + [{**item, "qty": short.available},
                                                                      {**alt_line, "qty": short.shortfall}])})
            else:
                options.append({**base, "kind": "substitute_all", "alt_name": alt.name, "alt_unit": item["unit"],
                                "alt_qty": short.requested,
                                "items_after": _merge_lines(others + [{**alt_line, "qty": short.requested}])})
        if others:
            options.append({**base, "kind": "remove_item", "items_after": others})
        options.append({"kind": "cancel", "items_after": []})
        for o in options:
            o["label"] = stock_option_label(o, state.get("language", "en"))

        facts = {"name": item["name"], "requested": short.requested, "available": short.available,
                 "options": [{"number": i, "label": o["label"]} for i, o in enumerate(options, 1)]}
        return ask("stock_choice", say(state, "stock_choice", facts), options=options)

    @node("price")
    def price(state: OrderState):
        res = get_prices(state["customer"]["customer_id"], _tool_lines(state["items"]))
        if not res.success:
            return fail(state, f"get_prices failed: {res.message}")
        return Command(update={"pricing": res.model_dump(mode="json")}, goto="save_draft")

    @node("save_draft")
    def save_draft(state: OrderState):
        res = create_draft(thread_id=state["thread_id"], customer_id=state["customer"]["customer_id"],
                           items=_tool_lines(state["items"]), language=state.get("language", "en"))
        if not res.success:
            return fail(state, f"create_draft failed: {res.message}")
        draft, pricing = res.model_dump(mode="json"), state["pricing"]
        same = all(draft[k] == pricing[k] for k in ("subtotal", "discount_amount", "total"))
        if not guard("draft_matches_prices", same, draft_total=draft["total"], priced_total=pricing["total"]):
            cancel_draft(res.draft_id)
            return fail(state, "draft total differs from get_prices total")
        focus = _merge_focus(state.get("focus") or [], _focus_from("get_prices", draft))
        return Command(update={"draft": draft, "focus": focus}, goto="send_summary")

    @node("send_summary")
    def send_summary(state: OrderState):
        d = state["draft"]
        facts = {"customer_name": state["customer"]["name"], "lines": d["lines"], "subtotal": d["subtotal"],
                 "discount_pct": d["discount_pct"], "discount_amount": d["discount_amount"], "total": d["total"]}
        if state.get("backorders"):
            facts["backorders"] = state["backorders"]
        text = say(state, "summary", facts, required=(d["total"],))
        return ask("confirmation", text)

    @node("wait_for_reply")
    def wait_for_reply(state: OrderState):
        reply = interrupt({"message": state.get("question"), "awaiting": state.get("awaiting")})
        return Command(update={"message": str(reply), "question": None}, goto="memory")

    @node("revalidate")
    def revalidate(state: OrderState):
        """Re-check stock and prices right before confirming."""
        items, draft = state["items"], state["draft"]
        stock = check_stock(_tool_lines(items))
        prices = get_prices(state["customer"]["customer_id"], _tool_lines(items))
        ok = stock.success and stock.all_available and prices.success and str(prices.total) == draft["total"]
        guard("revalidate_before_confirm", ok, draft_total=draft["total"],
              current_total=str(getattr(prices, "total", None)), all_available=getattr(stock, "all_available", None))
        if ok:
            return Command(goto="confirm")
        cancel_draft(draft["draft_id"])
        return Command(update={"draft": None, "outbox": [say(state, "changed")]}, goto="check_stock")

    @node("confirm")
    def confirm(state: OrderState):
        draft = state["draft"]
        res = confirm_order(draft["draft_id"], draft["idempotency_key"])
        last = {"draft_id": draft["draft_id"], "idempotency_key": draft["idempotency_key"]}
        if res.success and res.already_confirmed:
            guard("duplicate_confirmation_suppressed", True, order_number=res.order_number)
            return finish(state, None, "duplicate_ignored", last_confirmed=last, awaiting=None)
        if res.success:
            return Command(update={"order": res.model_dump(mode="json"), "last_confirmed": last, "awaiting": None},
                           goto="invoice")
        if res.error_code in (ErrorCode.INSUFFICIENT_STOCK, ErrorCode.PRICE_CHANGED):
            return Command(update={"draft": None, "outbox": [say(state, "changed")]}, goto="check_stock")
        if res.error_code in (ErrorCode.DRAFT_EXPIRED, ErrorCode.DRAFT_NOT_PENDING):
            return finish(state, say(state, "expired"), "expired", **ORDER_RESET)
        return fail(state, f"confirm_order failed: {res.error_code} {res.message}")

    @node("invoice")
    def invoice(state: OrderState):
        order = state["order"]
        res = generate_invoice(order["order_id"])
        if not res.success:
            notify_staff("manager", f"Invoice failed for {order['order_number']}: {res.message}")
        guard("success_confirmed_by_tools", order["success"] and not order["already_confirmed"],
              order_number=order["order_number"], invoice_ok=res.success)
        backorders = state.get("backorders") or []
        if backorders:
            create_backorders(customer_id=state["customer"]["customer_id"], order_id=order["order_id"],
                              thread_id=state["thread_id"],
                              items=[{"product_id": b["product_id"], "qty": b["qty"]} for b in backorders])
        run_reorder_check(reason=f"reorder point after {order['order_number']}",
                          product_ids=sorted({l["product_id"] for l in order["lines"]}
                                             | {b["product_id"] for b in backorders}))
        facts = {"order_number": order["order_number"], "invoice_number": res.invoice_number if res.success else "pending",
                 "total": order["total"], "address": state["customer"]["address"]}
        if backorders:
            facts["backorders"] = backorders
        text = say(state, "confirmed", facts, required=(order["total"], order["order_number"]))
        return finish(state, text, "confirmed", invoice=res.model_dump(mode="json") if res.success else None)

    @node("cancel")
    def cancel(state: OrderState):
        """Customer cancels the PENDING order: the agent does it."""
        if state.get("draft"):
            cancel_draft(state["draft"]["draft_id"])
        return finish(state, say(state, "cancelled"), "cancelled", **ORDER_RESET)

    @node("cancel_handoff")
    def cancel_handoff(state: OrderState):
        """Customer wants to cancel a CONFIRMED order: never automatic, a human handles it."""
        req = state.get("cancel_request") or {}
        customer = state["customer"]
        number = (req.get("order_number") or "").strip().upper() or None
        history = get_order_history(customer_id=customer["customer_id"], limit=1, order_number=number)
        if not history.success or not history.orders:
            kind = "order_not_found" if number else "no_pending"
            return keep_waiting_or_end(state, say(state, kind, {"order_number": number}), "done")
        order_number = history.orders[0].order_number
        notify_staff("sales", f"{customer['name']} ({state['thread_id']}) asks to cancel confirmed order "
                              f"{order_number}. Reason: {req.get('reason') or state.get('message')}")
        return keep_waiting_or_end(state, say(state, "cancel_handoff", {"order_number": order_number}), "handed_off")

    # ----------------------------------------------------------------- wiring

    g = StateGraph(OrderState)
    for fn in (ingest, identify, memory, handoff, agent, resolve_products, apply_choice, check_stock_node, price, save_draft,
               send_summary, wait_for_reply, revalidate, confirm, invoice, cancel, cancel_handoff):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "ingest")
    return g.compile(checkpointer=checkpointer)
