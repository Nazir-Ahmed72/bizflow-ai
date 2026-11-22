"""The tools layer: the ONLY code that touches the database.

READ-ONLY (the LLM may call these directly):
    identify_customer, list_products, search_products, check_stock, suggest_alternatives, get_prices,
    get_order_history
WRITES (only the agent's graph nodes call these):
    create_draft, cancel_draft, confirm_order, generate_invoice, notify_staff
Plumbing: record_message, recent_messages, log_event, log_context
"""
from app.tools.base import ErrorCode, ToolError, log_context, log_event
from app.tools.catalog import (
    check_stock, get_prices, identify_customer, list_products, search_products, suggest_alternatives,
)
from app.tools.notify import ConversationBusy, conversation_lock, notify_staff, recent_messages, record_message
from app.tools.orders import (
    cancel_draft, confirm_order, create_draft, generate_invoice, get_customer_profile, get_order_history,
    idempotency_key_for,
)

__all__ = [
    "identify_customer", "list_products", "search_products", "check_stock", "suggest_alternatives", "get_prices",
    "get_order_history", "get_customer_profile", "create_draft", "cancel_draft", "confirm_order", "generate_invoice", "notify_staff",
    "idempotency_key_for", "record_message", "recent_messages", "conversation_lock", "ConversationBusy",
    "log_event", "log_context",
    "ErrorCode", "ToolError",
]
