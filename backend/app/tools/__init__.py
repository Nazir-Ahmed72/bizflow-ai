"""The tools layer: the ONLY code that touches the database.

READ-ONLY (the LLM may call these directly):
    identify_customer, list_products, search_products, check_stock, suggest_alternatives, get_prices,
    get_order_history, get_restock_eta, get_order_status
WRITES (only the agent's graph nodes, staff endpoints and jobs call these):
    create_draft, cancel_draft, confirm_order, generate_invoice, notify_staff, create_backorders,
    run_reorder_check, approve/reject/send/receive_purchase_order,
    submit_payment, verify_payment, reject_payment, cancel_unpaid_orders,
    record/approve/reject/pay_supplier_invoice
Plumbing: record_message, recent_messages, log_event, log_context, conversation_lock
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
from app.tools.payables import (
    approve_supplier_invoice, list_supplier_invoices, pay_supplier_invoice, record_supplier_invoice,
    reject_supplier_invoice,
)
from app.tools.payments import (
    PAY_TO, cancel_unpaid_orders, get_order_status, list_payments, local_time_text, reject_payment, submit_payment,
    verify_payment,
)
from app.tools.procurement import (
    approve_purchase_order, create_backorders, get_purchase_order, get_restock_eta, list_purchase_orders,
    receive_purchase_order, reject_purchase_order, run_reorder_check, send_purchase_order,
)

__all__ = [
    "identify_customer", "list_products", "search_products", "check_stock", "suggest_alternatives", "get_prices",
    "get_order_history", "get_customer_profile", "get_restock_eta",
    "create_draft", "cancel_draft", "confirm_order", "generate_invoice", "notify_staff", "create_backorders",
    "run_reorder_check", "list_purchase_orders", "get_purchase_order", "approve_purchase_order",
    "reject_purchase_order", "send_purchase_order", "receive_purchase_order",
    "submit_payment", "list_payments", "verify_payment", "reject_payment", "cancel_unpaid_orders", "get_order_status",
    "PAY_TO", "local_time_text",
    "record_supplier_invoice", "approve_supplier_invoice", "reject_supplier_invoice", "pay_supplier_invoice",
    "list_supplier_invoices",
    "idempotency_key_for", "record_message", "recent_messages", "conversation_lock", "ConversationBusy",
    "log_event", "log_context",
    "ErrorCode", "ToolError",
]
