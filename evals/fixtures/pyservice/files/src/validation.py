"""Checking an incoming order before it is placed."""


def validate_order(payload):
    """None if the order is valid, otherwise a message saying what is wrong."""
    sku = payload.get("sku")
    if not isinstance(sku, str) or not sku.strip():
        return "sku is required"
    qty = payload.get("qty")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        return "qty must be a positive integer"
    coupon = payload.get("coupon")
    if coupon is not None and not isinstance(coupon, str):
        return "coupon must be a string"
    return None
