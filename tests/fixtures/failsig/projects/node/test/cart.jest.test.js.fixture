const { total } = require('../src/cart');

// jest globals: describe, test, expect

describe('cart', () => {
  test('total adds line totals', () => {
    expect(total([{ price: 2, qty: { value: 3 } }])).toBe(7);
  });
});
