"""The shop's request handlers."""

from src.validation import validate_order


def create_order(payload):
    """Handle POST /orders."""
    error = validate_order(payload)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "sku": payload["sku"], "qty": payload["qty"]}
