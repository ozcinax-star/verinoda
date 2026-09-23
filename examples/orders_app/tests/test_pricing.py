from orders.pricing import apply_discount, compute_total


def test_discount_applies_above_threshold():
    assert apply_discount(200.0) == 180.0


def test_no_discount_below_threshold():
    assert apply_discount(50.0) == 50.0


def test_compute_total():
    assert compute_total([{"price": 10.0, "qty": 2}]) == 20.0
