"""Pricing rules."""

from orders.config import DISCOUNT_THRESHOLD


def compute_total(items: list[dict]) -> float:
    subtotal = sum(i["price"] * i["qty"] for i in items)
    return apply_discount(subtotal)


def apply_discount(subtotal: float) -> float:
    # 10% off above the configured threshold.
    if subtotal > DISCOUNT_THRESHOLD:
        return round(subtotal * 0.9, 2)
    return subtotal
