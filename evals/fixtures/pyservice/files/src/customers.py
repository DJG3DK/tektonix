"""Customer records."""


def dedupe(customers):
    """Customers with duplicate emails removed.

    Emails compare case-insensitively; the FIRST record for an address is
    kept, and the order of the survivors is the input order.
    """
    out = []
    for c in customers:
        if not any(o["email"].lower() == c["email"].lower() for o in out):
            out.append(c)
    return out
