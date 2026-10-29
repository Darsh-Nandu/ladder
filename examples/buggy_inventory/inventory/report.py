"""Human-readable stock reports."""
from __future__ import annotations

from .models import Inventory
from .pricing import to_cents


def low_stock(inventory: Inventory, threshold: int) -> list[str]:
    """SKUs with fewer than `threshold` units left, scarcest first (ties by SKU)."""
    rows = [(q, p.sku) for p, q in inventory.items() if q < threshold]
    return [sku for _, sku in sorted(rows)]


def stock_report(inventory: Inventory) -> list[str]:
    """One line per stocked SKU: 'SKU name xQTY $VALUE', most valuable stock first, ties by SKU."""
    rows = [(to_cents(p.unit_price * q), p.sku, p.name, q) for p, q in inventory.items() if q > 0]
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [f"{sku} {name} x{q} ${value}" for value, sku, name, q in rows]
