"""Wallet accounting tests with isolated SQLite and mocked Telegram."""
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from telegram.error import TimedOut
import test_admin_workflows as workflow
from test_admin_workflows import auth
import app

class GiftCards(unittest.TestCase):
    setUp = workflow.AdminWorkflows.setUp
    tearDown = workflow.AdminWorkflows.tearDown
    balance = workflow.AdminWorkflows.balance

    def fund(self, amount=100000):
        with app.db() as con:
            con.execute('UPDATE users SET balance_cents=? WHERE id=?',(amount,self.user['id']))

    def payload(self, amount=40000):
        return {'brand_id':next(iter(app.GIFT_CARD_BRANDS)),'amount_cents':amount,'request_id':str(uuid.uuid4())}

    def buy(self, payload=None):
        return self.client.post('/api/gift-cards/orders',headers=auth(100),json=payload or self.payload())

    def decide(self, oid, status='approved', details='TEST CODE, US redemption only', uid=900):
        return self.client.post(f'/api/admin/gift-cards/orders/{oid}/decision',headers=auth(uid),json={'status':status,'details':details})

    def test_limits_validation_and_insufficient_funds(self):
        self.fund()
        self.assertEqual(self.client.get('/api/gift-cards').status_code,401)
        brands=self.client.get('/api/gift-cards',headers=auth(100)).json()['brands']
        self.assertGreater(len(brands),100)
        for amount in [39999,100001,0,-1,40000.5,True,'40000']:
            self.assertEqual(self.buy(self.payload(amount)).status_code,422)
        self.assertEqual(self.balance(),100000)
        self.assertEqual(self.buy(self.payload(100000)).status_code,200)
        self.assertEqual(self.balance(),0)
        self.assertEqual(self.buy().status_code,409)

    def test_retry_and_private_delivery(self):
        self.fund(); payload=self.payload()
        first=self.buy(payload);self.assertEqual(first.status_code,200,first.text)
        oid=first.json()['order']['id']
        self.assertEqual(self.buy(payload).json()['order']['id'],oid)
        self.assertEqual(self.balance(),60000)
        self.assertEqual(self.buy({**payload,'amount_cents':50000}).status_code,409)
        self.assertEqual(self.decide(oid,uid=100).status_code,403)
        self.assertEqual(self.client.get('/api/admin/gift-cards/orders',headers=auth(100)).status_code,403)
        self.assertEqual(self.decide(oid).status_code,200)
        self.assertEqual(self.decide(oid).status_code,200)
        self.assertEqual(self.balance(),60000); self.assertEqual(self.sender.await_count,1)
        mine=self.client.get('/api/gift-cards/orders',headers=auth(100))
        self.assertEqual(mine.headers['cache-control'],'no-store')
        self.assertIn('TEST CODE',mine.json()['orders'][0]['delivery_details'])
        self.assertEqual(self.client.get('/api/gift-cards/orders',headers=auth(101)).json()['orders'],[])
        self.assertEqual(self.decide(oid,status='rejected').status_code,409)
        self.assertEqual(self.balance(),60000)

    def test_competing_orders_cannot_overspend(self):
        self.fund(60000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes=list(pool.map(lambda _:self.buy().status_code,range(2)))
        self.assertEqual(sorted(codes),[200,409]);self.assertEqual(self.balance(),20000)

    def test_concurrent_retry_debits_once(self):
        self.fund(); payload=self.payload()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.buy(payload).json(),range(2)))
        self.assertEqual(results[0]['order']['id'],results[1]['order']['id']);self.assertEqual(self.balance(),60000)

    def test_refund_once_despite_notification_failure(self):
        self.fund(); oid=self.buy().json()['order']['id'];self.sender.side_effect=TimedOut()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.decide(oid,status='rejected',details='Unavailable'),range(2)))
        self.assertEqual([r.status_code for r in results],[200,200]);self.assertEqual(self.balance(),100000)
        self.assertEqual(self.sender.await_count,1)
        self.assertFalse(results[0].json()['notification_sent'])
        self.assertEqual(self.decide(oid).status_code,409)

    def test_migration_pagination_and_required_details(self):
        self.fund(); oid=self.buy().json()['order']['id'];app.init_db();app.init_db()
        self.assertEqual(self.balance(),60000)
        self.assertEqual(self.client.get('/api/gift-cards/orders',headers=auth(100)).json()['orders'][0]['id'],oid)
        self.assertEqual(self.client.get('/api/gift-cards/orders?page=2',headers=auth(100)).json()['orders'],[])
        self.assertEqual(self.decide(oid,details='   ').status_code,422)
        self.assertEqual(self.decide(999999).status_code,404)
        self.assertEqual(self.decide(oid).status_code,200)
        self.assertEqual(self.client.get('/api/admin/gift-cards/orders',headers=auth(900)).json()['total'],0)
        self.assertEqual(self.client.get('/api/admin/gift-cards/orders?status=approved',headers=auth(900)).json()['total'],1)

    def test_quantity_discount_boundaries(self):
        for qty, charge, percent in [(1,40000,0),(10,400000,0),(11,176000,60),(50,800000,60)]:
            with self.subTest(quantity=qty):
                self.fund(1000000)
                result=self.buy({**self.payload(),'quantity':qty})
                self.assertEqual(result.status_code,200,result.text)
                order=result.json()['order']
                self.assertEqual(order['quantity'],qty)
                self.assertEqual(order['total_value_cents'],qty*40000)
                self.assertEqual(order['charged_cents'],charge)
                self.assertEqual(order['discount_percent'],percent)
                self.assertEqual(order['discount_cents'],qty*40000-charge)
                self.assertEqual(self.balance(),1000000-charge)
        self.fund(2500000)
        order=self.buy({**self.payload(100000),'quantity':50}).json()['order']
        self.assertEqual(order['charged_cents'],2000000)
        self.assertEqual(self.balance(),500000)

    def test_invalid_quantity_and_price_tampering(self):
        self.fund(1000000)
        for qty in [0,-1,51,100,1.5,True,'11',None]:
            with self.subTest(quantity=qty):
                self.assertEqual(self.buy({**self.payload(),'quantity':qty}).status_code,422)
        for key in ['charged_cents','discount_percent','total_value_cents']:
            self.assertEqual(self.buy({**self.payload(),'quantity':11,key:1}).status_code,422)
        self.assertEqual(self.balance(),1000000)
        payload={**self.payload(),'quantity':11}
        self.assertEqual(self.buy(payload).status_code,200)
        self.assertEqual(self.buy({**payload,'quantity':12}).status_code,409)

    def test_discounted_rejection_refunds_payment_once(self):
        self.fund(176000)
        payload={**self.payload(),'quantity':11}
        order=self.buy(payload).json()['order'];oid=order['id']
        self.assertEqual(self.balance(),0)
        self.assertEqual(self.buy(payload).json()['order']['id'],oid)
        self.sender.side_effect=TimedOut()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.decide(oid,status='rejected',details='Unavailable'),range(2)))
        self.assertEqual([r.status_code for r in results],[200,200])
        self.assertEqual(self.balance(),176000)
        row=self.client.get('/api/gift-cards/orders',headers=auth(100)).json()['orders'][0]
        self.assertEqual(row['refunded_cents'],176000)
        self.assertEqual(row['total_value_cents'],440000)
        self.assertEqual(row['discount_percent'],60)

    def test_bulk_order_approval_and_cent_rounding(self):
        self.fund(176009)
        order=self.buy({**self.payload(40002),'quantity':11}).json()['order']
        self.assertEqual(order['charged_cents'],176009)
        self.assertEqual(self.balance(),0)
        oid=order['id']
        app.init_db()
        self.assertEqual(self.decide(oid,details='Eleven test card codes').status_code,200)
        self.assertEqual(self.balance(),0)
        row=self.client.get('/api/admin/gift-cards/orders?status=approved',headers=auth(900)).json()['orders'][0]
        self.assertEqual(row['quantity'],11)
        self.assertEqual(row['charged_cents'],176009)
        self.assertEqual(row['discount_cents'],264013)
        self.assertIn('11 cards',self.sender.call_args.kwargs['text'])
        self.assertEqual(self.client.get('/api/gift-cards/orders',headers=auth(101)).json()['orders'],[])

    def test_legacy_orders_migrate_without_repricing(self):
        with app.db() as con:
            for column in ['charged_cents','discount_percent','quantity']:
                con.execute('ALTER TABLE gift_card_orders DROP COLUMN '+column)
            for status in ['pending','approved','rejected']:
                con.execute('''INSERT INTO gift_card_orders
                    (user_id,request_id,brand_id,brand_name,amount_cents,status,refunded_cents,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (self.user['id'],str(uuid.uuid4()),'legacy','Legacy Brand',40000,status,
                     40000 if status=='rejected' else 0,app.now_iso(),app.now_iso()))
        before=self.balance()
        app.init_db();app.init_db()
        rows=self.client.get('/api/gift-cards/orders',headers=auth(100)).json()['orders']
        for row in rows:
            self.assertEqual((row['quantity'],row['discount_percent'],row['charged_cents']),(1,0,40000))
            self.assertEqual(row['refunded_cents'],40000 if row['status']=='rejected' else 0)
        pending=next(r['id'] for r in rows if r['status']=='pending')
        self.assertEqual(self.decide(pending,status='rejected').status_code,200)
        self.assertEqual(self.balance(),before+40000)

    def test_competing_bulk_orders_respect_discounted_balance(self):
        self.fund(200000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes=list(pool.map(lambda _:self.buy({**self.payload(),'quantity':11}).status_code,range(2)))
        self.assertEqual(sorted(codes),[200,409])
        self.assertEqual(self.balance(),24000)
