from decimal import Decimal

from inventory import BULK_THRESHOLD, Product, line_total, order_total, to_cents

PEN = Product("P-1", "Pen", Decimal("1.25"))
INK = Product("I-1", "Ink", Decimal("3.35"))


def test_to_cents_half_up():
    assert to_cents(Decimal("2.675")) == Decimal("2.68")
    assert to_cents(Decimal("0.125")) == Decimal("0.13")


def test_to_cents_always_two_places():
    assert str(to_cents(Decimal("5"))) == "5.00"


def test_line_total_below_threshold():
    assert line_total(PEN, 4) == Decimal("5.00")


def test_bulk_discount_applies_at_threshold():
    # 10 x 1.25 = 12.50, minus 10% = 11.25
    assert line_total(PEN, BULK_THRESHOLD) == Decimal("11.25")


def test_bulk_discount_rounds_half_up():
    # 15 x 3.35 = 50.25, minus 10% = 45.225 -> 45.23
    assert line_total(INK, 15) == Decimal("45.23")


def test_order_total():
    assert order_total([(PEN, 4), (INK, 1)]) == Decimal("8.35")
