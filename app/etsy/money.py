from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# The Etsy Open API v3 works in minor units (sub-units) of the shop currency by
# default — e.g. $10.99 is sent as 1099. Inventory offerings have the same
# convention. The UI works in dollars, the API in minor units; these helpers
# convert at the boundary.

_MINOR_UNITS = 100


def parse_price(dollars: str | int | float | Decimal) -> Decimal:
    """Parse a user-facing price string/float into a Decimal (major units)."""
    if isinstance(dollars, Decimal):
        return dollars
    if isinstance(dollars, (int, float)):
        dollars = f"{dollars:.2f}"
    dollars = dollars.strip().replace(",", "")
    try:
        return Decimal(dollars).quantize(Decimal("0.01"))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid price: {dollars!r}") from exc


def dollars_to_minor(dollars: str | int | float | Decimal) -> int:
    """Convert user price (major units) to Etsy minor units (pennies)."""
    amount = parse_price(dollars)
    return int((amount * _MINOR_UNITS).to_integral_value())


def minor_to_dollars(minor: int | float) -> str:
    """Convert Etsy minor units to a user-facing dollar string."""
    return f"{Decimal(minor) / _MINOR_UNITS:.2f}"

_PRICE_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")


def is_valid_price_string(value: str) -> bool:
    return bool(_PRICE_RE.match(value.strip()))
