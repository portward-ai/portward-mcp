"""Columns to suggest keeping from Claude at setup (#118): those whose names look personal (email, phone, salary,
address, date of birth and similar).

Names only, never values: setup reads no rows to suggest. A suggestion is shown pre-ticked on the setup page and
applies only when the user confirms there (spec #114: never applied silently). A column the patterns miss is still
the user's to tick, so the patterns lean towards suggesting.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from portward_free.sources import Table

# A word in the name that makes it look personal.
PERSONAL_WORDS = frozenset({
    "email", "emails", "phone", "phones", "telephone", "tel", "mobile", "fax",
    "salary", "salaries", "wage", "wages", "income", "compensation",
    "address", "addresses", "addr", "street", "postcode", "postal", "zip", "zipcode",
    "dob", "birth", "birthdate", "birthday",
    "ssn", "passport", "iban",
})
# Two words in a row that do.
PERSONAL_PAIRS = frozenset({("e", "mail"), ("tax", "id"), ("national", "id"), ("social", "security"),
                            ("credit", "card"), ("card", "number"), ("account", "number")})
# A last word that makes the column about the personal data, not the data itself (email_verified, address_count).
ABOUT_WORDS = frozenset({"verified", "confirmed", "count", "opt", "in", "type", "status", "sent", "domain", "flag",
                         "enabled", "consent"})


def words(name: str) -> list[str]:
    """`customerEmail` -> customer, email; `date_of_birth` -> date, of, birth."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]


def looks_personal(name: str) -> bool:
    found = words(name)
    if not found or found[-1] in ABOUT_WORDS:
        return False
    return any(w in PERSONAL_WORDS for w in found) or any(p in PERSONAL_PAIRS for p in zip(found, found[1:]))


def suggested(tables: Iterable[Table]) -> frozenset[tuple[str, str]]:
    """(table, column) for every column to suggest keeping from Claude, named as the source names them."""
    return frozenset((t.name, c) for t in tables for c in t.columns if looks_personal(c))
