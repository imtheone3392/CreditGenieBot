const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
const code=html.slice(html.indexOf('// Keep an uncertain payment'),html.indexOf('async function startApp()'));
const elements={}, stored=new Map(), calls=[];
let confirm=true, fail=null;
const $=id=>elements[id] ||= {value:'',disabled:false,textContent:'',innerHTML:'',events:{},addEventListener(name,fn){this.events[name]=fn;}};
const context=vm.createContext({$,currentUser:{member_id:1},crypto:{randomUUID:()=> 'test-request-id'},localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},window:{confirm:()=>confirm},giftMoney:n=>'$'+(n/100).toFixed(2),esc:s=>String(s).replaceAll('<','&lt;'),prettyTime:s=>s,loadWallet:async()=>{},api:async(path,options)=>{
 if(path.startsWith('/api/transfers/recipient/')) return {member_id:2,first_name:'Recipient'};
 if(options){calls.push(JSON.parse(options.body));if(fail)throw fail;return {already_processed:calls.length>1,transfer:{id:10,recipient_id:2,amount_cents:calls.at(-1).amount_cents}};}
 return {page:1,total:0,transfers:[]};
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
 console.log('Payment confirmation, amount validation, persistent retry and recipient-change checks passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
