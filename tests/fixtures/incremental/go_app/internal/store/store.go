// Package store keeps orders in memory.
package store

import "sync"

// Order is one placed order.
type Order struct {
	ID    string
	Cents int64
}

// Store holds the orders.
type Store struct {
	mu     sync.Mutex
	orders map[string]Order
}

// New returns an empty store.
func New() *Store {
	return &Store{orders: map[string]Order{}}
}

// Save keeps an order.
func (s *Store) Save(o Order) Order {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.orders[o.ID] = o
	return o
}

// Find returns an order by id.
func (s *Store) Find(id string) (Order, bool) {
	o, ok := s.orders[id]
	return o, ok
}
