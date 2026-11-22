"""Chat with the order agent from the terminal.

    python -m app.cli --phone "+92 300 1234567"                 # interactive
    python -m app.cli --phone "+92 300 1234567" -m "Send 10 cartons of Dalda oil" -m "yes"
    python -m app.cli --phone "+92 300 1234567" --reset-thread  # forget this conversation first
    python -m app.cli --phone "+92 300 1234567" --test-db --seed-reset   # on the Neon test branch, fresh data

Uses Groq (GROQ_API_KEY) and the DATABASE_URL in backend/.env.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from app import config  # noqa: F401  (loads backend/.env into os.environ)
from app.agent.llm import GroqLLM
from app.agent.service import OrderAgent, postgres_checkpointer


def _print_reply(reply) -> None:
    if not reply.messages:
        print("bot> (no reply)")
    for m in reply.messages:
        print("bot> " + m.replace("\n", "\n     "))
    meta = [f"status={reply.status}"]
    if reply.waiting_for:
        meta.append(f"waiting_for={reply.waiting_for}")
    if reply.order_number:
        meta.append(f"order={reply.order_number}")
    if reply.invoice_number:
        meta.append(f"invoice={reply.invoice_number}")
    print("     [" + ", ".join(meta) + "]\n")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Urdu on Windows consoles
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", required=True, help="customer phone, e.g. '+92 300 1234567'")
    parser.add_argument("-m", "--message", action="append", help="send this message (repeatable), then exit")
    parser.add_argument("--reset-thread", action="store_true", help="forget this phone's conversation state first")
    parser.add_argument("--model", help="override GROQ_MODEL")
    parser.add_argument("--test-db", action="store_true", help="use TEST_DATABASE_URL (Neon test branch)")
    parser.add_argument("--seed-reset", action="store_true",
                        help="with --test-db: reset orders/stock/conversations on the test branch first")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if args.test_db:  # must happen before the first DB connection (engines are created lazily)
        test_url = os.getenv("TEST_DATABASE_URL")
        if not test_url:
            parser.error("TEST_DATABASE_URL is not set in backend/.env")
        os.environ["DATABASE_URL"] = test_url
        print("(using the Neon test branch - do not run pytest at the same time: tests reset this database)")
        if args.seed_reset:
            from app.db.seed import seed

            seed(reset=True)
    elif args.seed_reset:
        parser.error("--seed-reset is only allowed together with --test-db")

    with postgres_checkpointer() as saver:
        agent = OrderAgent(GroqLLM(model=args.model), saver)
        if args.reset_thread:
            agent.reset_thread(args.phone)
            print(f"(conversation state for {args.phone} cleared)")

        if args.message:
            for text in args.message:
                print(f"you> {text}")
                _print_reply(agent.handle_message(args.phone, text))
            return

        print(f"Chatting as {args.phone}. Type 'quit' to exit.\n")
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text.lower() in ("quit", "exit"):
                break
            if text:
                _print_reply(agent.handle_message(args.phone, text))


if __name__ == "__main__":
    main()
