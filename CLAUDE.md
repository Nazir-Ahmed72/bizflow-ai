# BizFlow AI

Portfolio project: an AI order agent for an FMCG/grocery distributor in Karachi.
Customers send order messages. The agent identifies the customer, finds products,
checks stock, gets prices and asks for confirmation. Then it creates the order,
reserves stock and issues an invoice.

**Current phase: backend + agent orchestration only. No frontend.**

## Stack

- **Python 3.10** (the machine's existing 3.10.10; do not install other Pythons or system tools). Keep code 3.10-compatible: no `tomllib`, `except*`, `typing.Self`.
- FastAPI, SQLAlchemy 2.0, Alembic (migrations), Pydantic v2, pytest
- **PostgreSQL on Neon** (hosted, not local Docker). `DATABASE_URL` in `backend/.env`
  - Paste Neon's `postgresql://...?sslmode=require` as-is; `app/config.database_url()` rewrites it to `postgresql+psycopg://` (psycopg 3). psycopg2 is not installed.
  - Use Neon's **direct** host (not `-pooler`) for migrations and the LangGraph checkpointer
- LangGraph + `langgraph-checkpoint-postgres` (thread_id = customer phone)
- **LLM: Groq** via `langchain-groq` (NOT Anthropic; user's choice). Key `GROQ_API_KEY`, model `GROQ_MODEL` (default `openai/gpt-oss-120b`; other models on the key: `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`).
  - `app/config.groq_api_key()` / `GROQ_MODEL`. The LLM sits behind an interface so tests use a fake LLM with no key.
- venv at repo root: `.venv`. Dependencies in `backend/requirements.txt`

## Layout

```
.venv/
backend/
  requirements.txt
  .env                # not committed; copy from .env.example
  app/
    config.py         # settings from env
    phone.py          # normalize_phone -> E.164
    db/               # models.py, session.py (session_scope), seed.py
    tools/            # base.py (plumbing+shared models), catalog.py (read-only), orders.py, notify.py
    agent/            # llm.py (tool specs, prompts, Groq), graph.py, text.py (format, guard, backups), service.py
    cli.py            # python -m app.cli --phone "+92 300 1234567"
    api/main.py       # FastAPI: POST /chat, GET /conversations/{phone}, GET /health
  tests/
  alembic.ini
  migrations/         # Alembic; env.py reads DATABASE_URL via app.config
```

Run everything from `backend/` with the venv active.

## Core rules (non-negotiable)

1. **The LLM never calculates** prices, totals, discounts or stock. It only understands messages, picks tools and writes replies. All numbers come from deterministic tools.
2. **Agents never touch the database directly.** All DB access goes through the tools.
3. **No order exists until the customer confirms.** Before confirming, re-check stock and prices.
4. **`confirm_order` is idempotent**: it creates the order and reserves stock. The idempotency key is `"<draft_id>:confirm"`. A duplicate "yes" creates nothing and sends no second message.
5. **Atomic stock reservation**: one `UPDATE inventory SET reserved = reserved + :qty WHERE product_id = :pid AND physical - reserved >= :qty`, check the rowcount, all in the same transaction as order creation. Roll back everything if any row fails.
6. **Never report success until the tool result confirms it.**

## Tables (~10)

| Table | Notes |
|---|---|
| customers | phone (unique, normalised), name, price_category A/B/C, address, language pref |
| products | id like `P-001`, name, pack description, unit_price (Numeric) |
| price_lists | price_category → discount_pct (A 3%, B 2%, C 0%) |
| inventory | product_id, physical, reserved, reorder_level; available = physical − reserved |
| order_drafts | customer, items JSON, totals, status, `expires_at` = summary time + 2h |
| orders | draft_id, customer, totals, status (awaiting_payment / ready_to_dispatch / cancelled), `idempotency_key` UNIQUE, payment_due_at, paid_at, delivery_date, cancel_reason |
| order_items | order_id, product_id, qty, unit_price snapshot, line_total |
| invoices | order_id UNIQUE, invoice number, amounts |
| workflow_logs | every LLM call, tool call and guard check: thread_id, step, kind, input/output JSON, duration_ms, success |
| messages | thread_id, direction in/out, text, timestamp |
| staff_notifications | written by notify_staff: role, reason, thread_id, status open/resolved |

Implemented in `app/db/models.py`; initial migration `migrations/versions/2dd7357d1c41_initial_schema.py`.
Phones are stored normalised to E.164 (`+923001234567`) via `app/phone.normalize_phone`; thread_id uses the same form.
Products have `category` (oil/biscuits/rice/water/tea) and `search_terms` (incl. Roman Urdu like "chawal", "pani", "chai") for search_products / suggest_alternatives.
Status columns are strings with CHECK constraints (not PG enums). Alembic `env.py` ignores tables not in our metadata (LangGraph checkpoint tables live in the same DB).

Constraints: `physical >= 0`, `reserved >= 0`, `reserved <= physical`, qty > 0, FKs everywhere, unique idempotency key, unique customer phone. Money is `Numeric(12,2)` and Python `Decimal`, never float.

## The tools

Plain Python functions with Pydantic input/output, testable without AI. Errors return `{success: false, error_code, message}`.
The 8 from the spec plus `get_order_history` (read-only) and the draft helpers.

| Tool | File | Access |
|---|---|---|
| `identify_customer(phone)` | catalog.py | read (agent calls it on every new turn) |
| `list_products(category?)` | catalog.py | **read, LLM may call** (whole catalog + stock) |
| `search_products(query)` | catalog.py | **read, LLM may call** |
| `check_stock(items)` | catalog.py | **read, LLM may call** |
| `suggest_alternatives(product_id, qty_needed)` | catalog.py | **read, LLM may call** |
| `get_prices(customer_id, items)` → subtotal, discount, total | catalog.py | **read, LLM may call** (customer_id injected by agent) |
| `get_order_history(customer_id, limit, order_number)` | orders.py | **read, LLM may call** (customer_id injected; returns `count`) |
| `get_order_status(customer_id, order_number?)` | payments.py | **read, LLM may call** (status, payment status, pay_to, pay_before, delivery date, `meaning`) |
| `submit_payment` | payments.py | write, graph node `payment` only |
| `create_draft`, `cancel_draft` | orders.py | write, graph nodes only |
| `confirm_order(draft_id, idempotency_key)` | orders.py | write, graph nodes only |
| `generate_invoice(order_id)` | orders.py | write, graph nodes only |
| `notify_staff(role, reason)` | notify.py | write, graph nodes only |

Implementation (`app/tools/`):
- Each tool is wrapped by `@tool(InputModel)` (`base.py`): validates kwargs/positional args, never raises (crash → `INTERNAL_ERROR`), logs to workflow_logs (kind=`tool`, input, output, duration_ms). Results are `ToolOk` subclasses (`success=True`) or `ToolError` (`success=False, error_code, message, details`). Error codes live in `base.ErrorCode`.
- `log_context(thread_id, run_id)` tags every log written inside it; `log_event(kind, step, ...)` is what the agent uses for `node`/`llm`/`guard` logs (agents don't touch the DB themselves). `record_message` / `recent_messages` (notify.py) keep the transcript.
- `catalog.compute_pricing()` is the ONE place money is calculated (discount half-up to 0.01). get_prices, create_draft and confirm_order all use it.
- `search_products` takes ONE product phrase; returns best-scoring matches + `ambiguous` flag; no match = success with empty list.
- `create_draft` prices itself, sets 2h expiry, supersedes the customer's older pending drafts.
- `confirm_order`: one transaction: check idempotency key format → existing order? return `already_confirmed=True` (agent must send no message) → lock draft FOR UPDATE → re-check existing → pending? not expired (marks `expired`)? → re-price and compare (`PRICE_CHANGED`) → guarded UPDATE per line, sorted by product_id, rowcount must be 1 (`INSUFFICIENT_STOCK`) → insert order (`ORD-000001`) + items → draft `confirmed`. Failures raise `Abort` so everything rolls back.
- `generate_invoice` is idempotent (`INV-<order id>`; returns `already_existed=True`).

## Seed data (`app/db/seed.py`, fixed values, re-runnable)

Customers:
| Name | Phone | Cat | Address |
|---|---|---|---|
| Ali Traders | +92 300 1234567 | B (2%) | Shop 4 Saddar |
| Bismillah Store | +92 321 5550192 | A (3%) | Plot 12 Gulshan Block 5 |
| Karachi Mart | +92 345 2201876 | C (0%) | Main Tariq Road |
| Madina General Store | +92 312 4478210 | B (2%) | Liaquatabad No. 10 |

Products:
| ID | Name | Pack | Unit price Rs | Physical | Reorder |
|---|---|---|---|---|---|
| P-001 | Dalda Cooking Oil 1L | carton of 12 | 5400 | 140 | 40 |
| P-002 | Sooper Classic biscuits | carton of 24 | 1900 | 95 | 30 |
| P-003 | Basmati Super Kernel 5kg | bag | 2450 | 60 | 20 |
| P-004 | Sella Rice 5kg | bag | 2200 | 42 | 20 |
| P-005 | Broken Rice 25kg | bag | 6500 | 30 | 10 |
| P-006 | Nestle Pure Life 1.5L | carton of 6 | 720 | 18 | 40 |
| P-007 | Aquafina 1.5L | carton of 6 | 690 | 64 | 30 |
| P-008 | Tapal Danedar 950g | carton of 12 | 14000 | 8 | 20 |

## Agent (LangGraph, one order graph, no supervisor yet)

**Design (user's choice): the LLM decides, the graph writes.** Every customer message goes to the `agent` node: the LLM sees the customer, what is pending, the last 12 messages and the tools, and either
- calls **read-only tools** itself (search_products, check_stock, suggest_alternatives, get_prices, get_order_history) and answers in free text, or
- calls ONE **action tool**, which routes to deterministic graph nodes (the only DB writes):
  - `place_order(items[{product_phrase, qty, unit: pack|loose}], language)` → if any `loose` (single packets/bottles): refused in code (`whole_packs_only` guard, reply `loose_units`); else resolve_products (ask if several variants) → check_stock (short: partial / substitute / remove / cancel) → price → save_draft → send_summary → wait
  - `choose_option(option_number)` → apply_choice
  - `confirm_pending_order` → **confirmation check** (second narrow LLM call, `CONFIRM_CHECK_SYSTEM`: does the message clearly accept THIS summary? If NO, the LLM is told "REJECTED" and must choose again) → revalidate → confirm → invoice → END. With nothing pending but `last_confirmed` set → confirm_order again → `already_confirmed` → **silent**
  - `cancel_pending_order` → cancel (agent cancels the draft). Nothing pending → reply `nothing_to_cancel` (NEVER escalates by itself)
  - `request_cancel_confirmed_order(order_number?, reason)` → cancel_handoff: **never cancels**, calls notify_staff("sales") and tells the customer a human will handle it. Only when the customer explicitly asks.
  - `forward_to_staff(question)` → notify_staff("sales"); for policies/fees/delivery times not in BUSINESS FACTS (the LLM must not invent them)
  - `report_payment(reference, method?, order_number?)` → payment node → `submit_payment` (no amount param: amount = order total). Replies: payment_received / payment_already_submitted / already_paid / no_unpaid_order / order_cancelled_unpaid (+ accounts notified) / duplicate_reference / payment_reference_needed. Keeps a pending summary waiting.
- `AGENT_SYSTEM` contains BUSINESS FACTS (wholesale, whole packs only, 2h summary validity, confirmed-order cancellations go to staff). Optional tool params are nullable (`["string","null"]`): Groq validates tool calls strictly.
- Groq free tier: 200k tokens/day per model (`openai/gpt-oss-120b`); a full live scenario run uses a big share. Use `--model openai/gpt-oss-20b` when the limit is hit.

Flow: new message → ingest → identify_customer (unknown → handoff: notify_staff + polite reply; else loads `profile`) → memory → agent. Paused conversations resume at `wait_for_reply` (`interrupt()`) → memory → agent.

**Memory** (everything the LLM sees is built per call; see docs/LEARNING.md §5):
- Conversation state: checkpointer → `PENDING:` line (`_pending_text`).
- Short-term history: `recent_messages(thread, limit=SUMMARY_TRIGGER, after_id=summarized_until)`.
- Summary memory: `memory` node; when > `SUMMARY_TRIGGER` (16) unsummarised messages, the oldest (all but `KEEP_RECENT`=6) are merged into `state.summary` via `SUMMARY_SYSTEM` (no prices); number-guarded (`summary_numbers`); `summarized_until` = last summarised message id.
- Focus memory: `state.focus` (max 3 products) from read-tool results (`_focus_from`) and the draft; shown as `CURRENTLY DISCUSSING`.
- Long-term: `get_customer_profile` (orders.py, deterministic: orders_count, last order, top-3 usual items with last_qty, preferred_language) loaded in identify_customer; shown as `CUSTOMER HISTORY`; enables "my usual order".
- All three go into `AGENT_SYSTEM`'s `{memory}` slot via `_memory_text`.
- Don't use `durability="exit"` on invoke: the final checkpoint is written after invoke returns → `get_state` race (seen as flaky tests).
- Logs are buffered per inbound message (`log_context(..., buffered=True)` in service) and written in one transaction.

- State checkpointed in Postgres per conversation, `thread_id = phone`. Reply in the customer's language (LLM decides; action tools carry `language`).
- **Number guard** on every reply: free-text answers may only contain numbers found in tool results / the conversation / the system prompt (one retry, then error reply); replies of deterministic steps are written by the LLM from `display(facts)` (money pre-formatted as "Rs 5,400") and must contain only those numbers + the required ones (e.g. total), else the English backup template is sent.
- Log every step to `workflow_logs`. The LLM sits behind `OrderLLM` so tests inject a fake LLM.

Implementation (`app/agent/`):
- `llm.py`: tool specs (`READ_TOOLS`, `ACTION_TOOLS`), prompts (`AGENT_SYSTEM`, `WRITE_SYSTEM`), `OrderLLM` protocol = `decide(system, messages, tools) -> AIMessage` + `write(system, prompt) -> str`. `GroqLLM` uses `bind_tools(tool_choice="auto")`. `LoggedLLM` logs every call (kind=`llm`).
- `text.py`: `rs()`/`pct()`/`display()`, `TASKS` (what each reply kind must say), English `BACKUP` templates, `stock_option_label`, `number_guard`.
- `graph.py`: nodes return `Command(goto=...)`; only edge is START→ingest. Questions are stored in state (`question`, `awaiting`) and sent via the interrupt payload of `wait_for_reply`. After a free-text answer while something is pending, the graph pauses again with `question=None` (no repeat). `clean_text` strips markdown. `_pending_text` tells the LLM what is pending (and, after a confirmation, to call confirm_pending_order on a repeated yes).
- Guards logged (kind=`guard`): `confirmation_is_explicit`, `whole_packs_only`, `nothing_pending_to_cancel`, `answer_numbers`, `reply_numbers`, `order_quantities_positive`, `option_in_range`, `draft_matches_prices`, `revalidate_before_confirm`, `success_confirmed_by_tools`, `duplicate_confirmation_suppressed`, `agent_gave_up`.
- Node wrapper must NOT use functools.wraps: LangGraph inspects `__wrapped__` and then won't pass `config` (which carries `run_id`).
- `service.py`: `postgres_checkpointer()` (psycopg pool: autocommit, prepare_threshold=0, dict_row, check_connection) and `OrderAgent.handle_message(phone, text) -> AgentReply(messages, waiting_for, status, order_number, invoice_number)`. Records the inbound message BEFORE running the graph (the agent reads it via recent_messages). `reset_thread(phone)`. `seed --reset` also truncates checkpoint tables.

## Acceptance scenarios

1. Ali Traders: "Send 10 cartons of Dalda oil and 5 cartons of Sooper biscuits" → summary total **Rs 62,230** (63,500 − 2%) → "yes" → order + invoice, stock reserved.
2. Bismillah Store: "20 bags rice" → asks which of P-003/P-004/P-005 → "1" → total **Rs 47,530** → "yes" → order.
3. Karachi Mart: "30 cartons Nestle water 1.5L" → only 18 available, offers 18 / 18 + 12 Aquafina / cancel → "2" → total **Rs 21,240** → "yes" → order.
4. Ali Traders: "5 more cartons of Dalda please" → **Rs 26,460** → "yes" → order → "yes" again → no new order, no message.
5. +92 333 9876543 (unknown): "Send 5 cartons of tea" → no order, staff notified.
6. Madina General Store: "bhai 10 carton dalda aur 5 sooper bhej do" → Roman Urdu summary **Rs 62,230** → "haan theek hai, bhej do" → order, reply in Roman Urdu.

## Build plan (stop after each step for review)

1. ✅ Project setup + CLAUDE.md
2. ✅ Models + migrations + seed
3. ✅ The 8 tools with pytest tests (no LLM)
4. ✅ LangGraph agent + CLI chat script (`python -m app.cli --phone ...`)
5. ✅ Scenario tests (fake LLM, no API key) + optional live run; plus memory (summary, focus, long-term profile)
6. ✅ FastAPI `/chat` endpoint
7. ✅ Suppliers: reorder point, purchase orders with manager approval, receiving, backorders
8. ✅ Payments: online advance payment verified by an accountant, 24h deadline, supplier invoices (three-way match)
9. Delivery — next, after the user pushes 8. User's choices: riders by area first, then fewest deliveries today,
   capacity 3, FIFO queue when full; delivery date = same day if paid before 2 PM PKT else next day (already set as
   `orders.delivery_date` by verify_payment); NO OTP for now; failed delivery → retry once next day, then return
   (stock released). Needs: riders, deliveries tables, customer area, order statuses assigned/out_for_delivery/
   delivered/returned (CHECK migration), stock physical −= qty on delivery, rider endpoints, customer messages.

## Procurement (suppliers) — `app/tools/procurement.py`

User's choices: triggers = reorder point after each confirmed order + daily check; supplier choice = **preferred supplier with fallbacks** (no scoring); manager always approves; sending to suppliers is simulated; backorders offered.
- Tables (migration `cd5e56569960`): `suppliers`, `supplier_products` (priority 1 = preferred, unit_cost, lead_time_days, min_order_qty; unique (product, priority)), `purchase_orders` (status draft/approved/rejected/sent/partially_received/received/cancelled, approved_by, expected_at), `purchase_order_items` (qty_ordered, qty_received ≤ ordered, line_total = qty × cost), `backorders` (waiting/ready/cancelled). `inventory.max_level` (≥ reorder_level).
- `run_reorder_check(reason, product_ids?, exclude_supplier_ids?)`: takes `pg_advisory_xact_lock(hashtext('reorder-check'))`; position = physical − reserved + on order (open POs: draft/approved/sent/partially_received); if position ≤ reorder_level → qty = max(max_level − position, MOQ) from the lowest-priority active supplier; appends to that supplier's existing draft PO or creates `PO-000001`; notifies manager.
- `approve` / `reject(try_next_supplier → reorder check excluding that supplier)` / `send` (sets expected_at = now + max lead time; logs a simulated message on thread `supplier:<id>`) / `receive` (only sent/partially_received; receiver ≠ approver → `SAME_PERSON`; over-receipt → `RECEIPT_EXCEEDS_ORDER`; physical += qty; marks waiting backorders `ready` FIFO and messages the customer thread + notifies sales). Transitions via `_transition()` (row locked FOR UPDATE, allowed-status check → `INVALID_TRANSITION`).
- `get_restock_eta(product_id)` (read; LLM tool): incoming qty + eta_days (from expected_at, else supplier lead time).
- Agent: `check_stock` adds a `backorder` option when short, available > 0 and stock is incoming; `state.backorders`; summary/confirmed facts include `backorders`; `invoice` node creates backorder rows and runs `run_reorder_check` for the ordered + backordered products.
- Staff endpoints `app/api/staff.py` (`/staff/reorder-check`, `/staff/purchase-orders[...]/approve|reject|send|receive`); tool error codes → HTTP 404/409/403/422. Daily job: `python -m app.jobs reorder-check [--test-db]`.

## Payments — `app/tools/payments.py`, `app/tools/payables.py`, `app/api/finance.py`

User's choices: **online payment only** (bank transfer / JazzCash / Easypaisa, no COD, no credit, no gateway);
a human (accounts) verifies every payment; 24h deadline; supplier bills need three-way match + manager approval
+ a different person records payment.
- Migration `ebe90ceec433` (hand-edited: drop `ck_orders_status` → UPDATE confirmed/fulfilled → ready_to_dispatch →
  new CHECK; server_default awaiting_payment). Tables `payments` (partial unique indexes: one live payment per
  order; (method, reference) unique unless rejected), `supplier_invoices` (unique (supplier, invoice_number),
  CHECK difference = amount − expected_amount), `suppliers.payment_terms_days` (KW 15, Metro 7, Sindh 30).
- `confirm_order` creates `awaiting_payment` with `payment_due_at = now + 24h` (`orders.PAYMENT_WINDOW`).
  `orders.release_reservation(s, order)` = guarded `reserved -= qty` (also for Part 2).
- `submit_payment`: locks the customer's latest awaiting_payment order (or by number); pending claim exists →
  `already_submitted`; no unpaid order but one cancelled in the last 3 days → `ORDER_CANCELLED` (late payer:
  agent notifies accounts for a refund); notifies `accounts`. `verify_payment` (pending → verified; order → ready_to_dispatch,
  paid_at, `delivery_date_for` = PKT (fixed UTC+5) < 14:00 same day else next) writes the customer message in
  the SAME transaction (`record_message(..., session=s)`, templates en/roman_ur/ur in `MESSAGES`, language from
  the draft) + notifies warehouse. `reject_payment` → customer asked for a new ID; after the deadline → cancel.
- verify/reject take `payment` = payment id, `ORD-…` or the transaction reference (`_matching_ids`; id and reference
  pointing at different payments → `AMBIGUOUS_PAYMENT`; not found → message lists waiting payments).
- `cancel_unpaid_orders` (job `payment-deadline`, endpoint `/staff/payments/cancel-unpaid`): only orders past due
  with NO pending/verified payment (re-checked after `FOR UPDATE`); releases stock, cancels waiting backorders,
  messages the customer; reminds accounts about claims older than 2h.
- Payables: `record_supplier_invoice` (PO must be (partially_)received; expected = received value − earlier
  invoices' expected; mismatch flagged), `approve` (≠ recorder; mismatch needs `override_reason` else
  `INVOICE_MISMATCH`), `reject` (message to supplier thread), `pay` (≠ approver), `list_supplier_invoices`
  (overdue_only, total_open).
- Agent: confirmation reply facts include `pay_to` (`payments.PAY_TO`) and `pay_before`; the `memory` node
  reloads `get_customer_profile` every turn (`open_orders` → `OPEN ORDERS` line) so the LLM sees verifications
  that happened while the customer was away. AgentReply.status stays `confirmed` after "yes".
- notify_staff roles: sales, warehouse, manager, accounts.

## API (`app/api/main.py`)

- `create_app(llm=None)` app factory (tests inject `FakeLLM`); module-level `app = create_app()` for uvicorn. `lifespan` opens `postgres_checkpointer()` and stores `OrderAgent` on `app.state.agent`; endpoints get it via `Depends(get_agent)`. Endpoints are sync `def` (blocking DB + LLM work runs in FastAPI's thread pool).
- `POST /chat {phone, message}` → `{thread_id, messages[], waiting_for, status, order_number, invoice_number}`. Empty `messages` = send nothing (duplicate "yes"). Bad input → 422. Busy conversation → 409.
- `GET /conversations/{phone}?limit=50` → `{thread_id, waiting_for, messages[{id, direction, text}]}` (read-only).
- `GET /health`.
- `OrderAgent.handle_message` holds `conversation_lock(thread_id)` (tools/notify.py: Postgres session advisory lock on `hashtext(thread_id)`, `lock_timeout` 30s → `ConversationBusy`) so messages from one phone are processed one at a time, also across processes.
- `USE_TEST_DB=1` makes `app/config.database_url()` use `TEST_DATABASE_URL` (for running the server against the test branch).
- Tests: `tests/api/test_api.py`.

At each step, briefly explain what was built and how to run it.

## Commands

```powershell
# from repo root
.\.venv\Scripts\Activate.ps1
cd backend
pip install -r requirements.txt
alembic upgrade head         # apply migrations (also proves the Neon connection)
python -m app.db.seed        # upsert master data (keeps live stock)
python -m app.db.seed --reset  # wipe orders/drafts/logs/messages, restore seed stock
alembic revision --autogenerate -m "..."   # after model changes; review the file before upgrading
pytest                       # DB tests run on the Neon TEST branch only
python -m app.cli --phone "+92 300 1234567"                      # live chat (Groq), main DB
python -m app.cli --phone "+92 300 1234567" --test-db --seed-reset -m "10 cartons dalda" -m "yes"
python -m app.cli --phone "+92 300 1234567" --reset-thread       # forget conversation state
python -m app.jobs payment-deadline [--test-db]                  # cancel unpaid orders (run hourly)
uvicorn app.api.main:app --reload                                # API on http://127.0.0.1:8000 (docs at /docs)
$env:USE_TEST_DB="1"; uvicorn app.api.main:app                   # API on the test branch
```

## Tests

- DB tests run ONLY against `TEST_DATABASE_URL` (a Neon branch) from `backend/.env`. `tests/conftest.py` swaps it into `DATABASE_URL` before any app import and refuses to run if it's the same endpoint as main. Without it, DB tests are skipped.
- `migrated_db` (session fixture) runs `alembic upgrade head` on the test branch; `fresh_db` runs `seed(reset=True)` before each test. `tests/tools/` uses it automatically.
- Tests may query the DB directly (fixtures `sql`, `stock`); agent code may not.
- `-pooler` is stripped from any DB URL automatically (`app/config.py`).
- A full run takes ~15 min because every query is a network round trip to Neon.
- `tests/scenarios/`: the real graph + tools + test DB + Postgres checkpointer with a SCRIPTED fake LLM (`FakeLLM` in conftest: `act(tool, **args)` / `answer(text)` per customer message; `write()` returns `"[<language>] <FACTS json>"`, answers the confirm check via `accepts`, summaries via `summary_text`). Files: `test_acceptance.py` (the 6 scenarios), `test_safety.py`, `test_memory.py`, `test_live.py` (real Groq; only with `RUN_LIVE_LLM=1`, optional `LIVE_MODEL`).
- **Never chat with `--test-db` while pytest runs**: tests reset that branch (incl. conversation state) before every test, and use the same customers.
- Code style for this learning project: no comments in new code; explanations go in chat and docs/LEARNING.md.

```powershell
pytest tests/tools -q        # tool tests only
pytest tests/scenarios -q    # scenario tests (fake LLM)
$env:RUN_LIVE_LLM="1"; pytest tests/scenarios/test_live.py -q   # live Groq (costs tokens)
```
