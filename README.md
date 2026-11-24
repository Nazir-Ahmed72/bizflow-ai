# BizFlow AI

**An AI order assistant for wholesale FMCG and grocery distributors.**
Shopkeepers message their orders the way they already do (in English, Urdu or Roman Urdu), and BizFlow AI turns
those messages into priced, stock-checked orders with invoices, collects payment, and keeps the warehouse,
accounts and suppliers in step.

```
Customer: bhai 10 carton dalda aur 5 sooper bhej do
Bot:      Madina General Store, aap ka order:
          1. Dalda Cooking Oil 1L - 10 carton @ Rs 5,400 = Rs 54,000
          2. Sooper Classic biscuits - 5 carton @ Rs 1,900 = Rs 9,500
          Subtotal Rs 63,500, discount 2% (Rs 1,270), total Rs 62,230.
          Confirm karne ke liye 'haan' likhein. Yeh summary 2 ghante tak valid hai.
Customer: ek carton mein kitni bottles hoti hain?
Bot:      Ek carton mein 12 bottle hoti hain.
Customer: haan theek hai, bhej do
Bot:      Order ORD-000001 place ho gaya, stock aap ke liye reserve hai. Rs 62,230 Meezan Bank ya
          JazzCash/Easypaisa par 24 ghante mein bhej kar transaction ID yahan bhej dein.
Customer: bhej diya, TID 784512
Bot:      Shukriya! Accounts team payment check kar rahi hai, confirm hote hi aap ko message aa jayega.
          ... (later, after the accountant checks the bank statement) ...
Bot:      Order ORD-000001 ki payment Rs 62,230 mil gayi hai. Aap ka order confirm ho gaya hai aur aaj
          (24 Nov) Liaquatabad No. 10 par deliver hoga. Shukriya!
Bot:      Aap ka order ORD-000001 aaj hamara rider Yasir (+923035550104) deliver karega.
Bot:      Aap ka order ORD-000001 rawana ho gaya hai, rider Yasir (+923035550104) aa raha hai.
Bot:      Order ORD-000001 deliver ho gaya hai. Shukriya!
```

---

## The problem

A distributor in Karachi serves hundreds of small shops. Orders arrive all day as informal messages:
*"bhai 10 carton dalda bhej do"*, *"20 bags rice"*, *"same as last time"*. Today a person has to:

- read and interpret each message (mixed languages, brand nicknames, typos)
- work out which product is meant ("rice": basmati, sella or broken?)
- check whether there is enough stock, and offer alternatives when there isn't
- apply each shop's discount and calculate the bill
- confirm with the shop, then enter the order, reserve the stock and issue an invoice
- chase the payment, match it against the bank statement, and only then release the goods
- pay suppliers the right amount, only for goods that actually arrived
- decide which rider takes which order, and keep the shop informed until the goods arrive

This is slow, error-prone and doesn't scale. A wrong total, a double-booked order or overselling stock costs
money and trust.

## The solution

BizFlow AI handles the whole conversation automatically, from the first message to the invoice. An AI model
understands the customer, but every price, total and stock figure comes from the business system, never from
the AI.

## What it does

