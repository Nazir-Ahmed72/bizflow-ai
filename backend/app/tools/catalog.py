"""Read-only tools: identify_customer, search_products, check_stock, suggest_alternatives, get_prices.

compute_pricing() is the ONE place money is calculated in the whole system.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional, Union

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Customer, Inventory, PriceList, Product
from app.db.session import session_scope
from app.phone import normalize_phone
from app.tools.base import (
    ErrorCode, OrderLineIn, OrderLines, PricedLine, Pricing, ToolError, ToolOk, fail, tool,
)

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# =========================================================================== identify_customer

class IdentifyCustomerInput(BaseModel):
    phone: str = Field(min_length=1, max_length=40)


class IdentifyCustomerOutput(ToolOk):
    customer_id: int
    name: str
    phone: str
    price_category: str
    discount_pct: Decimal
    address: str


@tool(IdentifyCustomerInput)
def identify_customer(p: IdentifyCustomerInput) -> Union[IdentifyCustomerOutput, ToolError]:
    """Look up a registered, active customer by phone number."""
    try:
        phone = normalize_phone(p.phone)
    except ValueError:
        return fail(ErrorCode.INVALID_PHONE, f"'{p.phone}' is not a valid phone number.")
    with session_scope() as s:
        row = s.execute(
            select(Customer, PriceList.discount_pct)
            .join(PriceList, PriceList.category == Customer.price_category)
            .where(Customer.phone == phone, Customer.is_active.is_(True))
        ).first()
        if row is None:
            return fail(ErrorCode.CUSTOMER_NOT_FOUND, f"No registered customer with phone {phone}.", phone=phone)
        c, pct = row
        return IdentifyCustomerOutput(customer_id=c.id, name=c.name, phone=c.phone,
                                      price_category=c.price_category, discount_pct=pct, address=c.address)


# =========================================================================== search_products

_TOKEN_RE = re.compile(r"[a-z0-9.]+")


def _tokens(text: str) -> set[str]:
    out = set()
    for tok in _TOKEN_RE.findall(text.lower()):
        tok = tok.strip(".")
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]  # biscuits -> biscuit
        if tok:
            out.add(tok)
    return out


class SearchProductsInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class ProductMatch(BaseModel):
    product_id: str
    name: str
    pack: str
    unit: str
    category: str
    unit_price: Decimal


class SearchProductsOutput(ToolOk):
    query: str
    matches: list[ProductMatch]
    ambiguous: bool  # several variants match equally well -> ask the customer


@tool(SearchProductsInput)
def search_products(p: SearchProductsInput) -> Union[SearchProductsOutput, ToolError]:
    """Find products for ONE product phrase ('Dalda oil', 'rice', 'nestle water 1.5L', 'chai').

    Returns only the best-scoring products (word overlap with name, category and search terms).
    No match -> success with an empty list.
    """
    q = _tokens(p.query)
    with session_scope() as s:
        products = list(s.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.id)))
    scored = [(len(q & _tokens(f"{pr.name} {pr.category} {pr.search_terms}")), pr) for pr in products]
    best = max((sc for sc, _ in scored), default=0)
    matches = [ProductMatch(product_id=pr.id, name=pr.name, pack=pr.pack, unit=pr.unit, category=pr.category,
                            unit_price=pr.unit_price)
               for sc, pr in scored if best and sc == best]
    return SearchProductsOutput(query=p.query, matches=matches, ambiguous=len(matches) > 1)


# =========================================================================== list_products

class ListProductsInput(BaseModel):
    category: Optional[str] = Field(default=None, max_length=40)


class CatalogItem(BaseModel):
    product_id: str
    name: str
    pack: str  # sold only in whole packs: "carton of 12", "bag"
    category: str
    unit_price: Decimal  # price per pack
    available: int  # packs available now
    in_stock: bool


class ListProductsOutput(ToolOk):
    count: int
    products: list[CatalogItem]


@tool(ListProductsInput)
def list_products(p: ListProductsInput) -> Union[ListProductsOutput, ToolError]:
    """The whole catalog (optionally one category) with pack, price per pack and stock available."""
    with session_scope() as s:
        q = (select(Product, Inventory).join(Inventory, Inventory.product_id == Product.id)
             .where(Product.is_active.is_(True)).order_by(Product.id))
        if p.category:
            q = q.where(Product.category == p.category.strip().lower())
        rows = s.execute(q).all()
    items = [CatalogItem(product_id=pr.id, name=pr.name, pack=pr.pack, category=pr.category, unit_price=pr.unit_price,
                         available=inv.physical - inv.reserved, in_stock=inv.physical - inv.reserved > 0)
             for pr, inv in rows]
    return ListProductsOutput(count=len(items), products=items)


# =========================================================================== check_stock

class CheckStockInput(BaseModel):
    items: OrderLines


class StockLine(BaseModel):
    product_id: str
    name: str
    requested: int
    available: int
    sufficient: bool
    shortfall: int


class CheckStockOutput(ToolOk):
    items: list[StockLine]
    all_available: bool


@tool(CheckStockInput)
def check_stock(p: CheckStockInput) -> Union[CheckStockOutput, ToolError]:
    """Available = physical - reserved, per requested line. Read-only (confirm_order reserves)."""
    ids = [i.product_id for i in p.items]
    with session_scope() as s:
        rows = {prod.id: (prod, inv) for prod, inv in s.execute(
            select(Product, Inventory).join(Inventory, Inventory.product_id == Product.id)
            .where(Product.id.in_(ids), Product.is_active.is_(True)))}
    missing = [pid for pid in ids if pid not in rows]
    if missing:
        return fail(ErrorCode.UNKNOWN_PRODUCT, f"Unknown product(s): {', '.join(missing)}.", product_ids=missing)
    lines = []
    for item in p.items:
        prod, inv = rows[item.product_id]
        available = inv.physical - inv.reserved
        lines.append(StockLine(product_id=prod.id, name=prod.name, requested=item.qty, available=available,
                               sufficient=available >= item.qty, shortfall=max(0, item.qty - available)))
    return CheckStockOutput(items=lines, all_available=all(line.sufficient for line in lines))


# =========================================================================== suggest_alternatives

class SuggestAlternativesInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=10)
    qty_needed: int = Field(gt=0, le=10_000)

    @field_validator("product_id")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().upper()


class Alternative(BaseModel):
    product_id: str
    name: str
    pack: str
    unit_price: Decimal
    available: int
    can_cover: bool


class SuggestAlternativesOutput(ToolOk):
    product_id: str
    name: str
    available_of_requested: int
    qty_needed: int
    alternatives: list[Alternative]  # same category, in stock; full cover first, then closest price


@tool(SuggestAlternativesInput)
def suggest_alternatives(p: SuggestAlternativesInput) -> Union[SuggestAlternativesOutput, ToolError]:
    """Substitutes for a short product: same category, other products with stock."""
    with session_scope() as s:
        row = s.execute(select(Product, Inventory).join(Inventory, Inventory.product_id == Product.id)
                        .where(Product.id == p.product_id, Product.is_active.is_(True))).first()
        if row is None:
            return fail(ErrorCode.UNKNOWN_PRODUCT, f"Unknown product: {p.product_id}.", product_ids=[p.product_id])
        prod, inv = row
        candidates = s.execute(
            select(Product, Inventory).join(Inventory, Inventory.product_id == Product.id)
            .where(Product.category == prod.category, Product.id != prod.id, Product.is_active.is_(True),
                   Inventory.physical - Inventory.reserved > 0)).all()
    alts = [Alternative(product_id=c.id, name=c.name, pack=c.pack, unit_price=c.unit_price,
                        available=ci.physical - ci.reserved, can_cover=ci.physical - ci.reserved >= p.qty_needed)
            for c, ci in candidates]
    alts.sort(key=lambda a: (not a.can_cover, abs(a.unit_price - prod.unit_price), a.product_id))
    return SuggestAlternativesOutput(product_id=prod.id, name=prod.name,
                                     available_of_requested=inv.physical - inv.reserved,
                                     qty_needed=p.qty_needed, alternatives=alts)


# =========================================================================== pricing

def compute_pricing(session: Session, customer_id: int, lines: Iterable[OrderLineIn]) -> Union[Pricing, ToolError]:
    """subtotal = sum(qty * unit_price); discount = subtotal * category pct (half-up to paisa)."""
    lines = list(lines)
    row = session.execute(
        select(Customer, PriceList.discount_pct).join(PriceList, PriceList.category == Customer.price_category)
        .where(Customer.id == customer_id, Customer.is_active.is_(True))).first()
    if row is None:
        return fail(ErrorCode.CUSTOMER_NOT_FOUND, f"No active customer with id {customer_id}.")
    _customer, discount_pct = row
    ids = [line.product_id for line in lines]
    products = {pr.id: pr for pr in session.scalars(
        select(Product).where(Product.id.in_(ids), Product.is_active.is_(True)))}
    missing = [pid for pid in ids if pid not in products]
    if missing:
        return fail(ErrorCode.UNKNOWN_PRODUCT, f"Unknown product(s): {', '.join(missing)}.", product_ids=missing)
    priced = [PricedLine(product_id=pr.id, name=pr.name, pack=pr.pack, unit=pr.unit, qty=line.qty,
                         unit_price=money(pr.unit_price), line_total=money(pr.unit_price * line.qty))
              for line in lines for pr in [products[line.product_id]]]
    subtotal = money(sum((pl.line_total for pl in priced), Decimal("0")))
    discount_amount = money(subtotal * discount_pct / Decimal(100))
    return Pricing(lines=priced, subtotal=subtotal, discount_pct=money(discount_pct),
                   discount_amount=discount_amount, total=money(subtotal - discount_amount))


class GetPricesInput(BaseModel):
    customer_id: int = Field(gt=0)
    items: OrderLines


class GetPricesOutput(ToolOk, Pricing):
    customer_id: int


@tool(GetPricesInput)
def get_prices(p: GetPricesInput) -> Union[GetPricesOutput, ToolError]:
    """Price items for a customer: lines, subtotal, discount (by price category), total."""
    with session_scope() as s:
        result = compute_pricing(s, p.customer_id, p.items)
    if isinstance(result, ToolError):
        return result
    return GetPricesOutput(customer_id=p.customer_id, **result.model_dump())
