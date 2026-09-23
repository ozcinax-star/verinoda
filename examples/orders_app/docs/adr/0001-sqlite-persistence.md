# ADR 0001: SQLite for order persistence

Status: accepted

We persist orders in SQLite through `OrderRepository` only. No other module
may open a database connection. Chosen for zero-ops local development; a
server database can replace it behind the same repository interface.