| Capability | Example |
|---|---|
| **Understands natural orders** in English, Urdu and Roman Urdu | "bhai 10 carton dalda aur 5 sooper bhej do" |
| **Recognises customers** by phone number, with their own discount | Ali Traders gets 2%, Bismillah Store 3% |
| **Resolves ambiguous products** by asking | "20 bags rice" → "Basmati, Sella or Broken?" |
| **Checks live stock** and offers options when short | only 18 Nestle left → "18 only / 18 + 12 Aquafina / cancel" |
| **Prices orders exactly** with the customer's discount | subtotal, discount and total on every summary |
| **Asks before ordering**: nothing is booked until the customer says yes | the summary is valid for 2 hours |
| **Creates the order, reserves stock and issues an invoice** | ORD-000001, INV-000001 |
| **Re-checks prices and stock** right before confirming | a new summary if anything changed |
| **Ignores duplicate confirmations** | a second "yes" creates nothing and sends nothing |
| **Answers questions** about products, prices, stock and past orders | "list my orders", "price of 3 cartons of tapal?" |
| **Remembers context**: the product being discussed, long chats, usual orders | "how many in a carton?", "send my usual order" |
| **Replies in the customer's language** | Roman Urdu in, Roman Urdu out |
| **Hands over to staff** when a human is needed | unknown numbers, cancelling a confirmed order, policy questions |
| **Reorders from suppliers automatically** when stock falls to its reorder point | Nestle at 18 (reorder point 40) → purchase order for 62 cartons |
| **Picks the supplier** (preferred supplier, with fallbacks) | Metro Supply for water; Karachi Wholesale if Metro can't deliver |
| **Keeps a manager in the loop**: every purchase order needs approval | draft → approved → sent → received |
| **Offers backorders** when new stock is on its way | "18 now, the other 12 when stock arrives (about 1 day)" |
| **Tells customers when stock returns** | "when will Tapal be back?" |
| **Collects payment before dispatch** (online: bank transfer, JazzCash, Easypaisa) | pay within 24 hours, reference = order number |
| **An accountant verifies every payment** against the bank statement | the customer is messaged the moment it is verified, even if they left the chat |
| **Cancels unpaid orders** after 24 hours and frees the reserved stock | reported payments are never cancelled while accounts is busy |
| **Schedules delivery** | paid before 2 PM: delivered the same day; otherwise the next day |
| **Pays suppliers safely**: invoice checked against what was ordered and received | billed for 62 cartons, received 60 → flagged |
| **Assigns riders automatically**: same area first, then whoever has delivered least today | Ali Traders (Saddar) → Imran, the Saddar rider |
| **Never overloads a rider** (3 deliveries at a time) and queues the rest | the 4th order waits and goes to the first rider who finishes |
| **Keeps the shop informed** at every step | rider assigned (name, phone) → on the way → delivered |
| **Retries failed deliveries** once the next day, then returns the goods | shop closed twice → back to the warehouse, refund arranged |
| **Logs everything** | every AI decision, tool call and safety check, with timing |

## What it guarantees

- **No invented numbers:** prices, discounts, totals and stock always come from the database.
- **No order without consent:** an order exists only after a clear "yes" to that exact summary.
- **No overselling:** stock is reserved atomically, even when several shops confirm at the same moment.
- **No double orders:** repeated or simultaneous confirmations produce exactly one order.
- **No silent failures:** success is reported only after the order and invoice really exist.
- **No dispatch without verified payment:** a person checks every payment; the amount always comes from the order.
- **Stock is exact:** reserved when the customer says yes, removed from the warehouse only when the rider delivers,
  released if the order is cancelled or returned.
- **No paying twice or for missing goods:** supplier invoices are matched against receipts, and recording,
  approving and paying are done by three different people.

---

## Architecture

The AI understands and talks; the business system decides and records.

