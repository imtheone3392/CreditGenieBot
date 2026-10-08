const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
const code=html.slice(html.indexOf('/* Consent-based manual Bitcoin withdrawals. */'),html.indexOf('async function startApp()'));
const elements={},storage=new Map(),posts=[],prompts=[];
let confirm=true,fail=false;
let row={id:1,user_id:2,amount_cents:2000,btc_satoshis:20000,status:'requested',request_note:'Note',updated_at:'now'};
const $=id=>elements[id] ||= {value:'',textContent:'',innerHTML:'',events:{},contains:()=>false,scrollIntoView(){},addEventListener(k,v){this.events[k]=v;}};
const member={id:2,first_name:'Test Member',balance_cents:3000,reserved_cents:0};
const context=vm.createContext({$,adminEnabled:true,currentUser:{member_id:900},document:{activeElement:null},window:{confirm:s=>{prompts.push(s);return confirm;}},crypto:{randomUUID:()=> 'fixed-reference'},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},giftMoney:n=>'$'+(n/100).toFixed(2),esc:s=>String(s).replaceAll('<','&lt;').replaceAll('"','&quot;'),prettyTime:s=>s,transferCents:s=>/^\d+(\.\d{1,2})?$/.test(s)?Math.round(Number(s)*100):null,loadWallet:async()=>{},loadAdminMembers:async()=>{},api:async(path,options)=>{
 if(options){const payload=JSON.parse(options.body);posts.push({path,payload});if(fail)throw new Error('Network timeout');if(path==='/api/admin/withdrawals')return {withdrawal:row,notification_sent:true};row={...row,status:({accept:'accepted',approve:'approved',paid:'paid',reject:'rejected',decline:'declined'})[payload.action],approval_note:payload.note || '',btc_address:payload.btc_address || row.btc_address};return {withdrawal:row,notification_sent:true};}
 if(path.startsWith('/api/admin/members'))return {members:[member],page:1,total:1};
 return {withdrawals:[row],page:1,total:1,reserved_cents:row.status==='accepted'?2000:0};
}});
vm.runInContext(code,context);
(async()=>{
 for(const [value,expected] of [['0.00000001',1],['0.001',100000],['0',null],['-1',null],['1.000000001',null],['abc',null]])assert.equal(context.btcSatoshis(value),expected);
 await context.loadWithdrawalMembers();assert.match($('withdrawalMembers').innerHTML,/Available: \$30.00/);
 await $('withdrawalMembers').events.click({target:{closest:()=>({dataset:{withdrawMember:'2'}})}});
 $('withdrawalUsd').value='20';$('withdrawalBtc').value='0.00020000';$('withdrawalRequestNote').value='Note';
 confirm=false;await $('withdrawalCreateForm').events.submit({preventDefault(){}});assert.equal(posts.length,0);
 confirm=true;fail=true;await $('withdrawalCreateForm').events.submit({preventDefault(){}});assert.equal(storage.size,1);
 const original=posts.at(-1).payload;fail=false;await $('withdrawalCreateForm').events.submit({preventDefault(){}});assert.deepEqual(posts.at(-1).payload,original);assert.equal(storage.size,0);
 await context.loadWithdrawals();assert.match($('withdrawalList').innerHTML,/ACCEPT &amp;|ACCEPT & RESERVE/);
 $('withdraw-address-1').value='1BoatSLRHtKNngkdXEeobR76b53LETtpyT';
 const button={dataset:{withdrawal:'1',action:'accept'}};
 confirm=false;const before=posts.length;await $('withdrawalList').events.click({target:{closest:()=>button}});assert.equal(posts.length,before);
 confirm=true;await $('withdrawalList').events.click({target:{closest:()=>button}});
 assert.equal(posts.at(-1).payload.agreed_amount_cents,2000);assert.equal(posts.at(-1).payload.agreed_btc_satoshis,20000);assert.match(prompts.at(-1),/Reserve \$20.00/);
 await context.loadAdminWithdrawals();$('withdraw-note-1').value='Approved for your payout';
 await $('adminWithdrawalList').events.click({target:{closest:()=>({dataset:{adminWithdrawal:'1',action:'approve'}})}});
 assert.equal(posts.at(-1).payload.note,'Approved for your payout');assert.match(prompts.at(-1),/does not send Bitcoin/);
 $('withdraw-note-1').value='Payment sent';$('withdraw-txid-1').value='a'.repeat(64);
 await $('adminWithdrawalList').events.click({target:{closest:()=>({dataset:{adminWithdrawal:'1',action:'paid'}})}});
 assert.equal(posts.at(-1).payload.confirmed_sent,true);assert.equal(posts.at(-1).payload.txid,'a'.repeat(64));
 console.log('Withdrawal consent, approval notes, balance rendering, payment confirmation and stable retry checks passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
