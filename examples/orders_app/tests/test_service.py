import pytest

from orders.repository import OrderRepository
from orders.service import ValidationError, fetch_order, place_order


def test_place_and_fetch_roundtrip():
    repo = OrderRepository(":memory:")
    oid = place_order(repo, "ada", [{"price": 5.0, "qty": 1}])
    assert fetch_order(repo, oid)["total"] == 5.0


def test_empty_order_rejected():
    with pytest.raises(ValidationError):
        place_order(OrderRepository(":memory:"), "ada", [])
