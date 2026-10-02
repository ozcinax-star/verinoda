import { OrderStore, addTax, fromCents } from "./lib";
import type { Order } from "./lib/store";

const store = new OrderStore();

export function placeOrder(id: string, total: number): Order {
  const order = store.save({ id, total });
  return order;
}

export function invoiceTotal(id: string, rate: number): number {
  const order = store.find(id);
  if (!order) {
    return 0;
  }
  return fromCents(addTax(order.total, rate));
}
