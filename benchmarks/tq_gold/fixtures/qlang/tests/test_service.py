from app.service import check


def test_check():
    assert check({"a": 1})
