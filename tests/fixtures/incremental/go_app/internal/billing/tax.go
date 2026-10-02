package billing

// AddTax adds a tax rate given in basis points.
func AddTax(cents int64, bps int64) int64 {
	return cents + Round(cents*bps, 10000)
}

// Round divides and rounds half up.
func Round(n int64, d int64) int64 {
	return (n + d/2) / d
}
