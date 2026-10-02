const { placeOrder, invoiceTotal } = require("./service");

function main() {
  placeOrder("a1", 12.5);
  console.log(invoiceTotal("a1", 0.2));
}

function report(ids) {
  return ids.map((id) => invoiceTotal(id, 0.2));
}

module.exports = { main, report };
