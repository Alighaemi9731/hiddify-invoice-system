"""Public invoice number: 8-digit, deterministic, unique (bijective), non-sequential."""
from app.core.codes import invoice_code


def test_invoice_code_is_8_digits_and_deterministic():
    for i in (1, 2, 99, 12345, 90_000_000 - 1):
        code = invoice_code(i)
        assert code.isdigit() and len(code) == 8
        assert 10_000_000 <= int(code) <= 99_999_999
        assert invoice_code(i) == code  # stable


def test_invoice_code_unique_and_non_sequential():
    codes = [invoice_code(i) for i in range(1, 20_001)]
    assert len(set(codes)) == len(codes)  # no collisions over 20k ids
    # Adjacent ids must NOT produce adjacent numbers (the whole point — hides the count/order).
    assert abs(int(invoice_code(2)) - int(invoice_code(1))) > 1
    assert abs(int(invoice_code(101)) - int(invoice_code(100))) > 1