```mermaid
flowchart TB
    subgraph Clients
        Dash["Web dashboard (Streamlit)<br/>customer chat, my orders,<br/>payments & inventory, delivery"]
        CLI["CLI chat"]
        API["REST API<br/>POST /chat"]
        Staff["Staff endpoints<br/>manager, accounts, storekeeper, dispatcher"]
        Riders["Rider app endpoints<br/>pickup, delivered, failed"]
        Jobs["Scheduled jobs<br/>reorder check, payment deadline, dispatch"]
        Future["WhatsApp / voice<br/>(planned)"]
    end

    subgraph Agent["Order agent"]
        Service["Conversation service<br/>one message at a time per phone"]
        Graph["Order workflow<br/>(LangGraph)"]
        LLM["AI model (Groq)<br/>understands, chooses actions, writes replies"]
        Guards["Safety checks"]
    end

    subgraph Tools["Business tools"]
        Read["Look-ups<br/>products, stock, prices, order history, order status"]
        Write["Actions<br/>drafts, confirm order, invoice, payment claims, notify staff"]
    end

    subgraph DB["PostgreSQL"]
        Business[("customers, products, inventory,<br/>orders, invoices")]
        Audit[("messages, activity logs,<br/>staff notifications")]
        Checkpoints[("conversation state<br/>per customer")]
    end

    Dash -- HTTP --> API
    Dash -- HTTP --> Staff
    CLI --> Service
    API --> Service
    Future -.-> API
    Service --> Graph
    Graph <--> LLM
    Graph --> Guards
    LLM -- "may look up" --> Read
    Graph -- "only the workflow acts" --> Write
    Staff --> Write
    Riders --> Write
    Jobs --> Write
    Graph --> Read
    Read --> Business
    Write --> Business
    Write --> Audit
    Service --> Checkpoints
```

---

## Dashboard

A web dashboard (Streamlit) that talks to the API over HTTP, exactly like a real WhatsApp or back-office client
would:

| Page | Who it's for | What it shows |
|---|---|---|
| **Chat with the bot** | a shopkeeper | the conversation, and next to it *what the server did* for the last message: every AI decision, tool call (with input and output), safety check and timing |
| **My orders** | a shopkeeper | their details, every order with items, payment status, delivery date and rider |
| **Payments & inventory** | accounts and the manager | payments to verify, stock (physical, reserved, available, on order, reorder warnings), purchase orders, supplier bills, staff notifications |
| **Delivery** | the dispatcher and riders | riders and their load, dispatch, and a simulated rider app (picked up / delivered / could not deliver) |

```mermaid
flowchart LR
    UI["Streamlit pages"] -- "POST /chat" --> API["FastAPI"]
    UI -- "GET /conversations/{phone}/trace" --> API
    UI -- "/staff/..., /riders/..." --> API
    API --> Agent["LangGraph agent"] --> LLM["Groq LLM"]
    Agent --> Tools["business tools"] --> DB[("PostgreSQL")]
    Tools -. "every call logged" .-> DB
```

---

## Order workflow

Every message first goes to the AI, which either answers a question or chooses an action. Actions run as fixed
business steps. When the assistant needs an answer from the customer, the conversation is saved and resumes with
the next message, even hours later.

```mermaid
flowchart TD
    start((new message)) --> ingest
    ingest --> identify[identify customer<br/>+ load order history]
    identify -- unknown number --> handoff[hand over to sales staff] --> done((end))
    identify --> memory[memory<br/>summarise long chats]
    memory --> agent{{"AI decides"}}

    agent -- "answer a question" --> reply[reply]
    reply -- nothing pending --> done
    reply -- something pending --> wait

    agent -- place order --> resolve[find products]
    resolve -- several variants --> askP[ask which product] --> wait
    resolve --> stock[check stock]
    stock -- short --> askS["offer: partial / substitute /<br/>remove item / cancel"] --> wait
    stock --> price[price with discount] --> draft[save draft] --> summary[send summary] --> wait

    agent -- choose option --> apply[apply choice]
    apply --> resolve
    apply --> stock

    agent -- confirm --> check{"clear yes to<br/>this summary?"}
    check -- no --> agent
    check -- yes --> reval[re-check stock + prices]
    reval -- changed --> stock
    reval --> confirm[create order<br/>reserve stock] --> invoice["issue invoice<br/>+ payment instructions"] --> done

    agent -- "customer sends<br/>transaction ID" --> pay["record payment claim<br/>notify accounts"] --> done

    agent -- cancel pending order --> cancel[cancel draft] --> done
    agent -- "cancel a confirmed order /<br/>question for staff" --> human[notify staff] --> done

    wait[/"wait for the customer<br/>(conversation saved)"/] -. next message .-> memory
```

---

## Confirming an order

