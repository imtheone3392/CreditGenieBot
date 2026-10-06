"""Run: python -m unittest test_catalog. No live messages or database writes."""
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
os.environ['BOT_TOKEN']='123456:test-only-token'
os.environ['ADMIN_IDS']='900'
import app
from fastapi.testclient import TestClient

def headers(user_id):
    pairs={'auth_date':str(int(time.time())), 'user':json.dumps({'id':user_id})}
    secret=hmac.new(b'WebAppData',app.BOT_TOKEN.encode(),hashlib.sha256).digest()
    pairs['hash']=hmac.new(secret,'\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs)).encode(),hashlib.sha256).hexdigest()
    return {'X-Telegram-Init-Data':urlencode(pairs)}

class RetiredCatalogTests(unittest.TestCase):
    def test_purchases_and_approvals_disabled_without_changing_records(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app,'DB_PATH',folder+'/test.db'), patch.object(app,'ADMIN_IDS',{900}):
            app.init_db()
            user=app.get_or_create_user({'id':100})
            with app.db() as con:
                con.execute('UPDATE users SET balance_cents=1700 WHERE id=?',(user['id'],))
                con.execute("INSERT INTO catalog_orders(user_id,catalog_id,status,agreed_price_cents,charged_cents,player_json,created_at,updated_at) VALUES(?,?,'released',1300,1300,?,?,?)",(user['id'],'nova-scout',json.dumps({'player_id':'CG-SAVED'}),app.now_iso(),app.now_iso()))
            client=TestClient(app.api)
            self.assertEqual(client.post('/api/catalog/orders',headers=headers(100),json={'catalog_id':'nova-scout','agreed_price_cents':1300}).status_code,410)
            self.assertEqual(client.post('/api/admin/catalog/orders/1/decision',headers=headers(900),json={'status':'released'}).status_code,410)
            self.assertEqual(client.get('/api/wallet',headers=headers(100)).json()['balance_cents'],1700)
            orders=client.get('/api/catalog/orders',headers=headers(100)).json()['orders']
            self.assertEqual(orders[0]['player']['player_id'],'CG-SAVED')
            self.assertNotIn('OPEN CHARACTER CATALOG',client.get('/app').text)
            client.close()

if __name__=='__main__':unittest.main()
