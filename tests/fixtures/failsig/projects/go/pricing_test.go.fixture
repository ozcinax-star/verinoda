package orders

import "testing"

func TestTotal(t *testing.T) {
	got := Total([]Item{{Price: 10, Qty: 2}})
	if got != 20 {
		t.Errorf("Total = %v, want 20", got)
	}
}

func TestDiscount(t *testing.T) {
	cases := []struct {
		name     string
		in, want float64
	}{{"above", 200, 180}, {"at", 100, 100}}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := Discount(c.in); got != c.want {
				t.Fatalf("Discount(%v) = %v, want %v", c.in, got, c.want)
			}
		})
	}
}

func TestLast(t *testing.T) {
	if got := Last([]Item{{Price: 1, Qty: 1}}); got.Price != 1 {
		t.Errorf("Last = %v", got)
	}
}

func TestSave(t *testing.T) {
	var r Repo
	r.Save(1, 2.0)
}
