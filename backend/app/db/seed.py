"""Seed fixed demo data. Re-runnable.

    python -m app.db.seed           # upsert master data; inventory rows only created if missing
    python -m app.db.seed --reset   # also wipe orders/drafts/invoices/logs/messages and reset stock

Use --reset before running the acceptance scenarios so stock starts at the values below.
"""
from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Customer, Inventory, PriceList, Product, Supplier, SupplierProduct
from app.db.session import session_scope
from app.phone import normalize_phone

PRICE_LISTS = [
    {"category": "A", "discount_pct": Decimal("3.00"), "description": "Category A - 3% discount"},
    {"category": "B", "discount_pct": Decimal("2.00"), "description": "Category B - 2% discount"},
    {"category": "C", "discount_pct": Decimal("0.00"), "description": "Category C - no discount"},
]

CUSTOMERS = [
    ("Ali Traders", "+92 300 1234567", "B", "Shop 4 Saddar"),
    ("Bismillah Store", "+92 321 5550192", "A", "Plot 12 Gulshan Block 5"),
    ("Karachi Mart", "+92 345 2201876", "C", "Main Tariq Road"),
    ("Madina General Store", "+92 312 4478210", "B", "Liaquatabad No. 10"),
]

# id, name, pack, unit, category, unit price Rs, physical, reorder level, search terms
PRODUCTS = [
    ("P-001", "Dalda Cooking Oil 1L", "carton of 12", "carton", "oil", "5400", 140, 40, "dalda oil cooking oil ghee tel"),
    ("P-002", "Sooper Classic biscuits", "carton of 24", "carton", "biscuits", "1900", 95, 30, "sooper biscuit biscuits peek freans"),
    ("P-003", "Basmati Super Kernel 5kg", "bag", "bag", "rice", "2450", 60, 20, "rice basmati super kernel chawal"),
    ("P-004", "Sella Rice 5kg", "bag", "bag", "rice", "2200", 42, 20, "rice sella chawal"),
    ("P-005", "Broken Rice 25kg", "bag", "bag", "rice", "6500", 30, 10, "rice broken tota chawal"),
    ("P-006", "Nestle Pure Life 1.5L", "carton of 6", "carton", "water", "720", 18, 40, "water nestle pure life mineral pani"),
    ("P-007", "Aquafina 1.5L", "carton of 6", "carton", "water", "690", 64, 30, "water aquafina mineral pani"),
    ("P-008", "Tapal Danedar 950g", "carton of 12", "carton", "tea", "14000", 8, 20, "tea tapal danedar chai patti"),
]

MAX_LEVELS = {"P-001": 160, "P-002": 120, "P-003": 80, "P-004": 70, "P-005": 40, "P-006": 80, "P-007": 100,
              "P-008": 40}

SUPPLIERS = [
    ("Karachi Wholesale Co.", "+92 21 3456 7890", 15),
    ("Metro Supply", "+92 21 3111 2222", 7),
    ("Sindh Traders", "+92 22 2765 4321", 30),
]

SUPPLIER_PRODUCTS = [
    ("Karachi Wholesale Co.", "P-001", 1, "4900", 2, 20),
    ("Metro Supply", "P-001", 2, "5000", 1, 10),
    ("Sindh Traders", "P-001", 3, "4750", 5, 50),
    ("Metro Supply", "P-002", 1, "1700", 1, 10),
    ("Karachi Wholesale Co.", "P-002", 2, "1650", 3, 24),
    ("Sindh Traders", "P-003", 1, "2150", 4, 20),
    ("Karachi Wholesale Co.", "P-003", 2, "2250", 2, 10),
    ("Sindh Traders", "P-004", 1, "1950", 4, 20),
    ("Karachi Wholesale Co.", "P-004", 2, "2000", 2, 10),
    ("Sindh Traders", "P-005", 1, "5900", 5, 10),
    ("Metro Supply", "P-006", 1, "600", 1, 20),
    ("Karachi Wholesale Co.", "P-006", 2, "620", 2, 30),
    ("Metro Supply", "P-007", 1, "580", 1, 20),
    ("Karachi Wholesale Co.", "P-007", 2, "600", 2, 30),
    ("Karachi Wholesale Co.", "P-008", 1, "12800", 3, 5),
    ("Metro Supply", "P-008", 2, "13100", 1, 2),
]

