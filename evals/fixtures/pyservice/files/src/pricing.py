"""What an order costs."""


def line_total(price, qty):
    return price * qty


def order_total(lines, tax_rate):
    """The order's total including tax, to the cent.

    Each line is {"price": ..., "qty": ...}.
    """
    subtotal = sum(line_total(line["price"], line["qty"]) for line in lines)
    return round(subtotal * (1 + tax_rate), 2)