```mermaid
sequenceDiagram
    autonumber
    participant C as Customer
    participant A as Order agent
    participant AI as AI model
    participant DB as Database

    C->>A: "haan theek hai, bhej do"
    A->>AI: what does the customer want?
    AI-->>A: confirm the pending order
    A->>AI: is this a clear yes to THIS summary?
    AI-->>A: yes
    A->>DB: re-check stock and prices
    A->>DB: one transaction: create order, reserve stock for every line
    DB-->>A: order created (or nothing at all if any line fails)
    A->>DB: issue invoice
    A-->>C: "Order ORD-000001 placed, invoice INV-000001, please pay Rs 62,230 within 24 hours"
    C->>A: "haan" (again)
    A-->>C: (nothing: the order already exists)
```

---

## Restocking from suppliers

After every confirmed order, and once a day, BizFlow AI checks each product's stock against its reorder point and
drafts purchase orders from the preferred supplier. A manager approves every purchase order; stock goes up only
when the goods are actually received.

```mermaid
flowchart TD
    trigger1((order confirmed)) --> check
    trigger2((daily check)) --> check
    check{"stock + incoming<br/>at or below<br/>reorder point?"} -- no --> ok((nothing to do))
    check -- yes --> pick["pick supplier<br/>preferred first, then fallbacks"]
    pick --> qty["quantity = refill to max level<br/>(at least the supplier's minimum)"]
    qty --> draft["draft purchase order<br/>(one per supplier)"] --> notify[notify manager]
    notify --> decide{manager}
    decide -- approve --> sent[send to supplier<br/>expected arrival date]
    decide -- reject --> next[try next supplier] --> draft
    sent --> receive["storekeeper records goods received<br/>(not the approver)"]
    receive --> stock[(stock goes up)]
    stock --> back[customers waiting on backorders<br/>are told the stock is back]
```

```mermaid
stateDiagram-v2
    [*] --> draft: system proposes
    draft --> approved: manager approves
    draft --> rejected: manager rejects
    approved --> rejected: manager rejects
    approved --> sent: sent to supplier
    sent --> partially_received: some goods arrive
    partially_received --> received: the rest arrives
    sent --> received: all goods arrive
    received --> [*]
    rejected --> [*]
```

---

## Getting paid

Payment is online and in advance. The bot never decides that money has arrived: a person in accounts checks the
bank statement. Because that can take hours, the customer doesn't have to stay in the chat; the system messages
them as soon as the payment is verified.

```mermaid
sequenceDiagram
    autonumber
    participant C as Customer
    participant B as Order agent
    participant A as Accountant
    participant J as Deadline job

    C->>B: "yes"
    B-->>C: Order placed, stock reserved. Pay Rs 62,230 to the bank account<br/>(reference ORD-000001) within 24 hours
    C->>B: "paid, TID 784512"
    B->>B: record the claim (amount taken from the order)
    B->>A: notification: verify payment
    B-->>C: Thanks, we're verifying it, we'll message you
    Note over C: customer leaves the chat
    A->>B: verify (checked the bank statement)
    B-->>C: Payment received, order confirmed,<br/>delivered today (paid before 2 PM) or tomorrow
    Note over J: every hour: unpaid after 24 hours and no payment reported?<br/>cancel the order and free the stock
```

```mermaid
stateDiagram-v2
    [*] --> awaiting_payment: customer says yes (stock reserved)
    awaiting_payment --> ready_to_dispatch: accountant verifies the payment
    awaiting_payment --> awaiting_payment: payment rejected, customer sends a new ID
    awaiting_payment --> cancelled: no payment reported within 24 hours (stock released)
    ready_to_dispatch --> [*]: delivery
    cancelled --> [*]
```

## Delivering

Once a payment is verified, the order is queued for its delivery day: today if paid before 2 PM, otherwise
tomorrow. On that day each order goes to a rider, and the shop hears about every step.

