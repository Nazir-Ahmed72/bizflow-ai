# Learning guide: the ideas behind BizFlow AI

Each section: what the idea is, where it lives in this project, and what to read.
Read them in order; each one builds on the previous.

---

## 1. Tools: the LLM decides, code does the work

**Idea.** An LLM is good at understanding language and bad at arithmetic and at being exactly right. So the LLM never touches the database and never calculates. It *chooses* a tool (a normal Python function), the code runs it, and the result goes back to the LLM.

**In this project.**
- [backend/app/tools/](../backend/app/tools/): every tool is a plain function with Pydantic input/output (`@tool` in `base.py`).
- [backend/app/agent/llm.py](../backend/app/agent/llm.py): `READ_TOOLS` (the LLM may call these freely) and `ACTION_TOOLS` (they only *ask* the graph to do a write).
- `catalog.compute_pricing()` is the only place money is calculated.

**Read.**
- Groq tool use (how the model sees your tools as JSON schemas): https://console.groq.com/docs/tool-use/overview
- Anthropic, "Building effective agents" (when to use workflows vs agents): https://www.anthropic.com/engineering/building-effective-agents
- Pydantic (the input/output models): https://docs.pydantic.dev/latest/

---

## 2. Graphs and state: LangGraph

**Idea.** Instead of one long `if/else`, the conversation is a graph of small steps (nodes). Each node reads a shared **state** dict and returns updates plus the next node (`Command(goto=...)`).

**In this project.** [backend/app/agent/graph.py](../backend/app/agent/graph.py): the docstring at the top draws the graph. `OrderState` is the state. Each `@node(...)` function is one step.

**Read.**
- LangGraph Graph API guide (state, nodes, edges): https://docs.langchain.com/oss/python/langgraph/use-graph-api
- `Command` (update state + choose the next node in one return): https://reference.langchain.com/python/langgraph/types/Command

---

## 3. Checkpointers: memory that survives restarts

**Idea.** A **checkpointer** saves the graph state after each step, keyed by a `thread_id`. Here `thread_id` = the customer's phone number, so every customer has their own saved conversation in Postgres (`checkpoint_*` tables). If the server restarts, the conversation continues where it stopped.

**In this project.** `postgres_checkpointer()` in [backend/app/agent/service.py](../backend/app/agent/service.py). Test: `test_conversation_survives_a_restart`.

**Lesson learned here.** We tried saving only once at the end of each message (`durability="exit"`) for speed. The save then happened in the background, and the next read sometimes saw old state: a race condition. We went back to the default.

**Read.**
- Persistence (threads, checkpoints, durability modes): https://docs.langchain.com/oss/python/langgraph/persistence
- Checkpointer backends (Postgres, SQLite, in-memory): https://docs.langchain.com/oss/python/integrations/checkpointers

---

## 4. Interrupts: waiting for the human

**Idea.** `interrupt()` pauses the graph and saves it. The next message resumes it with `Command(resume=text)`. This is how the bot "waits for yes" for hours without a running process.

**In this project.** The `wait_for_reply` node, and `OrderAgent.handle_message` (resume vs new turn).
Rule to remember: a node re-runs **from its beginning** on resume, so we keep `interrupt()` alone in its own node. Then no tool or LLM call is repeated.

**Read.**
- Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Human-in-the-loop guide: https://docs.langchain.com/oss/python/langgraph/add-human-in-the-loop

---

## 5. Memory: what the LLM gets to see

**Idea.** An LLM has **no memory**. Every call starts blank. "Memory" means: what *your code* puts into the prompt each time. This project uses five kinds:

| Memory | Question it answers | Where |
|---|---|---|
| Conversation state | What am I waiting for? | checkpointer → `PENDING:` line in the prompt (`_pending_text`) |
| Short-term history | What was just said? | last messages from the `messages` table (`recent_messages`) |
| **Summary memory** | What happened earlier in a long chat? | `memory` node → `CONVERSATION SUMMARY` |
| **Focus (entity) memory** | Which product does "a carton" refer to? | `focus` in state → `CURRENTLY DISCUSSING` |
| **Long-term profile** | What does this customer usually order? | `get_customer_profile` tool → `CUSTOMER HISTORY` |

**Summary memory.** When more than `SUMMARY_TRIGGER` (16) messages are unsummarised, the oldest ones (all but the last `KEEP_RECENT` = 6) are merged by the LLM into a short summary. The prompt then carries summary + recent messages instead of the whole chat: fewer tokens, same meaning. The summary goes through the **number guard** too, so an invented number can't sneak into memory.

**Focus memory.** Whenever a turn touches products (search, prices, stock, an order), they become the "currently discussing" list (max 3). It's a cheap, deterministic way to resolve "it", "that", "a carton".

**Long-term memory.** Built from confirmed orders by a deterministic tool (counts and quantities are computed by code, not the LLM), so "send my usual order" works.

