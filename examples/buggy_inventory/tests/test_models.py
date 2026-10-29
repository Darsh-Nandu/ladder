from decimal import Decimal

import pytest

from inventory import InsufficientStock, Inventory, Product

WIDGET = Product("W-1", "Widget", Decimal("2.50"))


def test_add_accumulates():
    inv = Inventory()
    inv.add(WIDGET, 3)
    inv.add(WIDGET, 4)
    assert inv.quantity("W-1") == 7


def test_remove_and_insufficient_stock():
    inv = Inventory()
    inv.add(WIDGET, 5)
    inv.remove("W-1", 2)
    assert inv.quantity("W-1") == 3
    with pytest.raises(InsufficientStock):
        inv.remove("W-1", 4)


def test_add_rejects_non_positive():
    with pytest.raises(ValueError):
        Inventory().add(WIDGET, 0)
