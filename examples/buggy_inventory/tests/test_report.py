from decimal import Decimal

from inventory import Inventory, Product, low_stock, stock_report


def make_inventory() -> Inventory:
    inv = Inventory()
    inv.add(Product("B-2", "Bolt", Decimal("0.40")), 50)     # 20.00
    inv.add(Product("A-1", "Anchor", Decimal("10.00")), 2)   # 20.00
    inv.add(Product("C-3", "Clamp", Decimal("7.25")), 4)     # 29.00
    inv.add(Product("D-4", "Drill", Decimal("89.99")), 1)    # 89.99
    return inv


def test_stock_report_most_valuable_first():
    lines = stock_report(make_inventory())
    assert lines[0] == "D-4 Drill x1 $89.99"
    assert lines[1] == "C-3 Clamp x4 $29.00"


def test_stock_report_ties_by_sku():
    lines = stock_report(make_inventory())
    assert [ln.split()[0] for ln in lines[2:]] == ["A-1", "B-2"]


def test_stock_report_skips_empty():
    inv = make_inventory()
    inv.remove("D-4", 1)
    assert all(not ln.startswith("D-4") for ln in stock_report(inv))


def test_low_stock_scarcest_first():
    assert low_stock(make_inventory(), 5) == ["D-4", "A-1", "C-3"]