```mermaid
flowchart TD
    paid((payment verified)) --> q[queued for its delivery day]
    q --> due{"delivery day<br/>reached?"}
    due -- no --> wait1[waits until that morning]
    due -- yes --> area{"rider from the shop's area<br/>on duty with room?<br/>(max 3 at a time)"}
    area -- yes --> assign
    area -- no --> any{"any rider with room?<br/>(fewest deliveries today first)"}
    any -- yes --> assign["rider assigned<br/>shop gets name + phone"]
    any -- no --> queue["waits in line (oldest first)<br/>dispatcher told"]
    queue -. a rider finishes .-> area
    assign --> pick["rider picks up<br/>shop: on its way"]
    pick --> outcome{at the shop}
    outcome -- delivered --> done[("delivered<br/>stock leaves the warehouse")]
    outcome -- "failed, 1st time" --> retry[try again tomorrow] --> q
    outcome -- "failed, 2nd time" --> ret[("returned<br/>stock released, refund arranged")]
```

```mermaid
stateDiagram-v2
    [*] --> awaiting_payment: customer says yes
    awaiting_payment --> ready_to_dispatch: payment verified
    awaiting_payment --> cancelled: unpaid after 24 hours
    ready_to_dispatch --> out_for_delivery: rider picks up
    out_for_delivery --> delivered: handed over
    out_for_delivery --> ready_to_dispatch: failed, retry tomorrow
    out_for_delivery --> returned: failed twice
    delivered --> [*]
    returned --> [*]
    cancelled --> [*]
```

## Paying suppliers

A supplier is paid only for what we ordered **and** received, and only after a manager approves.

```mermaid
flowchart LR
    recv[goods received<br/>against a purchase order] --> rec["clerk records the<br/>supplier's invoice"]
    rec --> match{"three-way match<br/>order · receipt · invoice"}
    match -- matches --> approve["manager approves<br/>(not the clerk)"]
    match -- "billed more than received" --> flag{manager}
    flag -- "reject: ask for a<br/>corrected invoice" --> rec
    flag -- "approve with a<br/>written reason" --> approve
    approve --> pay["accountant records the payment<br/>(not the approver)"]
    pay --> done[(paid, supplier informed)]
```

Each supplier has payment terms (e.g. 15 days), so every invoice has a due date and overdue bills stand out.

---

## Memory

The assistant keeps track of the conversation at several levels, so customers can talk naturally.

```mermaid
flowchart LR
    subgraph Context["What the assistant knows on every message"]
        P1["Pending<br/>what it is waiting for"]
        P2["Customer history<br/>usual items, last order,<br/>open orders + payment status"]
        P3["Conversation summary<br/>older messages, compressed"]
        P4["Currently discussing<br/>products in focus"]
        P5["Recent messages"]
    end
    CP[("saved conversation state")] --> P1
    CP --> P4
    Orders[("past orders")] --> P2
    Msgs[("message history")] --> P3
    Msgs --> P5
```

| Memory | Lets the customer say |
|---|---|
| Pending action | "yes", "2", "no" |
| Products in focus | "how many bottles in **a carton**?" |
| Conversation summary | long chats without losing earlier details |
| Customer history | "send my **usual order**" |

---

## Data model

