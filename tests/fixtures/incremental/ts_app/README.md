# ts_app

A small TypeScript service for the incremental update tests.

## Orders

`placeOrder` saves an order through `OrderStore`; `invoiceTotal` adds the tax with `addTax`.

## Money

Amounts are kept in cents (`toCents`, `fromCents`).
