# orders_app

Example service for Verinoda. `orders/api.py` handlers call `orders/service.py`,
which prices orders via `orders/pricing.py` and persists them through
`orders/repository.py` (SQLite, see docs/adr/0001-sqlite-persistence.md).
