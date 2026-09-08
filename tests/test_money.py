from decimal import Decimal

import pytest

from app.etsy.money import (
    dollars_to_minor,
    is_valid_price_string,
    minor_to_dollars,
    parse_price,
)


def test_parse_price_string():
    assert parse_price("19.99") == Decimal("19.99")


def test_parse_price_float():
    assert parse_price(10) == Decimal("10.00")


def test_parse_price_invalid_raises():
    with pytest.raises(ValueError):
        parse_price("abc")


@pytest.mark.parametrize(
    ("dollars", "minor"),
    [("0.00", 0), ("10.99", 1099), ("1099", 109900), (12.5, 1250), ("121.50", 12150)],
)
def test_dollars_to_minor(dollars, minor):
    assert dollars_to_minor(dollars) == minor


def test_minor_to_dollars():
    assert minor_to_dollars(1999) == "19.99"
    assert minor_to_dollars(100) == "1.00"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10.99", True), ("10.9", True), ("10", True), ("abc", False), ("", False), ("-5", True)],
)
def test_is_valid_price_string(value, expected):
    assert is_valid_price_string(value) is expected
