function lineTotal(item) {
  return item.price * item.qty.value;
}

function total(items) {
  return items.reduce((sum, item) => sum + lineTotal(item), 0);
}

module.exports = { lineTotal, total };