```mermaid
erDiagram
    PRICE_LISTS ||--o{ CUSTOMERS : "category A/B/C"
    CUSTOMERS ||--o{ ORDER_DRAFTS : has
    CUSTOMERS ||--o{ ORDERS : places
    ORDER_DRAFTS ||--o| ORDERS : "becomes (on yes)"
    ORDERS ||--|{ ORDER_ITEMS : contains
    ORDERS ||--o| INVOICES : billed_by
    ORDERS ||--o{ PAYMENTS : "paid by"
    ORDERS ||--o{ DELIVERIES : "delivery attempts"
    RIDERS ||--o{ DELIVERIES : carries
    PRODUCTS ||--|| INVENTORY : stocked_as
    PRODUCTS ||--o{ ORDER_ITEMS : "price snapshot"

    PRICE_LISTS {
        string category PK
        numeric discount_pct
    }
    CUSTOMERS {
        int id PK
        string phone UK
        string name
        string price_category FK
        string address
        string area
    }
    PRODUCTS {
        string id PK
        string name
        string pack
        numeric unit_price
        string category
    }
    INVENTORY {
        string product_id PK
        int physical
        int reserved
        int reorder_level
    }
    ORDER_DRAFTS {
        uuid id PK
        jsonb items
        numeric total
        string status
        timestamp expires_at
    }
    ORDERS {
        int id PK
        string order_number UK
        uuid draft_id UK
        numeric total
        string status
        timestamp payment_due_at
        date delivery_date
    }
    DELIVERIES {
        int id PK
        int order_id FK
        int rider_id FK
        int attempt
        string status
        date scheduled_date
    }
    RIDERS {
        int id PK
        string name
        string area
        int capacity
        int active_count
        bool is_on_duty
    }
    PAYMENTS {
        int id PK
        int order_id FK
        numeric amount
        string method
        string reference
        string status
        string reviewed_by
    }
    ORDER_ITEMS {
        int id PK
        int order_id FK
        string product_id FK
        int qty
        numeric unit_price
        numeric line_total
    }
    INVOICES {
        int id PK
        string invoice_number UK
        int order_id UK
    }
```

```mermaid
erDiagram
    SUPPLIERS ||--o{ SUPPLIER_PRODUCTS : offers
    PRODUCTS ||--o{ SUPPLIER_PRODUCTS : "bought from"
    SUPPLIERS ||--o{ PURCHASE_ORDERS : receives
    PURCHASE_ORDERS ||--|{ PURCHASE_ORDER_ITEMS : contains
    PRODUCTS ||--o{ PURCHASE_ORDER_ITEMS : restocks
    ORDERS ||--o{ BACKORDERS : "rest delivered later"
    PURCHASE_ORDERS ||--o{ SUPPLIER_INVOICES : "billed by"

    SUPPLIERS {
        int id PK
        string name UK
        string phone
        int payment_terms_days
    }
    SUPPLIER_INVOICES {
        int id PK
        string invoice_number
        numeric amount
        numeric expected_amount
        string match_status
        date due_date
        string status
    }
    SUPPLIER_PRODUCTS {
        int supplier_id FK
        string product_id FK
        int priority
        numeric unit_cost
        int lead_time_days
        int min_order_qty
    }
    PURCHASE_ORDERS {
        int id PK
        string po_number UK
        string status
        numeric total_cost
        string approved_by
        timestamp expected_at
    }
    PURCHASE_ORDER_ITEMS {
        int purchase_order_id FK
        string product_id FK
        int qty_ordered
        int qty_received
        numeric unit_cost
    }
    BACKORDERS {
        int id PK
        int order_id FK
        string product_id FK
        int qty
        string status
    }
```

Also stored: the full message history, an activity log of every AI decision and business action, and
notifications for staff.

---

## Tech stack

| Area | Technology |
|---|---|
| Language | Python |
| Agent workflow | LangGraph (with conversation state saved in PostgreSQL) |
| AI model | Groq (`openai/gpt-oss-120b`) via LangChain |
| API | FastAPI |
| Database | PostgreSQL on Neon, SQLAlchemy 2.0, Alembic |
| Validation | Pydantic |
| Dashboard | Streamlit (calls the API over HTTP) |

---

## Getting started

