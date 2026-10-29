"""Core data model: products and the stock held for each of them."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class InsufficientStock(Exception):
    """Raised when removing more units than are in stock."""


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit_price: Decimal
    category: str = "general"


class Inventory:
    def __init__(self) -> None:
        self._products: dict[str, Product] = {}
        self._qty: dict[str, int] = {}

    def add(self, product: Product, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._products[product.sku] = product
        self._qty[product.sku] = self._qty.get(product.sku, 0) + quantity

    def remove(self, sku: str, quantity: int) -> None:
        have = self._qty.get(sku, 0)
        if quantity > have:
            raise InsufficientStock(f"{sku}: requested {quantity}, have {have}")
        self._qty[sku] = have - quantity

    def quantity(self, sku: str) -> int:
        return self._qty.get(sku, 0)

    def product(self, sku: str) -> Product:
        return self._products[sku]

    def items(self) -> list[tuple[Product, int]]:
        """(product, quantity) for every SKU ever stocked, in insertion order."""
        return [(self._products[s], q) for s, q in self._qty.items()]
