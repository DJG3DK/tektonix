"""Turning a list of rows into a summary someone can read."""

from collections import Counter


def total(rows):
    """Sum the `amount` of every row."""
    return sum(r["amount"] for r in rows)


def average(rows):
    """The mean amount across rows."""
    return total(rows) / len(rows)


def by_category(rows):
    """How many rows fall in each category, commonest first."""
    counts = Counter(r.get("category", "other") for r in rows)
    return counts.most_common()


def top_n(rows, n):
    """The `n` largest rows by amount, largest first."""
    return sorted(rows, key=lambda r: r["amount"], reverse=True)[:n]


def summarise(rows):
    """A one-line summary of the whole set."""
    return f"{len(rows)} rows, {total(rows)} total, {average(rows):.2f} average"
