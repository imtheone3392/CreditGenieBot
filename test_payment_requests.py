"""Payment directory and request consent/accounting tests with dummy members."""
import unittest, uuid
from concurrent.futures import ThreadPoolExecutor
import test_admin_workflows as workflow
from test_admin_workflows import auth
import app

class PaymentRequests(unittest.TestCase):
    setUp=workflow.AdminWorkflows.setUp
    tearDown=workflow.AdminWorkflows.tearDown
    def payer(self):
        u=app.get_or_create_user({'id':101,'first_name':'Payer','username':'pay_member'})
        with app.db() as con:con.execute('UPDATE users SET balance_cents=2000 WHERE id=?',(u['id'],))
        return u['id']
    def payload(self,payer,amount=501):return {'payer_id':payer,'amount_cents':amount,'request_id':str(uuid.uuid4())}
    def create(self,payload,uid=100):return self.client.post('/api/payment-requests',headers=auth(uid),json=payload)
    def decide(self,rid,action='pay',uid=101):return self.client.post(f'/api/payment-requests/{rid}/decision',headers=auth(uid),json={'action':action})
    def balances(self):
        with app.db() as con:return {r['id']:r['balance_cents'] for r in con.execute('SELECT id,balance_cents FROM users')}
    def test_member_directory_username_search_and_privacy(self):
        payer=self.payer()
        self.assertEqual(self.client.get('/api/payment-members').status_code,401)
        data=self.client.get('/api/payment-members?search=@pay_member',headers=auth(100)).json()
        self.assertEqual(data['total'],1)
        self.assertEqual(set(data['members'][0]),{'member_id','first_name','username'})
        result=self.client.get('/api/transfers/resolve?username=@PAY_MEMBER',headers=auth(100))
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['member_id'],payer)
        self.assertEqual(self.client.get('/api/transfers/resolve?username=missing',headers=auth(100)).status_code,404)
        app.get_or_create_user({'id':102,'username':'PAY_MEMBER'})
        self.assertEqual(self.client.get('/api/transfers/resolve?username=pay_member',headers=auth(100)).status_code,409)
        for i in range(103,129):app.get_or_create_user({'id':i})
        a=self.client.get('/api/payment-members',headers=auth(100)).json();b=self.client.get('/api/payment-members?page=2',headers=auth(100)).json()
        self.assertEqual(len(a['members']),25);self.assertEqual(len(b['members']),a['total']-25)
    def test_request_never_debits_until_payer_accepts_and_retries_once(self):
        payer=self.payer();before=self.balances();p=self.payload(payer)
        r=self.create(p);self.assertEqual(r.status_code,200,r.text);rid=r.json()['request']['id']
        self.assertEqual(self.create(p).json()['request']['id'],rid)
        self.assertEqual(self.balances(),before)
        self.assertEqual(self.decide(rid,uid=100).status_code,403)
        self.assertEqual(self.decide(rid,uid=999).status_code,404)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.decide(rid),range(2)))
        self.assertEqual([r.status_code for r in results],[200,200])
        self.assertEqual(results[0].json()['transfer_id'],results[1].json()['transfer_id'])
        balances=self.balances();self.assertEqual(balances[payer],1499);self.assertEqual(balances[self.user['id']],3501)
        self.assertEqual(sum(balances.values()),sum(before.values()))
        self.assertEqual(self.client.get('/api/transfers',headers=auth(100)).json()['total'],1)
        self.assertEqual(self.decide(rid,'cancel',uid=100).status_code,409)
    def test_decline_cancel_and_insufficient_funds(self):
        payer=self.payer();before=self.balances()
        rid=self.create(self.payload(payer)).json()['request']['id']
        self.assertEqual(self.decide(rid,'decline',uid=100).status_code,403)
        self.assertEqual(self.decide(rid,'cancel').status_code,403)
        self.assertEqual(self.decide(rid,'decline').status_code,200)
        self.assertEqual(self.decide(rid,'decline').status_code,200)
        self.assertEqual(self.decide(rid).status_code,409)
        rid=self.create(self.payload(payer,2500)).json()['request']['id']
        self.assertEqual(self.decide(rid).status_code,409)
        self.assertEqual(self.decide(rid,'cancel',uid=100).status_code,200)
        self.assertEqual(self.decide(rid,'cancel',uid=100).status_code,200)
        self.assertEqual(self.balances(),before)
    def test_private_history_validation_and_pending_duplicates(self):
        payer=self.payer();p=self.payload(payer)
        self.assertEqual(self.client.get('/api/payment-requests').status_code,401)
        self.assertEqual(self.client.post('/api/payment-requests',json=p).status_code,401)
        for amount in [0,-1,True,'1',1.5]:self.assertEqual(self.create(self.payload(payer,amount)).status_code,422)
        self.assertEqual(self.create(self.payload(self.user['id'])).status_code,422)
        self.assertEqual(self.create(self.payload(9999)).status_code,404)
        r=self.create(p).json()['request']
        self.assertEqual(self.create(self.payload(payer)).status_code,409)
        self.assertEqual(self.create({**p,'amount_cents':502}).status_code,409)
        self.assertEqual(self.client.get('/api/payment-requests',headers=auth(999)).json()['total'],0)
        sent=self.client.get('/api/payment-requests',headers=auth(100)).json()['requests'][0]
        incoming=self.client.get('/api/payment-requests',headers=auth(101)).json()['requests'][0]
        self.assertFalse(sent['incoming']);self.assertTrue(incoming['incoming']);self.assertNotIn('request_id',incoming)
        app.init_db();self.assertEqual(self.client.get('/api/payment-requests',headers=auth(100)).json()['total'],1)
    def test_pay_cancel_race_has_one_outcome(self):
        payer=self.payer();rid=self.create(self.payload(payer)).json()['request']['id']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda x:self.decide(rid,*x),[('pay',101),('cancel',100)]))
        self.assertEqual(sorted(r.status_code for r in results),[200,409])
        row=self.client.get('/api/payment-requests',headers=auth(100)).json()['requests'][0]
        self.assertEqual(self.balances()[payer],1499 if row['status']=='paid' else 2000)
    def test_request_update_failure_rolls_back_transfer(self):
        payer=self.payer();rid=self.create(self.payload(payer)).json()['request']['id'];before=self.balances()
        with app.db() as con:con.execute("CREATE TRIGGER test_fail_request BEFORE UPDATE ON payment_requests BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.decide(rid)
        self.assertEqual(self.balances(),before)
        self.assertEqual(self.client.get('/api/transfers',headers=auth(100)).json()['total'],0)
