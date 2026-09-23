"""Which orders belong to which day."""

from datetime import datetime


def orders_on(orders, day, utc_offset_hours=0):
    """The orders placed on `day` (a date) in the shop's own time zone.

    Each order has "placed_at", an ISO-8601 timestamp with a UTC offset. The
    shop's time zone is UTC + `utc_offset_hours`.
    """
    return [o for o in orders if datetime.fromisoformat(o["placed_at"]).date() == day]
