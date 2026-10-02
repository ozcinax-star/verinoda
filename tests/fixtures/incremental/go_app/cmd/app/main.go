package main

import (
	"fmt"

	"example.com/goapp/internal/billing"
	"example.com/goapp/internal/store"
)

func main() {
	s := store.New()
	s.Save(store.Order{ID: "a1", Cents: 1250})
	fmt.Println(billing.Invoice(s, "a1", 2000))
}

func report(s *store.Store, ids []string) []int64 {
	out := make([]int64, 0, len(ids))
	for _, id := range ids {
		out = append(out, billing.Invoice(s, id, 2000))
	}
	return out
}
