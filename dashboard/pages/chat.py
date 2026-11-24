from __future__ import annotations

import json

import streamlit as st

from api import SHOPS, ApiError, get, phone_path, post

KIND_ICON = {"node": ":material/account_tree:", "llm": ":material/psychology:", "tool": ":material/build:",
             "guard": ":material/shield:"}
EXAMPLES = ["Send 10 cartons of Dalda oil and 5 cartons of Sooper biscuits", "20 bags rice",
            "bhai 10 carton dalda aur 5 sooper bhej do", "what do you have in water?", "yes",
            "paid via jazzcash, TID 784512", "where is my order?"]


def _llm_line(step: dict) -> str:
    out = step.get("output") or {}
    if step["step"] == "decide":
        calls = out.get("tool_calls") or []
        if calls:
            return "LLM decided to call " + ", ".join(f"`{c['name']}`" for c in calls)
        return "LLM answered in text"
    prompt = (step.get("input") or {}).get("prompt", "")
    if prompt.startswith("SUMMARY:"):
        return "LLM confirmation check: " + str(out.get("text", "")).strip()[:20]
    return "LLM wrote a reply"


def _line(step: dict) -> str:
    ok = "" if step["success"] else " :red[failed]"
    took = f" · {step['duration_ms']} ms" if step.get("duration_ms") is not None else ""
    if step["kind"] == "llm":
        text = _llm_line(step)
    elif step["kind"] == "tool":
        text = f"Tool `{step['step']}`"
    elif step["kind"] == "guard":
        text = f"Safety check `{step['step']}` " + (":green[passed]" if step["success"] else ":red[blocked]")
        ok = ""
    else:
        text = f"Graph step `{step['step']}`"
    return f"{KIND_ICON[step['kind']]} {text}{ok}{took}"


def show_trace(phone: str, run_id: str | None) -> None:
    try:
        trace = get(f"/conversations/{phone_path(phone)}/trace", run_id=run_id)
    except ApiError as exc:
        st.warning(str(exc))
        return
    steps = trace["steps"]
    if not steps:
        st.caption("Send a message to see what happens on the server.")
        return
    path = " → ".join(s["step"] for s in steps if s["kind"] == "node")
    c1, c2, c3 = st.columns(3)
    c1.metric("LLM calls", sum(s["kind"] == "llm" for s in steps))
    c2.metric("Tool calls", sum(s["kind"] == "tool" for s in steps))
    c3.metric("Safety checks", sum(s["kind"] == "guard" for s in steps))
    st.markdown(f"**Graph path:** {path}")
    show_nodes = st.toggle("Show graph steps", value=False)
    for step in steps:
        if step["kind"] == "node" and not show_nodes:
            continue
        with st.expander(_line(step)):
            if step.get("input"):
                st.markdown("**Input**")
                st.code(json.dumps(step["input"], indent=2, ensure_ascii=False), language="json")
            if step.get("output"):
                st.markdown("**Output**")
                st.code(json.dumps(step["output"], indent=2, ensure_ascii=False), language="json")


st.title("Chat with the order bot")
st.caption("This is what a shopkeeper sees on WhatsApp. On the right: what the server did for your last message.")

shop = st.sidebar.selectbox("Chat as", list(SHOPS), key="chat_shop")
phone = SHOPS[shop]
st.sidebar.code(phone)
with st.sidebar.expander("Try these messages"):
    for example in EXAMPLES:
        st.code(example, language=None)

chat_col, trace_col = st.columns([3, 2], gap="large")

with chat_col:
    try:
        convo = get(f"/conversations/{phone_path(phone)}", limit=60)
    except ApiError as exc:
        st.error(str(exc))
        st.stop()
    box = st.container(height=560, border=True)
    with box:
        if not convo["messages"]:
            st.caption("No messages yet. Say hello or send an order.")
        for m in convo["messages"]:
            with st.chat_message("user" if m["direction"] == "in" else "assistant",
                                 avatar=":material/storefront:" if m["direction"] == "in" else ":material/smart_toy:"):
                st.markdown(m["text"].replace("\n", "  \n"))
    if convo.get("waiting_for"):
        st.info(f"The bot is waiting for: **{convo['waiting_for'].replace('_', ' ')}**", icon=":material/hourglass_top:")

    message = st.chat_input("Type a message as this shop")
    if message:
        with st.spinner("The server is running the agent (LLM + tools)..."):
            try:
                reply = post("/chat", {"phone": phone, "message": message})
                st.session_state[f"run_{phone}"] = reply.get("run_id")
            except ApiError as exc:
                st.error(str(exc))
                st.stop()
        st.rerun()

with trace_col:
    st.subheader("Behind the scenes")
    st.caption("Customer page → FastAPI `/chat` → LangGraph agent → Groq LLM + tools → Postgres")
    show_trace(phone, st.session_state.get(f"run_{phone}"))
