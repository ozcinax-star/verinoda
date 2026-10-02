import { toCents } from "./money";

export interface Order {
  id: string;
  total: number;
}

export class BaseStore {
  protected items: Order[] = [];

  count(): number {
    return this.items.length;
  }
}

export class OrderStore extends BaseStore {
  save(order: Order): Order {
    const saved = { ...order, total: toCents(order.total) };
    this.items.push(saved);
    return saved;
  }

  find(id: string): Order | undefined {
    return this.items.find((o) => o.id === id);
  }
}