**Read.**
- LangGraph memory overview (short-term vs long-term): https://docs.langchain.com/oss/python/langgraph/memory
- Adding memory, including trimming and summarising messages: https://docs.langchain.com/oss/python/langgraph/add-memory
- Short-term memory in LangChain agents: https://docs.langchain.com/oss/python/langchain/short-term-memory
- Anthropic, "Effective context engineering for AI agents" (what to put in the context window and why): https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

---

## 6. Guards: never trust the LLM with facts

**Idea.** Check the LLM's output with code before it reaches the customer or the database.

**In this project** (all logged to `workflow_logs`, kind=`guard`):
- `number_guard` ([text.py](../backend/app/agent/text.py)): every number in a reply must exist in tool data.
- `confirmation_is_explicit`: a second, narrow LLM question before confirming an order.
- `whole_packs_only`: loose packets are refused in code.
- `revalidate_before_confirm`: stock and prices are checked again just before the order.

---

## 7. Databases: transactions, atomic updates, idempotency

**Idea.** Two customers can say "yes" at the same moment. The database must never sell stock twice, and a repeated "yes" must not create two orders.

**In this project.** `confirm_order` in [backend/app/tools/orders.py](../backend/app/tools/orders.py):
- `SELECT ... FOR UPDATE` locks the draft row, so two requests for the same draft wait in line.
- `UPDATE inventory SET reserved = reserved + :qty WHERE physical - reserved >= :qty`: check-and-reserve in one statement; if the row count isn't 1, everything rolls back.
- `idempotency_key` (`<draft_id>:confirm`, UNIQUE in the DB): a repeated request returns the existing order.

**Read.**
- PostgreSQL explicit locking (`FOR UPDATE`): https://www.postgresql.org/docs/current/explicit-locking.html
- PostgreSQL transaction isolation: https://www.postgresql.org/docs/current/transaction-iso.html
- Stripe, "Designing robust and predictable APIs with idempotency": https://stripe.com/blog/idempotency
- SQLAlchemy 2.0 unified tutorial: https://docs.sqlalchemy.org/en/20/tutorial/
- Alembic (database migrations) tutorial: https://alembic.sqlalchemy.org/en/latest/tutorial.html

---

## 8. The web API: FastAPI

**Idea.** The bot logic (`OrderAgent`) doesn't care where messages come from. The API is a thin layer: it validates the HTTP request, calls `agent.handle_message`, and returns JSON. A WhatsApp webhook or a frontend would call it.

**In this project.** [backend/app/api/main.py](../backend/app/api/main.py):
- `create_app(llm=None)`: an *app factory*. Production gets the real Groq LLM; tests pass a fake one. This is dependency injection.
- `lifespan`: runs once at server start and stop. It opens the Postgres checkpointer pool and builds the agent, so each request doesn't reconnect.
- `Depends(get_agent)`: FastAPI hands the shared agent to each endpoint.
- Request/response models (`ChatRequest`, `ChatResponse`): Pydantic validates input; bad input gets HTTP 422 automatically.
- The endpoints are plain `def` (not `async def`) because the agent does blocking work (database + LLM calls). FastAPI runs them in a thread pool so the server stays responsive.

**Read.**
- FastAPI tutorial (request bodies, response models): https://fastapi.tiangolo.com/tutorial/body/
- Lifespan events: https://fastapi.tiangolo.com/advanced/events/
- Dependencies: https://fastapi.tiangolo.com/tutorial/dependencies/
- `async def` vs `def` in FastAPI: https://fastapi.tiangolo.com/async/
- Testing with `TestClient`: https://fastapi.tiangolo.com/tutorial/testing/

---

## 9. One conversation at a time: advisory locks

**Idea.** On a server, two messages from the same phone can arrive at the same moment (a WhatsApp retry, a double tap). Both would load the same saved state and work on it at once, and one could overwrite the other. A **Postgres advisory lock** is a named lock that *your code* defines; here the name is the phone number. The second message waits until the first is finished (up to 30 s; after that the API answers HTTP 409 "busy"). Because the lock lives in Postgres, it also works with several server processes.

**In this project.** `conversation_lock()` in [backend/app/tools/notify.py](../backend/app/tools/notify.py), used by `OrderAgent.handle_message`. Tests: `test_conversation_lock_makes_messages_wait_in_line`, `test_simultaneous_yes_makes_one_order_and_one_message`, `test_busy_conversation_returns_409`.

Note the layers of protection against a double "yes": the lock (messages wait in line), the graph (`last_confirmed` → silent), and the database (unique `idempotency_key`). Any one of them is enough; together they're defence in depth.

**Read.**
- PostgreSQL advisory locks: https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS
- Advisory lock functions (`pg_advisory_lock`, `pg_advisory_unlock`): https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS

---

## 10. Buying from suppliers (procure-to-pay)

**Idea.** A distributor runs two cycles. *Order-to-cash*: customers order, we deliver and get paid (sections 1–9).
*Procure-to-pay*: we buy from suppliers, receive the goods, and pay. This section is the first half of procure-to-pay.

**Concepts, and where they live** ([backend/app/tools/procurement.py](../backend/app/tools/procurement.py)):

| Concept | What it means | In this project |
|---|---|---|
| **Reorder point** (min) | "Buy more when stock falls to this level" | `inventory.reorder_level` |
| **Max level** (order-up-to) | "Buy enough to get back up to this level" | `inventory.max_level`; quantity = max − position |
| **Inventory position** | physical − reserved + already on order | `_inventory_positions()`: counting stock *on its way* is what prevents ordering twice |
| **Minimum order quantity** | Suppliers won't sell less than X | `supplier_products.min_order_qty`; we round up |
| **Lead time** | Days from ordering until delivery | `supplier_products.lead_time_days` → `expected_at`, `get_restock_eta` |
| **Preferred supplier + fallback** | Fixed priority list per product | `supplier_products.priority` (1 = preferred); a rejection with `try_next_supplier` skips that supplier |
| **Purchase order state machine** | Only allowed status changes | `_transition()`: draft → approved → sent → (partially_)received; rejected |
| **Human in the loop** | People approve and record; the system proposes | staff endpoints in [backend/app/api/staff.py](../backend/app/api/staff.py) |
| **Maker-checker / separation of duties** | The approver can't also confirm receipt | `receive_purchase_order` returns `SAME_PERSON` |
| **Backorder** | A promise to deliver the rest later | `backorders` table; marked `ready` when goods arrive |
| **Triggers** | When to run the check | B: after every confirmed order (`invoice` node). C: daily job `python -m app.jobs reorder-check` |

**Two patterns you've seen before, reused.**
- *Serialising with a lock:* two orders confirmed at the same moment could both see "low stock" and both draft
  a purchase order. `run_reorder_check` takes a transaction-level advisory lock (`pg_advisory_xact_lock`) first,
  so checks run one at a time. Same idea as the conversation lock in §9.
- *State machine with a locked read:* `_transition()` loads the purchase order `FOR UPDATE`, checks the current
  status is allowed, then changes it, all in one transaction. Two managers clicking "approve" at once can't both succeed.

**Why the database status and not a LangGraph interrupt for approvals?** An interrupt pauses *a conversation*.
A purchase order isn't a conversation: anyone may approve it, at any time, from any screen, and we want to list,
filter and audit them. A status column plus endpoints fits that better. Use interrupts for "this conversation
waits for this person's reply"; use a status for "this record waits for someone to act".

**Scheduling the daily check.** The job is a normal command, so any scheduler can run it. On Windows:
`schtasks /create /tn "BizFlow reorder check" /sc daily /st 06:00 /tr "<path>\.venv\Scripts\python.exe -m app.jobs reorder-check"`
(run from `backend/`). On Linux you'd use cron. Keeping jobs as plain commands means they're easy to test and run by hand.

**Read.**
- Reorder point: https://en.wikipedia.org/wiki/Reorder_point
- Safety stock (the next refinement of the reorder point): https://en.wikipedia.org/wiki/Safety_stock
- Purchase order: https://en.wikipedia.org/wiki/Purchase_order
- Finite-state machine: https://en.wikipedia.org/wiki/Finite-state_machine
- Separation of duties: https://en.wikipedia.org/wiki/Separation_of_duties
- PostgreSQL advisory locks (transaction-level `pg_advisory_xact_lock`): https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS
- FastAPI routers (`APIRouter`, how the staff endpoints are grouped): https://fastapi.tiangolo.com/tutorial/bigger-applications/

---

## 11. Money in and money out (payments)

**Idea.** §10 was the "procure" half of procure-to-pay. This section adds the money: customers paying us
(*accounts receivable*) and us paying suppliers (*accounts payable*). The rule of thumb: **software proposes,
records and reminds; a person moves or confirms money.** Neither the LLM nor any automatic step decides that
money has arrived or should leave.

**Concepts, and where they live** ([payments.py](../backend/app/tools/payments.py),
[payables.py](../backend/app/tools/payables.py), [finance.py](../backend/app/api/finance.py)):

