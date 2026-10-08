"""Isolated withdrawal tests; Telegram is mocked and no Bitcoin is sent."""
import asyncio
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch
from test_admin_workflows import AdminWorkflows as _Base, auth
import app
from btc_address import normalize_btc_address
from telegram.error import TimedOut

ADDRESS='1BoatSLRHtKNngkdXEeobR76b53LETtpyT'

class TestWithdrawals(unittest.TestCase):
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    balance = _Base.balance
    def create(self, uid=900, **overrides):
        payload={'member_id':self.user['id'],'amount_cents':2000,'btc_satoshis':20000,'request_id':str(uuid.uuid4()),'note':'Requested payout'}
        payload.update(overrides)
        return self.client.post('/api/admin/withdrawals',headers=auth(uid),json=payload)

    def accept(self, wid, uid=100, **overrides):
        payload={'action':'accept','btc_address':ADDRESS,'agreed_amount_cents':2000,'agreed_btc_satoshis':20000};payload.update(overrides)
        return self.client.post(f'/api/withdrawals/{wid}/decision',headers=auth(uid),json=payload)

    def review(self,wid,action='approve',uid=900,**overrides):
        payload={'action':action,'note':'Your withdrawal has been approved.'};payload.update(overrides)
        return self.client.post(f'/api/admin/withdrawals/{wid}/decision',headers=auth(uid),json=payload)

    def test_full_lifecycle_and_balances(self):
        r=self.create();self.assertEqual(r.status_code,200,r.text);wid=r.json()['withdrawal']['id']
        self.assertEqual(self.balance(),3000);self.assertTrue(r.json()['notification_sent'])
        self.assertEqual(self.accept(wid).status_code,200);self.assertEqual(self.balance(),1000)
        member=self.client.get('/api/admin/members',headers=auth(900)).json()['members'][0]
        self.assertEqual((member['balance_cents'],member['reserved_cents']),(1000,2000))
        self.assertEqual(self.review(wid,action='paid',txid='a'*64,confirmed_sent=True).status_code,409)
        self.assertEqual(self.review(wid).status_code,200)
        mine=self.client.get('/api/withdrawals',headers=auth(100)).json()
        self.assertEqual(mine['reserved_cents'],2000)
        self.assertEqual(mine['withdrawals'][0]['approval_note'],'Your withdrawal has been approved.')
        self.assertIn('Admin note: Your withdrawal has been approved.',self.sender.call_args.kwargs['text'])
        self.assertEqual(self.review(wid).status_code,200);self.assertEqual(self.balance(),1000)
        self.assertEqual(self.review(wid,action='paid',txid='a'*64,confirmed_sent=True).status_code,200)
        self.assertEqual(self.review(wid,action='paid',txid='a'*64,confirmed_sent=True).status_code,200)
        self.assertEqual(self.balance(),1000)
        self.assertEqual(self.client.get('/api/withdrawals',headers=auth(100)).json()['reserved_cents'],0)
        self.assertEqual(self.review(wid,action='reject',confirmed_not_sent=True).status_code,409)
        self.assertEqual(self.accept(wid).status_code,200);self.assertEqual(self.balance(),1000)
        with app.db() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM withdrawal_events').fetchone()[0],4)

    def test_auth_owner_consent_and_admin_cannot_accept(self):
        self.assertEqual(self.create(uid=100).status_code,403)
        for path in ['/api/withdrawals','/api/admin/withdrawals']:
            self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.get('/api/admin/withdrawals',headers=auth(100)).status_code,403)
        wid=self.create().json()['withdrawal']['id']
        self.assertEqual(self.accept(wid,uid=101).status_code,404)
        self.assertEqual(self.accept(wid,uid=900).status_code,404)
        self.assertEqual(self.review(wid,uid=100).status_code,403)
        self.assertEqual(self.review(wid).status_code,422)
        self.assertEqual(self.accept(wid,agreed_amount_cents=1).status_code,409)
        self.assertEqual(self.accept(wid,agreed_btc_satoshis=1).status_code,409)
        self.assertEqual(self.accept(wid,btc_address='not-an-address').status_code,422)
        self.assertEqual(self.balance(),3000)
        self.assertEqual(self.client.get('/api/withdrawals',headers=auth(101)).json()['withdrawals'],[])

    def test_decline_cancel_and_no_charge(self):
        wid=self.create().json()['withdrawal']['id']
        self.assertEqual(self.accept(wid,action='decline',btc_address='').status_code,200)
        self.assertEqual(self.accept(wid,action='decline',btc_address='').status_code,200)
        self.assertEqual(self.accept(wid).status_code,409)
        self.assertEqual(self.balance(),3000)
        wid2=self.create().json()['withdrawal']['id']
        self.assertEqual(self.review(wid2,action='cancel').status_code,200)
        self.assertEqual(self.review(wid2,action='cancel').status_code,200)
        self.assertEqual(self.balance(),3000)

    def test_idempotent_creation_and_active_request_limit(self):
        ref=str(uuid.uuid4())
        first=self.create(request_id=ref).json()['withdrawal']['id']
        self.assertEqual(self.create(request_id=ref).json()['withdrawal']['id'],first)
        self.assertEqual(self.create(request_id=ref,amount_cents=1000).status_code,409)
        self.assertEqual(self.create().status_code,409)
        self.assertEqual(self.sender.await_count,1)
        self.assertEqual(self.balance(),3000)

    def test_concurrent_accept_and_reject_release_once(self):
        wid=self.create().json()['withdrawal']['id']
        with ThreadPoolExecutor(2) as pool:codes=list(pool.map(lambda _:self.accept(wid).status_code,range(2)))
        self.assertEqual(codes,[200,200]);self.assertEqual(self.balance(),1000)
        self.assertEqual(self.review(wid,action='reject').status_code,422)
        self.assertEqual(self.review(wid).status_code,200)
        with ThreadPoolExecutor(2) as pool:codes=list(pool.map(lambda _:self.review(wid,action='reject',confirmed_not_sent=True).status_code,range(2)))
        self.assertEqual(codes,[200,200]);self.assertEqual(self.balance(),3000)
        self.assertEqual(self.review(wid,action='paid',txid='b'*64,confirmed_sent=True).status_code,409)

    def test_spending_and_acceptance_compete(self):
        wid=self.create(amount_cents=3000).json()['withdrawal']['id']
        recipient=app.get_or_create_user({'id':101})
        def spend():
            return self.client.post('/api/transfers',headers=auth(100),json={'recipient_id':recipient['id'],'amount_cents':2500,'agreed_fee_cents':500,'request_id':str(uuid.uuid4())}).status_code
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(self.accept,wid,agreed_amount_cents=3000),pool.submit(spend)]
            codes=[futures[0].result().status_code,futures[1].result()]
        self.assertEqual(sorted(codes),[200,409]);self.assertEqual(self.balance(),0)

    def test_notification_failure_persists_request_and_note(self):
        self.sender.side_effect=TimedOut()
        result=self.create();self.assertEqual(result.status_code,200);self.assertFalse(result.json()['notification_sent'])
        wid=result.json()['withdrawal']['id'];self.accept(wid)
        result=self.review(wid,note='Processing your Bitcoin payment.')
        self.assertEqual(result.status_code,200);self.assertFalse(result.json()['notification_sent'])
        app.init_db();app.init_db()
        row=self.client.get('/api/withdrawals',headers=auth(100)).json()['withdrawals'][0]
        self.assertEqual(row['approval_note'],'Processing your Bitcoin payment.')
        self.assertEqual(row['btc_address'],ADDRESS);self.assertEqual(self.balance(),1000)

    def test_admin_direct_approval_reserves_once_and_releases(self):
        wid=self.create().json()['withdrawal']['id']
        self.assertEqual(self.review(wid,btc_address=ADDRESS).status_code,422)
        self.assertEqual(self.balance(),3000)
        self.assertEqual(self.review(wid,uid=100,btc_address=ADDRESS,confirmed_reserve=True).status_code,403)
        with ThreadPoolExecutor(2) as pool:
            codes=list(pool.map(lambda _:self.review(wid,btc_address=ADDRESS,confirmed_reserve=True).status_code,range(2)))
        self.assertEqual(codes,[200,200]);self.assertEqual(self.balance(),1000)
        mine=self.client.get('/api/withdrawals',headers=auth(100)).json()
        self.assertEqual(mine['reserved_cents'],2000)
        self.assertEqual(mine['withdrawals'][0]['status'],'approved')
        self.assertEqual(mine['withdrawals'][0]['btc_address'],ADDRESS)
        self.assertIn(ADDRESS,self.sender.call_args.kwargs['text'])
        with app.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM withdrawal_events WHERE action='approved_without_member_acceptance'").fetchone()[0],1)
        self.assertEqual(self.review(wid,btc_address='3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy',confirmed_reserve=True).status_code,409)
        self.assertEqual(self.review(wid,action='reject',confirmed_not_sent=True).status_code,200)
        self.assertEqual(self.review(wid,action='reject',confirmed_not_sent=True).status_code,200)
        self.assertEqual(self.balance(),3000)

    def test_direct_approval_insufficient_and_member_address_immutable(self):
        wid=self.create().json()['withdrawal']['id']
        with app.db() as con: con.execute('UPDATE users SET balance_cents=0 WHERE id=?',(self.user['id'],))
        self.assertEqual(self.review(wid,btc_address=ADDRESS,confirmed_reserve=True).status_code,409)
        self.assertEqual(self.client.get('/api/withdrawals',headers=auth(100)).json()['withdrawals'][0]['status'],'requested')
        with app.db() as con: con.execute('UPDATE users SET balance_cents=3000 WHERE id=?',(self.user['id'],))
        self.accept(wid)
        self.assertEqual(self.review(wid,btc_address='3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy',confirmed_reserve=True).status_code,409)
        self.assertEqual(self.review(wid).status_code,200)
        self.assertEqual(self.balance(),1000)

    def test_validation_and_immutable_receipt(self):
        for amount in [0,-1,1.5,True,'2000']:
            self.assertEqual(self.create(amount_cents=amount).status_code,422)
        self.assertEqual(self.create(amount_cents=4000).status_code,409)
        self.assertEqual(self.create(btc_satoshis=0).status_code,422)
        wid=self.create().json()['withdrawal']['id'];self.accept(wid)
        self.assertEqual(self.review(wid,note=' ').status_code,422)
        self.assertEqual(self.review(wid).status_code,200)
        self.assertEqual(self.review(wid,action='paid',txid='a'*64).status_code,422)
        self.assertEqual(self.review(wid,action='paid',txid='bad',confirmed_sent=True).status_code,422)
        self.assertEqual(self.review(wid,action='paid',txid='a'*64,confirmed_sent=True).status_code,200)
        self.assertEqual(self.review(wid,action='paid',txid='b'*64,confirmed_sent=True).status_code,409)


def test_btc_mainnet_checksums():
    for address in [ADDRESS,'3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy','bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4']:
        assert normalize_btc_address(address)==address
    import pytest
    for address in [ADDRESS[:-1]+'x','bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5','bitcoin:'+ADDRESS,'mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn','bc1Qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4']:
        with pytest.raises(ValueError): normalize_btc_address(address)
