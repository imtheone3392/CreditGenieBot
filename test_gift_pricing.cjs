// Execute the real checkout quote function against known boundary amounts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(__dirname+'/index.html','utf8');
const start = html.indexOf('function giftQuote()');
const end = html.indexOf('function updateGiftQuote()', start);
const inputs = {giftAmount:{value:'400'},giftQuantity:{value:'1'}};
const context = vm.createContext({$:id=>inputs[id],giftPricing:{min_cents:40000,max_cents:100000,max_quantity:50,discount_min_quantity:2,bulk_discount_percent:35,standard_discount_percent:0}});
vm.runInContext(html.slice(start,end),context);
for (const [qty,charge] of [[1,40000],[2,52000],[9,234000],[10,260000],[11,286000],[50,1300000]]) {
  inputs.giftQuantity.value=String(qty);
  assert.equal(context.giftQuote().charged_cents,charge);
}
inputs.giftAmount.value='500'; inputs.giftQuantity.value='2';
assert.equal(context.giftQuote().charged_cents,65000);
inputs.giftAmount.value='1000'; inputs.giftQuantity.value='50';
assert.equal(context.giftQuote().charged_cents,3250000);
inputs.giftAmount.value='400.02'; inputs.giftQuantity.value='11';
assert.equal(context.giftQuote().charged_cents,286014);
for (const qty of ['0','51','1.5','-1','']) {
  inputs.giftQuantity.value=qty;assert.equal(context.giftQuote(),null);
}
inputs.giftQuantity.value='11';
for (const amount of ['399.99','1000.01','400.001','']) {
  inputs.giftAmount.value=amount;assert.equal(context.giftQuote(),null);
}
console.log('Checkout pricing boundaries, rounding and invalid input checks passed.');