| Concept | What it means | In this project |
|---|---|---|
| **Advance payment** | Goods leave only after money arrives | `orders.status`: `awaiting_payment` → `ready_to_dispatch` |
| **Separate status per question** | "Is it paid?" and "where is the order?" change at different times | `payments.status` (pending_verification / verified / rejected) next to `orders.status` |
| **Payment claim vs payment** | "I paid, TID 784512" is a *claim* until someone checks the bank | `submit_payment` stores a claim; `verify_payment` makes it real |
| **Amount from the system, not the customer** | The customer can't type the amount | `report_payment` has no amount field; `payments.amount = orders.total` |
| **Reconciliation** | Matching our records against the bank statement | the accountant's verify / reject step |
| **Deadline + release** | Unpaid orders shouldn't hold stock forever | `cancel_unpaid_orders` (job `payment-deadline`): reserved −= qty, order cancelled |
| **Three-way match** | Purchase order × goods receipt × supplier invoice must agree | `record_supplier_invoice`: owed = qty received × agreed cost; anything else is `mismatch` |
| **Payment terms / due date** | "Net 15": pay within 15 days of the invoice | `suppliers.payment_terms_days` → `due_date`, `overdue` in the list |
| **Maker-checker (four eyes)** | One person records, another approves, a third pays | `SAME_PERSON` errors in approve / pay |
| **Duplicate-payment controls** | The same bill or bank transfer must never count twice | unique (supplier, invoice_number); partial unique index on payment references |

**Your scenario: the accountant is busy and the customer has left.** Three design choices make this work:
1. *The claim protects the order.* The deadline job only cancels orders with **no** pending or verified payment.
   A customer who paid on time is never punished for a slow accountant; instead the job reminds accounts about
   claims older than 2 hours.
2. *The system messages first (a proactive message).* `verify_payment` writes the customer's message in the
   **same transaction** as the status change. Either both happen or neither: the customer is never told
   "paid" for an order that isn't, and never misses the message for one that is. This is the
   **transactional outbox** pattern: a real WhatsApp sender would later read unsent rows from `messages` and
   deliver them.
3. *The agent re-reads the truth every turn.* The `memory` node reloads the customer profile, including
   `OPEN ORDERS` with payment status, so when the customer comes back hours later the LLM already knows the
   order is paid and when it will arrive.

**Two database ideas worth noticing.**
- *Partial unique index:* `UNIQUE (order_id) WHERE status IN ('pending_verification','verified')` means "at most
  one live payment per order", while rejected attempts can pile up as history. A plain UNIQUE couldn't say that.
- *Check-then-act under a lock:* the deadline job locks each order `FOR UPDATE`, then checks again that no payment
  arrived. `submit_payment` locks the same row first, so a payment reported at the last second and the
  cancellation can't both win. (Same idea as `_transition()` in §10.)

**Time zones.** Deadlines are stored in UTC (`timestamptz`), but "before 2 PM" means 2 PM in Karachi.
Pakistan has no daylight saving, so a fixed `UTC+5` works without the `tzdata` package (Windows has no built-in
time-zone database for `zoneinfo`).

**Changing a CHECK constraint (migration lesson).** Autogenerate doesn't notice CHECK changes. The migration
`ebe90ceec433` does it by hand, in the only safe order: drop the old constraint → move existing rows to new values
(`confirmed` → `ready_to_dispatch`) → add the new constraint. The downgrade does the reverse.

**Why a gateway would change only one step.** A payment gateway (JazzCash, Easypaisa, Stripe) calls your server
when money arrives (a **webhook**), signed with a secret so you can prove it came from them. In this design the
webhook would simply call `verify_payment` with `verified_by="gateway"`; everything after it stays the same.

**Read.**
- Accounts receivable: https://en.wikipedia.org/wiki/Accounts_receivable
- Accounts payable and the three-way match: https://en.wikipedia.org/wiki/Accounts_payable
- Bank reconciliation: https://en.wikipedia.org/wiki/Bank_reconciliation
- Four-eyes principle: https://en.wikipedia.org/wiki/Two-man_rule
- Transactional outbox pattern: https://microservices.io/patterns/data/transactional-outbox.html
- PostgreSQL partial indexes: https://www.postgresql.org/docs/current/indexes-partial.html
- `SELECT ... FOR UPDATE` row locks: https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS
- Webhooks and signature checks (Stripe's explanation is clear even if you never use Stripe): https://docs.stripe.com/webhooks
- Python `datetime` and time zones: https://docs.python.org/3.10/library/datetime.html#aware-and-naive-objects

---

## 12. Testing an LLM app

**Idea.** You can't test "is the model smart?" with exact assertions, but you *can* test everything around it. The scenario tests use a **scripted fake LLM** that says "for 'yes', call `confirm_pending_order`". Everything after that decision is real code on a real (test) database. A separate live test checks the real model, only when you ask (`RUN_LIVE_LLM=1`), because it costs tokens.

**In this project.** [backend/tests/scenarios/](../backend/tests/scenarios/): `conftest.py` (the fake), `test_acceptance.py` (the 6 scenarios), `test_safety.py`, `test_memory.py`, `test_live.py`.

**Read.**
- pytest fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- Neon branching (why tests use their own database branch): https://neon.com/docs/introduction/branching
