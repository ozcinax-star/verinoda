const test = require('node:test');
const assert = require('node:assert');
const { total } = require('../src/cart');

test('total adds line totals', () => {
  assert.strictEqual(total([{ price: 2, qty: { value: 3 } }]), 7);
});
