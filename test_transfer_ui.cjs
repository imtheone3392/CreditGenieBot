const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
const code=html.slice(html.indexOf('// Keep an uncertain payment'),html.indexOf('async function startApp()'));
const elements={}, stored=new Map(), calls=[], confirmations=[];
let confirm=true, fail=null;
const $=id=>elements[id] ||= {value:'',disabled:false,textContent:'',innerHTML:'',events:{},querySelectorAll:()=>[],focus(){},addEventListener(name,fn){this.events[name]=fn;}};
const context=vm.createContext({$,currentUser:{member_id:1},crypto:{randomUUID:()=> 'test-request-id'},localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},window:{confirm:text=>{confirmations.push(text);return confirm;}},giftMoney:n=>'$'+(n/100).toFixed(2),esc:s=>String(s).replaceAll('<','&lt;'),prettyTime:s=>s,loadWallet:async()=>{},api:async(path,options)=>{
 if(path.startsWith('/api/transfers/recipient/') || path.startsWith('/api/transfers/resolve?')) return {member_id:2,first_name:'Recipient'};
 if(options && path.endsWith('/decision')) return {status:'paid',transfer_id:99};
 if(options){calls.push(JSON.parse(options.body));if(fail)throw fail;if(path==='/api/payment-requests')return {request:{id:20,status:'pending',amount_cents:calls.at(-1).amount_cents}};return {already_processed:calls.length>1,transfer:{id:10,recipient_id:2,fee_cents:500,amount_cents:calls.at(-1).amount_cents}};}
 return {page:1,total:0,transfers:[],requests:[],members:[]};
}});
vm.runInContext(code,context);
(async()=>{
 context.initializeTransfers(context.currentUser);
 for(const [value,expected] of [['0.01',1],['25.50',2550],['0',null],['-1',null],['1.001',null],['Infinity',null],['9007199254740992',null]]) assert.equal(context.transferCents(value),expected);
 $('transferRecipient').value='2'; await $('findTransferRecipient').events.click();
 assert.equal($('sendTransferBtn').disabled,false);
 $('transferAmount').value='25.50'; confirm=false;
 await $('transferForm').events.submit({preventDefault(){}}); assert.equal(calls.length,0);
 confirm=true;fail=new Error('network timeout');
 await $('transferForm').events.submit({preventDefault(){}});
 assert.equal(calls.length,1);assert.equal(stored.size,1);assert.equal($('transferRecipient').disabled,true);
 assert.equal(calls[0].agreed_fee_cents,500);
 assert.match(confirmations.at(-1),/Merchant fee: \$5.00/);
 assert.match(confirmations.at(-1),/Total deducted: \$30.50/);
 const original=JSON.stringify(calls[0]);
 context.initializeTransfers(context.currentUser); // reopen with an uncertain payment
 assert.equal($('sendTransferBtn').textContent,'RETRY SAME PAYMENT');
 fail=null;await $('transferForm').events.submit({preventDefault(){}});
 assert.equal(JSON.stringify(calls[1]),original);assert.equal(stored.size,0);
 assert.match($('transferStatus').textContent,/Payment confirmed/);
 assert.equal($('sendTransferBtn').disabled,true);
 $('transferRecipient').value='2';await $('findTransferRecipient').events.click();
 $('transferRecipient').value='3';$('transferRecipient').events.input();
 assert.equal($('sendTransferBtn').disabled,true);
 $('transferRecipient').value='@pay_member';await $('findTransferRecipient').events.click();
 assert.equal($('sendTransferBtn').disabled,false);
 $('paymentMode').value='request';$('paymentMode').events.change();
 assert.equal($('sendTransferBtn').textContent,'REVIEW & REQUEST PAYMENT');
 $('transferAmount').value='10';await $('transferForm').events.submit({preventDefault(){}});
 assert.equal(calls.at(-1).payer_id,2);assert.equal(calls.at(-1).recipient_id,undefined);
 assert.match($('transferStatus').textContent,/Request #20 pending/);
 assert.equal(stored.size,0);
 await context.reviewPaymentRequest({id:20,amount_cents:1000,requester_id:2,requester_name:'Test'},'pay');
 assert.match($('paymentRequestStatus').textContent,/paid/);
 assert.match(confirmations.at(-1),/Total deducted: \$15.00/);
 console.log('Username lookup, requests, payer confirmation, payment confirmation, amount validation, persistent retry and recipient-change checks passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
