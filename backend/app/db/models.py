"""SQLAlchemy 2.0 models. Money is Numeric(12,2) / Decimal everywhere, never float."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(12, 2)


class Base(DeclarativeBase):
    pass


def _now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- master data

class PriceList(Base):
    """Discount per customer price category (A/B/C)."""

    __tablename__ = "price_lists"
    __table_args__ = (
        CheckConstraint("category IN ('A','B','C')", name="ck_price_lists_category"),
        CheckConstraint("discount_pct >= 0 AND discount_pct <= 100", name="ck_price_lists_discount_range"),
    )

    category: Mapped[str] = mapped_column(String(1), primary_key=True)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(100))


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)  # E.164, see app.phone
    price_category: Mapped[str] = mapped_column(ForeignKey("price_lists.category"), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = _now_col()

    price_list: Mapped[PriceList] = relationship()


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("unit_price > 0", name="ck_products_unit_price_positive"),)

    id: Mapped[str] = mapped_column(String(10), primary_key=True)  # e.g. P-001
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    pack: Mapped[str] = mapped_column(String(60), nullable=False)  # "carton of 12", "bag"
    unit: Mapped[str] = mapped_column(String(20), nullable=False)  # what qty counts: carton / bag
    category: Mapped[str] = mapped_column(String(40), nullable=False)  # oil, biscuits, rice, water, tea
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)  # Rs per pack
    search_terms: Mapped[str] = mapped_column(Text, nullable=False, server_default="")  # extra keywords incl. Urdu/Roman Urdu
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    inventory: Mapped["Inventory"] = relationship(back_populates="product", uselist=False)


class Inventory(Base):
    """available = physical - reserved."""

    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("physical >= 0", name="ck_inventory_physical_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_inventory_reserved_nonneg"),
        CheckConstraint("reserved <= physical", name="ck_inventory_reserved_le_physical"),
        CheckConstraint("reorder_level >= 0", name="ck_inventory_reorder_nonneg"),
        CheckConstraint("max_level >= reorder_level", name="ck_inventory_max_ge_reorder"),
    )

    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    physical: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reorder_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    product: Mapped[Product] = relationship(back_populates="inventory")

    @property
    def available(self) -> int:
        return self.physical - self.reserved


# --------------------------------------------------------------------------- ordering

DRAFT_STATUSES = ("pending", "confirmed", "cancelled", "expired")
ORDER_STATUSES = ("confirmed", "cancelled", "fulfilled")


class OrderDraft(Base):
    """A priced proposal shown to the customer. Not an order until confirm_order succeeds."""

    __tablename__ = "order_drafts"
    __table_args__ = (
        CheckConstraint(f"status IN {DRAFT_STATUSES}", name="ck_order_drafts_status"),
        CheckConstraint("subtotal >= 0 AND discount_amount >= 0 AND total >= 0", name="ck_order_drafts_amounts_nonneg"),
        CheckConstraint("discount_pct >= 0 AND discount_pct <= 100", name="ck_order_drafts_discount_range"),
        Index("ix_order_drafts_customer_status", "customer_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    # [{product_id, name, qty, unit_price, line_total}] - prices as strings (Decimal-safe)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False, server_default="en")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    created_at: Mapped[datetime] = _now_col()
    summary_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # summary_sent_at + 2h

    customer: Mapped[Customer] = relationship()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"status IN {ORDER_STATUSES}", name="ck_orders_status"),
        CheckConstraint("subtotal >= 0 AND discount_amount >= 0 AND total >= 0", name="ck_orders_amounts_nonneg"),
        CheckConstraint("discount_pct >= 0 AND discount_pct <= 100", name="ck_orders_discount_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    draft_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_drafts.id"), nullable=False, unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="confirmed")
    created_at: Mapped[datetime] = _now_col()

    customer: Mapped[Customer] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    invoice: Mapped[Optional["Invoice"]] = relationship(back_populates="order", uselist=False)


class OrderItem(Base):
    """Price snapshot at confirmation time."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_order_items_qty_positive"),
        CheckConstraint("unit_price > 0", name="ck_order_items_unit_price_positive"),
        CheckConstraint("line_total = qty * unit_price", name="ck_order_items_line_total"),
        UniqueConstraint("order_id", "product_id", name="uq_order_items_order_product"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Money, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("subtotal >= 0 AND discount_amount >= 0 AND total >= 0", name="ck_invoices_amounts_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, unique=True)
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    issued_at: Mapped[datetime] = _now_col()

    order: Mapped[Order] = relationship(back_populates="invoice")


# --------------------------------------------------------------------------- ops / audit

class StaffNotification(Base):
    """Written by notify_staff (e.g. unknown phone, stock problem)."""

    __tablename__ = "staff_notifications"
    __table_args__ = (CheckConstraint("status IN ('open','resolved')", name="ck_staff_notifications_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False)  # sales, warehouse, manager
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="open")
    created_at: Mapped[datetime] = _now_col()


class WorkflowLog(Base):
    """Every LLM call, tool call, guard check and node step, with timing."""

    __tablename__ = "workflow_logs"
    __table_args__ = (
        CheckConstraint("kind IN ('node','llm','tool','guard')", name="ck_workflow_logs_kind"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_workflow_logs_duration_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64))  # one id per inbound message
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    step: Mapped[str] = mapped_column(String(80), nullable=False)  # node / tool / guard name
    input: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    output: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    error_code: Mapped[Optional[str]] = mapped_column(String(60))
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now_col()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("direction IN ('in','out')", name="ck_messages_direction"),
        Index("ix_messages_thread_created", "thread_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"))
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now_col()


PO_STATUSES = ("draft", "approved", "rejected", "sent", "partially_received", "received", "cancelled")
OPEN_PO_STATUSES = ("draft", "approved", "sent", "partially_received")
BACKORDER_STATUSES = ("waiting", "ready", "cancelled")


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = _now_col()


class SupplierProduct(Base):
    __tablename__ = "supplier_products"
    __table_args__ = (
        UniqueConstraint("supplier_id", "product_id", name="uq_supplier_products_supplier_product"),
        UniqueConstraint("product_id", "priority", name="uq_supplier_products_product_priority"),
        CheckConstraint("unit_cost > 0", name="ck_supplier_products_cost_positive"),
        CheckConstraint("lead_time_days > 0", name="ck_supplier_products_lead_time_positive"),
        CheckConstraint("min_order_qty >= 1", name="ck_supplier_products_moq_positive"),
        CheckConstraint("priority >= 1", name="ck_supplier_products_priority_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    min_order_qty: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    supplier: Mapped[Supplier] = relationship()


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint(f"status IN {PO_STATUSES}", name="ck_purchase_orders_status"),
        CheckConstraint("total_cost >= 0", name="ck_purchase_orders_total_nonneg"),
        Index("ix_purchase_orders_supplier_status", "supplier_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    created_at: Mapped[datetime] = _now_col()
    approved_by: Mapped[Optional[str]] = mapped_column(String(80))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[Optional[str]] = mapped_column(String(80))
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    supplier: Mapped[Supplier] = relationship()
    items: Mapped[list["PurchaseOrderItem"]] = relationship(back_populates="purchase_order",
                                                             cascade="all, delete-orphan")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "product_id", name="uq_po_items_po_product"),
        CheckConstraint("qty_ordered > 0", name="ck_po_items_qty_positive"),
        CheckConstraint("qty_received >= 0 AND qty_received <= qty_ordered", name="ck_po_items_received_range"),
        CheckConstraint("line_total = qty_ordered * unit_cost", name="ck_po_items_line_total"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"),
                                                   nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty_ordered: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_received: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unit_cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Money, nullable=False)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="items")


class Backorder(Base):
    __tablename__ = "backorders"
    __table_args__ = (
        CheckConstraint(f"status IN {BACKORDER_STATUSES}", name="ck_backorders_status"),
        CheckConstraint("qty > 0", name="ck_backorders_qty_positive"),
        Index("ix_backorders_product_status", "product_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="waiting")
    created_at: Mapped[datetime] = _now_col()
    ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
