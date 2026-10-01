// Amounts in cents, so no float rounding reaches an invoice.

export function toCents(amount: number): number {
  return Math.round(amount * 100);
}

export function fromCents(cents: number): number {
  return cents / 100;
}

export function addTax(cents: number, rate: number): number {
  return cents + Math.round(cents * rate);
}
