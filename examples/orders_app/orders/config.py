"""Runtime configuration read from the environment."""

import os

DATABASE_URL = os.environ.get("ORDERS_DATABASE_URL", "orders.db")
MAX_ITEMS_PER_ORDER = int(os.environ.get("ORDERS_MAX_ITEMS", "50"))
DISCOUNT_THRESHOLD = float(os.environ.get("ORDERS_DISCOUNT_THRESHOLD", "100.0"))


def load_settings() -> dict:
    return {
        "database_url": DATABASE_URL,
        "max_items": MAX_ITEMS_PER_ORDER,
        "discount_threshold": DISCOUNT_THRESHOLD,
    }
