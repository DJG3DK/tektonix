"""Turning titles into URL slugs."""

import re
import unicodedata


def slugify(title):
    """A URL-safe slug for `title`.

    Accents are folded to plain letters, everything is lower-cased, any run
    of characters that is not a letter or digit becomes a single hyphen, and
    no slug starts or ends with a hyphen. An empty or all-punctuation title
    gives an empty string.
    """
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    lowered = folded.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    return hyphenated.strip("-")
