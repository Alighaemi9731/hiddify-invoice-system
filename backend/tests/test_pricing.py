"""Tests for Toman -> USDT conversion."""
from decimal import Decimal

from app.services.pricing import toman_to_usdt


def test_basic_conversion():
    assert toman_to_usdt(70_000, 70_000) == Decimal("1.000000")
    assert toman_to_usdt(140_000, 70_000) == Decimal("2.000000")


def test_rounding():
    # 14,245,000 / 70,000 = 203.50
    assert toman_to_usdt(14_245_000, 70_000) == Decimal("203.500000")


def test_zero_rate_is_safe():
    assert toman_to_usdt(1000, 0) == Decimal("0")
