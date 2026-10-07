"""Isolated workflow tests: no real Telegram sends, deposits or member changes."""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch
os.environ['BOT_TOKEN'] = '123456:test-only-token'
os.environ['ADMIN_IDS'] = '900'
import app
from fastapi.testclient import TestClient
from telegram.error import TimedOut


def auth(uid):
    pairs = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid, 'first_name': 'Test'})}
    key = hmac.new(b'WebAppData', app.BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs['hash'] = hmac.new(key, '\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs)).encode(), hashlib.sha256).hexdigest()
    return {'X-Telegram-Init-Data': urlencode(pairs)}


class AdminWorkflows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = patch.object(app, 'DB_PATH', self.temp.name+'/test.db'); self.path.start()
        self.admin = patch.object(app, 'ADMIN_IDS', {900}); self.admin.start()
        app.init_db()
        self.user = app.get_or_create_user({'id': 100, 'first_name': 'Test'})
        with app.db() as con:
            con.execute('UPDATE users SET balance_cents=3000 WHERE id=?', (self.user['id'],))
        self.client = TestClient(app.api)
        self.sender = AsyncMock(return_value=SimpleNamespace(message_id=88))
        self.bot = patch.object(type(app.telegram_bot.bot), 'send_message', self.sender); self.bot.start()

    def tearDown(self):
        self.client.close(); self.bot.stop(); self.admin.stop(); self.path.stop(); self.temp.cleanup()

    def request(self):
        r=self.client.post('/api/bank-job/requests',headers=auth(100),json={'alias':'Ghost','agreed_price_cents':1300})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()['request']['id']

    def decide(self, rid, note='Your fictional driver is ready.', status='approved', uid=900):
        return self.client.post(f'/api/admin/bank-job/requests/{rid}/decision',headers=auth(uid),json={'status':status,'note':note})

    def balance(self):
        return self.client.get('/api/wallet',headers=auth(100)).json()['balance_cents']

    def test_approval_saved_owner_only_and_once(self):
        rid=self.request(); self.assertEqual(self.request(),rid)
        self.assertEqual(self.balance(),3000)
        self.assertEqual(self.decide(rid).status_code,200)
        self.assertEqual(self.decide(rid).status_code,200)
        self.assertEqual(self.balance(),1700)
        mine=self.client.get('/api/bank-job/requests',headers=auth(100)).json()['requests']
        self.assertEqual(mine[0]['approval_note'],'Your fictional driver is ready.')
        self.assertTrue(mine[0]['player_id'].startswith('BJ-'))
        self.assertEqual(self.client.get('/api/bank-job/requests',headers=auth(101)).json()['requests'],[])
        self.assertEqual(self.sender.await_count,1)

    def test_alias_search(self):
        result=self.client.get('/api/bank-job/profiles/search',headers=auth(100),params={'name':' gHoSt ','birth_year':1995,'state':' neon '})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['profile']['alias'],'Ghost')
        for params in ({'name':'Ghost','birth_year':1997,'state':'Neon'}, {'name':'Ghost','birth_year':1995,'state':'NV'}):
            self.assertEqual(self.client.get('/api/bank-job/profiles/search',headers=auth(100),params=params).status_code,404)
        self.assertEqual(self.balance(),3000)
        self.assertEqual(self.client.get('/api/bank-job/profiles/search',headers=auth(100),params={'name':'Unknown','birth_year':1995,'state':'Neon'}).status_code,404)
        self.assertEqual(self.client.post('/api/bank-job/requests',headers=auth(100),json={'alias':'Unknown','agreed_price_cents':1300}).status_code,404)
        self.assertEqual(self.client.get('/api/bank-job/profiles/search?name=Ghost&birth_year=1995&state=Neon').status_code,401)

    def test_insufficient_funds_and_decline(self):
        rid=self.request()
        with app.db() as con: con.execute('UPDATE users SET balance_cents=0 WHERE id=?',(self.user['id'],))
        self.assertEqual(self.decide(rid).status_code,409)
        self.assertEqual(self.decide(rid,status='declined').status_code,200)
        self.assertEqual(self.balance(),0)

    def test_failure_does_not_lose_note_or_repeat_charge(self):
        self.sender.side_effect=TimedOut()
        rid=self.request()
        self.assertFalse(self.decide(rid).json()['notification_sent'])
        self.assertTrue(self.decide(rid).json()['already_processed'])
        self.assertEqual(self.balance(),1700)
        self.assertEqual(self.sender.await_count,1)

    def test_auth_and_payload_boundaries(self):
        rid=self.request()
        self.assertEqual(self.decide(rid,uid=100).status_code,403)
        self.assertEqual(self.decide(rid,note=' ').status_code,422)
        for path in ['/api/admin/inbox','/api/admin/bank-job/requests',f'/api/admin/members/{self.user["id"]}/messages','/api/admin/deposits']:
            self.assertEqual(self.client.get(path).status_code,401)
            self.assertEqual(self.client.get(path,headers=auth(100)).status_code,403)
        r=self.client.post('/api/bank-job/requests',headers=auth(100),json={'alias':'Ghost','agreed_price_cents':1300,'name':'arbitrary'})
        self.assertEqual(r.status_code,422)
        r=self.client.post('/api/character-requests',headers=auth(100),json={'character_name':'name','game':'2000','character_id':'NV'})
        self.assertEqual(r.status_code,410)

    def test_incoming_reply_dedup_and_read(self):
        update=SimpleNamespace(effective_user=SimpleNamespace(to_dict=lambda:{'id':100}),effective_chat=SimpleNamespace(type='private'),
            message=SimpleNamespace(text='My fictional mission question',caption=None,message_id=42))
        asyncio.run(app.receive_member_message(update,None)); asyncio.run(app.receive_member_message(update,None))
        inbox=self.client.get('/api/admin/inbox',headers=auth(900)).json()['threads']
        self.assertEqual(inbox[0]['unread'],1)
        path=f'/api/admin/members/{self.user["id"]}/messages'
        messages=self.client.get(path,headers=auth(900)).json()['messages']
        self.assertEqual(len(messages),1)
        self.client.post(path+'/read',headers=auth(900),json={'through_id':messages[0]['id']})
        self.assertEqual(self.client.get('/api/admin/inbox',headers=auth(900)).json()['threads'][0]['unread'],0)
        self.client.post(f'/api/admin/members/{self.user["id"]}/message',headers=auth(900),json={'message':'Your mission starts soon.'})
        messages=self.client.get(path,headers=auth(900)).json()['messages']
        self.assertEqual(messages[-1]['direction'],'outgoing')

    def test_concurrent_deposit_credit_once(self):
        r=self.client.post('/api/deposit',headers=auth(100),json={'txid':'a'*64})
        did=r.json()['deposit_id']
        def credit(_):
            c=TestClient(app.api)
            try: return c.post('/api/admin/credit-deposit',headers=auth(900),json={'deposit_id':did,'amount_cents':1000}).status_code
            finally: c.close()
        with ThreadPoolExecutor(2) as pool: codes=list(pool.map(credit,range(2)))
        self.assertEqual(sorted(codes),[200,409])
        self.assertEqual(self.balance(),4000)

    def test_concurrent_approval_once_and_migration(self):
        rid=self.request()
        def approve(_): return self.decide(rid).status_code
        with ThreadPoolExecutor(2) as pool: codes=list(pool.map(approve,range(2)))
        self.assertEqual(codes,[200,200]);self.assertEqual(self.balance(),1700)
        app.init_db();app.init_db();self.assertEqual(self.balance(),1700)
        self.assertEqual(len(self.client.get('/api/bank-job/requests',headers=auth(100)).json()['requests']),1)

if __name__=='__main__':unittest.main()