TRANSACTIONAL_TABLES = [
    "supplier_invoices", "payments", "backorders", "purchase_order_items", "purchase_orders",
    "invoices", "order_items", "orders", "order_drafts",
    "staff_notifications", "workflow_logs", "messages",
]
# Created by langgraph-checkpoint-postgres (checkpoint_migrations is kept).
CHECKPOINT_TABLES = ["checkpoints", "checkpoint_blobs", "checkpoint_writes"]


def _upsert(session, model, rows, key_cols, update_cols=None):
    stmt = insert(model).values(rows)
    update_cols = update_cols if update_cols is not None else [c for c in rows[0] if c not in key_cols]
    if update_cols:
        stmt = stmt.on_conflict_do_update(
            index_elements=key_cols, set_={c: getattr(stmt.excluded, c) for c in update_cols}
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=key_cols)
    session.execute(stmt)


def seed(reset: bool = False) -> None:
    with session_scope() as s:
        if reset:
            s.execute(text(f"TRUNCATE {', '.join(TRANSACTIONAL_TABLES)} RESTART IDENTITY CASCADE"))
            # LangGraph conversation state refers to the drafts we just deleted: clear it too.
            existing = [t for t in CHECKPOINT_TABLES
                        if s.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None]
            if existing:
                s.execute(text(f"TRUNCATE {', '.join(existing)}"))

        _upsert(s, PriceList, PRICE_LISTS, ["category"])

        _upsert(
            s,
            Customer,
            [
                {"name": n, "phone": normalize_phone(p), "price_category": c, "address": a, "is_active": True}
                for n, p, c, a in CUSTOMERS
            ],
            ["phone"],
        )

        _upsert(
            s,
            Product,
            [
                {"id": pid, "name": name, "pack": pack, "unit": unit, "category": cat,
                 "unit_price": Decimal(price), "search_terms": terms, "is_active": True}
                for pid, name, pack, unit, cat, price, _phys, _reorder, terms in PRODUCTS
            ],
            ["id"],
        )

        inv_rows = [
            {"product_id": pid, "physical": phys, "reserved": 0, "reorder_level": reorder,
             "max_level": MAX_LEVELS[pid]}
            for pid, _n, _p, _u, _c, _pr, phys, reorder, _t in PRODUCTS
        ]
        if reset:
            # Orders are gone, so reservations must go too: back to seed stock.
            _upsert(s, Inventory, inv_rows, ["product_id"])
        else:
            # Keep live stock/reservations; only fill in missing rows + reorder levels.
            _upsert(s, Inventory, inv_rows, ["product_id"], update_cols=["reorder_level", "max_level"])

        _upsert(s, Supplier, [{"name": n, "phone": p, "payment_terms_days": t, "is_active": True}
                         for n, p, t in SUPPLIERS], ["name"])
        supplier_ids = dict(s.execute(text("SELECT name, id FROM suppliers")).all())
        _upsert(s, SupplierProduct, [
            {"supplier_id": supplier_ids[name], "product_id": pid, "priority": prio, "unit_cost": Decimal(cost),
             "lead_time_days": lead, "min_order_qty": moq, "is_active": True}
            for name, pid, prio, cost, lead, moq in SUPPLIER_PRODUCTS
        ], ["supplier_id", "product_id"])

    print(f"Seeded {len(PRICE_LISTS)} price lists, {len(CUSTOMERS)} customers, {len(PRODUCTS)} products, "
          f"{len(SUPPLIERS)} suppliers" + (" (transactional data reset, stock restored)" if reset else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="wipe orders/drafts/logs and restore seed stock")
    seed(reset=parser.parse_args().reset)


if __name__ == "__main__":
    main()
