package billing

import "example.com/goapp/internal/store"

// Invoice prices one stored order.
func Invoice(s *store.Store, id string, bps int64) int64 {
	o, ok := s.Find(id)
	if !ok {
		return 0
	}
	return AddTax(o.Cents, bps)
}