**Requirements:** Python 3.10+, a [Neon](https://neon.com) Postgres database and a [Groq](https://console.groq.com) API key.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
cd backend
pip install -r requirements.txt
copy .env.example .env        # add DATABASE_URL and GROQ_API_KEY
alembic upgrade head          # create the tables
python -m app.db.seed         # demo shops and products
```

Chat in the terminal:

```powershell
python -m app.cli --phone "+92 300 1234567"
```

Or run the API (interactive docs at http://127.0.0.1:8000/docs):

```powershell
uvicorn app.api.main:app --reload
```

And the dashboard, in a second terminal from the repository root (opens http://localhost:8501):

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r dashboard/requirements.txt
streamlit run dashboard/app.py
```

**Demo shops:** Ali Traders `+92 300 1234567` (2% discount), Bismillah Store `+92 321 5550192` (3%),
Karachi Mart `+92 345 2201876` (no discount), Madina General Store `+92 312 4478210` (2%).
**Demo riders:** Imran (Saddar), Kashif (Gulshan), Naveed (Tariq Road), Yasir (Liaquatabad), 3 deliveries at a time.
**Demo products:** Dalda oil, Sooper biscuits, three kinds of rice, Nestle and Aquafina water, Tapal tea.

---

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/chat` | send a customer message, get the assistant's replies |
| `GET` | `/conversations/{phone}` | a customer's chat history and what the assistant is waiting for |
| `GET` | `/health` | service status |
| `GET` | `/conversations/{phone}/trace?run_id=` | what the server did for one message: AI calls, tool calls, safety checks |
| `GET` | `/customers/{phone}` | a shop's details and orders (payment, delivery, rider) |
| `GET` | `/staff/inventory` | stock per product: physical, reserved, available, on order, reorder flag |
| `GET` / `POST` | `/staff/notifications` · `/{id}/resolve` | staff notifications |
| `POST` | `/staff/reorder-check` | check all products now and draft purchase orders |
| `GET` | `/staff/purchase-orders?status=draft` | purchase orders, optionally by status |
| `POST` | `/staff/purchase-orders/{po}/approve` · `/reject` · `/send` · `/receive` | the manager and storekeeper steps |
| `GET` | `/staff/payments?status=pending_verification` | customer payments waiting for an accountant |
| `POST` | `/staff/payments/{payment}/verify` · `/reject` | the accountant's decision (the customer is messaged); `{payment}` = payment number, order number or the customer's transaction ID |
| `POST` | `/staff/payments/cancel-unpaid` | run the 24-hour payment deadline now |
| `GET` / `POST` | `/staff/supplier-invoices` | list supplier bills (due, overdue) / record a supplier's invoice |
| `POST` | `/staff/supplier-invoices/{id}/approve` · `/reject` · `/pay` | manager approval and recording the payment |
| `POST` | `/staff/dispatch` | assign today's waiting deliveries to riders now |
| `GET` | `/staff/deliveries` · `/staff/riders` | deliveries by status or date; riders with today's load |
| `GET` | `/riders/{rider}/deliveries` | a rider's current jobs (for a rider app) |
| `POST` | `/riders/{rider}/deliveries/{id}/pickup` · `/delivered` · `/failed` | the rider's steps |
| `POST` | `/riders/{rider}/duty` | start or end a shift |

Scheduled jobs: `python -m app.jobs reorder-check` (daily), `python -m app.jobs payment-deadline` (hourly) and
`python -m app.jobs dispatch` (every morning, then every 15 minutes).

```json
POST /chat
{"phone": "+92 300 1234567", "message": "Send 10 cartons of Dalda oil and 5 cartons of Sooper biscuits"}

{
  "thread_id": "+923001234567",
  "messages": ["Ali Traders, here is your order summary: ... Total: Rs 62,230 ..."],
  "waiting_for": "confirmation",
  "status": "active",
  "order_number": null,
  "invoice_number": null,
  "run_id": "3f9c1a2b7d4e"
}
```

---

## Roadmap

- WhatsApp Business integration
- Phone orders by voice (Vapi)
- Proof of delivery (a code the shop gives the rider) and live rider location
- Payment gateway (automatic verification), cash on delivery and credit limits
- Reports: daily sales, top products, slow payers, rider performance
- Deployment
