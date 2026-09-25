pub struct Cart {
    pub items: Vec<(f64, u32)>,
}

impl Cart {
    pub fn total(&self) -> f64 {
        self.items.iter().map(|(p, q)| p * (*q as f64)).sum()
    }

    pub fn first_price(&self) -> f64 {
        self.items.first().map(|(p, _)| *p).unwrap()
    }
}

pub fn discount(total: f64) -> f64 {
    if total >= 100.0 {
        return total * 0.9;
    }
    total
}

pub fn checked_qty(q: i64) -> u32 {
    if q < 0 {
        panic!("negative quantity {}", q);
    }
    q as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn total_counts_quantities() {
        let c = Cart { items: vec![(10.0, 2)] };
        assert_eq!(c.total(), 21.0);
    }

    #[test]
    fn empty_cart_first_price() {
        let c = Cart { items: vec![] };
        assert_eq!(c.first_price(), 0.0);
    }

    #[test]
    fn negative_quantity_is_rejected() {
        assert_eq!(checked_qty(-1), 0);
    }
}
