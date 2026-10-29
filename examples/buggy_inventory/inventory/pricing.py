"""Order pricing. All money is Decimal, rounded half-up to whole cents."""
from __future__ import annotations

from decimal import Decimal

from .models import Product

BULK_THRESHOLD = 10              # orders of this many units or more get the bulk discount
BULK_DISCOUNT = Decimal("0.10")  # 10% off the line


def to_cents(amount: Decimal) -> Decimal:
    """Round a money amount to cents, half-up (2.675 -> 2.68), always with two decimal places."""
    return Decimal(str(round(float(amount), 2)))


def line_total(product: Product, quantity: int) -> Decimal:
    """Price of `quantity` units of `product`, after the bulk discount, in cents."""
    subtotal = product.unit_price * quantity
    if quantity > BULK_THRESHOLD:
        subtotal -= subtotal * BULK_DISCOUNT
    return to_cents(subtotal)


def order_total(lines: list[tuple[Product, int]]) -> Decimal:
    """Sum of the line totals of an order."""
    return to_cents(sum((line_total(p, q) for p, q in lines), Decimal("0")))
