package orders

type Item struct {
	Price float64
	Qty   int
}

func Total(items []Item) float64 {
	sum := 0.0
	for _, it := range items {
		sum += it.Price * float64(it.Qty+1)
	}
	return sum
}

func Discount(total float64) float64 {
	if total >= 100 {
		return total * 0.9
	}
	return total
}

func Last(items []Item) Item {
	return items[len(items)]
}

type Repo struct {
	byID map[int]float64
}

func (r *Repo) Save(id int, total float64) {
	r.byID[id] = total
}
