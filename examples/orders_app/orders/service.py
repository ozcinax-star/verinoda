"""Business logic between the HTTP layer and the repository."""

from orders.config import MAX_ITEMS_PER_ORDER
from orders.pricing import compute_total
from orders.repository import OrderRepository


class ValidationError(ValueError):
    pass


def validate_items(items: list[dict]) -> None:
    if not items:
        raise ValidationError("order has no items")
    if len(items) > MAX_ITEMS_PER_ORDER:
        raise ValidationError("too many items")


def place_order(repo: OrderRepository, customer: str, items: list[dict]) -> int:
    validate_items(items)
    total = compute_total(items)
    return repo.save(customer, total)


def fetch_order(repo: OrderRepository, order_id: int):
    return repo.get(order_id)
