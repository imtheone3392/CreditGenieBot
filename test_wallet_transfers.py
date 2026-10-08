"""Isolated member payments; temporary database and no real funds or messages."""
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
import test_admin_workflows as workflow
from test_admin_workflows import auth
import app

class WalletTransfers(unittest.TestCase):
    setUp = workflow.AdminWorkflows.setUp
    tearDown = workflow.AdminWorkflows.tearDown
    balance = workflow.AdminWorkflows.balance

    def recipient(self, tg=101):
        return app.get_or_create_user({'id':tg,'first_name':'Recipient','username':'receiver'})['id']

    def payload(self, recipient, amount=1000):
        return {'recipient_id':recipient,'amount_cents':amount,'request_id':str(uuid.uuid4())}

    def send(self, payload, uid=100):
        return self.client.post('/api/transfers',headers=auth(uid),json=payload)

    def balances(self):
        with app.db() as con:return {r['id']:r['balance_cents'] for r in con.execute('SELECT id,balance_cents FROM users')}

    def test_auth_lookup_and_own_member_id(self):
        rid=self.recipient()
        self.assertEqual(self.client.get('/api/me',headers=auth(100)).json()['member_id'],self.user['id'])
        self.assertEqual(self.client.get(f'/api/transfers/recipient/{rid}').status_code,401)
        self.assertEqual(self.client.get('/api/transfers').status_code,401)
        self.assertEqual(self.client.post('/api/transfers',json=self.payload(rid)).status_code,401)
        response=self.client.get(f'/api/transfers/recipient/{rid}',headers=auth(100)).json()
        self.assertEqual(response,{'member_id':rid,'first_name':'Recipient'})
        self.assertEqual(self.client.get('/api/transfers/recipient/999999',headers=auth(100)).status_code,404)
        self.assertEqual(self.client.get(f'/api/transfers/recipient/{self.user["id"]}',headers=auth(100)).status_code,422)

    def test_transfer_and_private_two_sided_receipts(self):
        rid=self.recipient(); p=self.payload(rid,1251)
        response=self.send(p);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.balances(),{self.user['id']:1749,rid:1251})
        sent=self.client.get('/api/transfers',headers=auth(100)).json()['transfers'][0]
        received=self.client.get('/api/transfers',headers=auth(101)).json()['transfers'][0]
        self.assertEqual(sent['direction'],'sent');self.assertEqual(received['direction'],'received')
        self.assertEqual(sent['id'],received['id']);self.assertEqual(received['amount_cents'],1251)
        self.assertEqual(self.client.get('/api/transfers',headers=auth(102)).json()['total'],0)
        self.assertNotIn('request_id',received)
        self.assertEqual(self.sender.await_count,0)

    def test_invalid_self_unknown_and_insufficient_do_not_change_money(self):
        rid=self.recipient(); initial=self.balances()
        for amount in [0,-1,1.5,True,'1',app.MAX_WALLET_CENTS+1]:
            self.assertEqual(self.send(self.payload(rid,amount)).status_code,422)
        for recipient in [0,-1,True,'1',1.5]:
            self.assertEqual(self.send(self.payload(recipient)).status_code,422)
        self.assertEqual(self.send(self.payload(self.user['id'])).status_code,422)
        self.assertEqual(self.send(self.payload(999999)).status_code,404)
        self.assertEqual(self.send(self.payload(rid,3001)).status_code,409)
        self.assertEqual(self.send({**self.payload(rid),'sender_id':rid}).status_code,422)
        self.assertEqual(self.send({**self.payload(rid),'request_id':'bad'}).status_code,422)
        self.assertEqual(self.balances(),initial)

    def test_retry_and_competing_transfers(self):
        rid=self.recipient(); p=self.payload(rid,2000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.send(p),range(2)))
        self.assertEqual([r.status_code for r in results],[200,200])
        self.assertEqual(results[0].json()['transfer']['id'],results[1].json()['transfer']['id'])
        self.assertEqual(self.balances(),{self.user['id']:1000,rid:2000})
        self.assertEqual(self.send({**p,'amount_cents':1}).status_code,409)
        other=self.recipient(103)
        self.assertEqual(self.send({**p,'recipient_id':other}).status_code,409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes=list(pool.map(lambda _:self.send(self.payload(rid,700)).status_code,range(2)))
        self.assertEqual(sorted(codes),[200,409])
        self.assertEqual(self.balance(),300)
        self.assertEqual(sum(self.balances().values()),3000)

    def test_received_funds_can_be_spent_and_startup_preserves_ledger(self):
        rid=self.recipient(); self.assertEqual(self.send(self.payload(rid,3000)).status_code,200)
        self.assertEqual(self.send(self.payload(self.user['id'],1000),uid=101).status_code,200)
        app.init_db();app.init_db()
        self.assertEqual(self.balances(),{self.user['id']:1000,rid:2000})
        self.assertEqual(self.client.get('/api/transfers',headers=auth(100)).json()['total'],2)
        self.assertEqual(self.client.get('/api/transfers?page=2',headers=auth(100)).json()['transfers'],[])

    def test_insert_failure_rolls_back_both_balances(self):
        rid=self.recipient();before=self.balances()
        with app.db() as con:
            con.execute("CREATE TRIGGER test_fail_transfer BEFORE INSERT ON wallet_transfers BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.send(self.payload(rid))
        self.assertEqual(self.balances(),before)
        self.assertEqual(self.client.get('/api/transfers',headers=auth(100)).json()['total'],0)

    def test_competing_gift_purchase_and_transfer_conserve_balance(self):
        rid=self.recipient()
        with app.db() as con:con.execute('UPDATE users SET balance_cents=40000 WHERE id=?',(self.user['id'],))
        def action(which):
            if which:return self.send(self.payload(rid,30000)).status_code
            return self.client.post('/api/gift-cards/orders',headers=auth(100),json={'brand_id':next(iter(app.GIFT_CARD_BRANDS)),'amount_cents':40000,'request_id':str(uuid.uuid4())}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(action,[0,1]))
        self.assertEqual(sorted(codes),[200,409])
        self.assertTrue(all(x>=0 for x in self.balances().values()))
        with app.db() as con:spent=con.execute('SELECT COALESCE(SUM(charged_cents),0) FROM gift_card_orders').fetchone()[0]
        self.assertEqual(sum(self.balances().values())+spent,40000)

    def test_recipient_overflow_is_rejected_and_history_paginates(self):
        rid=self.recipient()
        with app.db() as con:con.execute('UPDATE users SET balance_cents=? WHERE id=?',(app.MAX_WALLET_CENTS,rid))
        self.assertEqual(self.send(self.payload(rid,1)).status_code,409)
        self.assertEqual(self.balance(),3000)
        with app.db() as con:con.execute('UPDATE users SET balance_cents=0 WHERE id=?',(rid,))
        for _ in range(26):self.assertEqual(self.send(self.payload(rid,1)).status_code,200)
        first=self.client.get('/api/transfers',headers=auth(100)).json()
        second=self.client.get('/api/transfers?page=2',headers=auth(100)).json()
        self.assertEqual((first['total'],len(first['transfers']),len(second['transfers'])),(26,25,1))
        self.assertGreater(first['transfers'][0]['id'],second['transfers'][0]['id'])
