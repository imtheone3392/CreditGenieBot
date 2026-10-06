from fastapi.testclient import TestClient
from telegram.error import Forbidden
from test_admin_members import app_module, headers, member

PAYLOAD = {"character_name":"Star Ranger", "game":"Fictional Galaxy", "character_id":"ranger-7"}


def test_request_isolation_duplicate_and_permissions(app_module):
    m=app_module; c=TestClient(m.api)
    assert c.post('/api/character-requests',json=PAYLOAD).status_code==401
    assert c.post('/api/character-requests',headers=headers(m,202),json={**PAYLOAD,'dob':'01/01/2000'}).status_code==422
    assert c.post('/api/character-requests',headers=headers(m,202),json={**PAYLOAD,'character_name':'   '}).status_code==422
    r=c.post('/api/character-requests',headers=headers(m,202),json=PAYLOAD)
    assert r.status_code==200
    rid=r.json()['request_id']
    repeat=c.post('/api/character-requests',headers=headers(m,202),json=PAYLOAD).json()
    assert repeat['request_id']==rid and repeat['already_pending']
    assert len(c.get('/api/character-requests',headers=headers(m,202)).json()['requests'])==1
    assert c.get('/api/character-requests',headers=headers(m,303)).json()['requests']==[]
    assert c.get('/api/admin/character-requests',headers=headers(m,202)).status_code==403
    assert c.post(f'/api/admin/character-requests/{rid}/decision',headers=headers(m,202),json={'status':'available'}).status_code==403
    queue=c.get('/api/admin/character-requests',headers=headers(m)).json()
    assert queue['total']==1 and queue['requests'][0]['character_id']=='ranger-7'
    m.init_db()
    assert c.get('/api/admin/character-requests',headers=headers(m)).json()['total']==1
    assert c.get('/api/wallet',headers=headers(m,202)).json()['balance_cents']==0


def test_decision_saved_and_only_one_notification(app_module):
    m=app_module; c=TestClient(m.api)
    rid=c.post('/api/character-requests',headers=headers(m,202),json=PAYLOAD).json()['request_id']
    path=f'/api/admin/character-requests/{rid}/decision'
    assert c.post(path,headers=headers(m),json={'status':'other'}).status_code==422
    assert c.post(path,headers=headers(m),json={'status':'available','result_text':'anything'}).status_code==422
    assert c.post('/api/admin/character-requests/999/decision',headers=headers(m),json={'status':'available'}).status_code==404
    r=c.post(path,headers=headers(m),json={'status':'available'})
    assert r.status_code==200 and r.json()['notification_sent'] is True
    assert c.post(path,headers=headers(m),json={'status':'unavailable'}).status_code==409
    assert m.telegram_bot.bot.send_message.await_count==1
    assert m.telegram_bot.bot.send_message.call_args.kwargs['chat_id']==202
    assert 'Available' in m.telegram_bot.bot.send_message.call_args.kwargs['text']
    assert c.get('/api/admin/character-requests',headers=headers(m)).json()['total']==0
    assert c.get('/api/character-requests',headers=headers(m,202)).json()['requests'][0]['status']=='available'


def test_notification_failure_keeps_visible_result(app_module):
    m=app_module; c=TestClient(m.api)
    rid=c.post('/api/character-requests',headers=headers(m,202),json=PAYLOAD).json()['request_id']
    m.telegram_bot.bot.send_message.side_effect=Forbidden('blocked')
    r=c.post(f'/api/admin/character-requests/{rid}/decision',headers=headers(m),json={'status':'unavailable'})
    assert r.status_code==200 and r.json()['notification_sent'] is False
    assert c.get('/api/character-requests',headers=headers(m,202)).json()['requests'][0]['status']=='unavailable'


def test_pending_limit_and_no_balance_charge(app_module):
    m=app_module; c=TestClient(m.api)
    for i in range(20):
        assert c.post('/api/character-requests',headers=headers(m,202),json={**PAYLOAD,'character_id':f'id-{i}'}).status_code==200
    assert c.post('/api/character-requests',headers=headers(m,202),json=PAYLOAD).status_code==429
    assert c.get('/api/wallet',headers=headers(m,202)).json()['balance_cents']==0
