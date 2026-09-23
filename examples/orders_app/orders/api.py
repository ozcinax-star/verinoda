"""HTTP-style entry points (framework-free handlers)."""

from orders.repository import OrderRepository
from orders.service import ValidationError, fetch_order, place_order

_repo = None


def get_repo() -> OrderRepository:
    global _repo
    if _repo is None:
        _repo = OrderRepository()
    return _repo


def create_order_handler(payload: dict) -> tuple[int, dict]:
    try:
        order_id = place_order(get_repo(), payload["customer"], payload["items"])
    except ValidationError as exc:
        return 400, {"error": str(exc)}
    return 201, {"id": order_id}


def get_order_handler(order_id: int) -> tuple[int, dict]:
    order = fetch_order(get_repo(), order_id)
    if order is None:
        return 404, {"error": "not found"}
    return 200, order
