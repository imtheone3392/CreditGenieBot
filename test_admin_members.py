"""Local integration tests: isolated SQLite and a mocked Telegram transport."""
import asyncio
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from telegram.error import Forbidden, BadRequest, RetryAfter, TimedOut


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:local-test-credential")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "persistent" / "test.db"))
    monkeypatch.setenv("ADMIN_IDS", "101")
    monkeypatch.setenv("MINI_APP_URL", "https://example.test/app")
    monkeypatch.setenv("BTC_DEPOSIT_ADDRESS", "test-address")
    spec = importlib.util.spec_from_file_location("isolated_app", Path(__file__).parent / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.init_db()
    module.telegram_bot = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123))))
    return module


def headers(module, uid=101, when=None):
    values = {"auth_date": str(int(time.time()) if when is None else when), "user": json.dumps({"id": uid, "first_name": "Test", "username": f"test{uid}"})}
    secret = hmac.new(b"WebAppData", module.BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, "\n".join(f"{k}={values[k]}" for k in sorted(values)).encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(values)}


def member(module, uid=202, **overrides):
    return module.get_or_create_user({"id": uid, "first_name": "Alex", "username": "alex", **overrides})


def test_directory_authorization_and_all_pages(app_module):
    m = app_module
    client = TestClient(m.api)
    for i in range(105):
        member(m, 200 + i, first_name="<script>example</script>", username=None)
    assert client.get("/api/admin/members").status_code == 401
    assert client.get("/api/admin/members", headers=headers(m, 202)).status_code == 403
    assert client.get("/api/admin/members", headers=headers(m, when=int(time.time())-86401)).status_code == 401
    tampered = headers(m)
    tampered["X-Telegram-Init-Data"] += "&auth_date=1"
    assert client.get("/api/admin/members", headers=tampered).status_code == 401
    rows = []
    for page in range(1, 4):
        response = client.get(f"/api/admin/members?page={page}", headers=headers(m))
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 105
        rows.extend(data["members"])
    assert len({row["id"] for row in rows}) == 105
    assert rows[0]["username"] is None
    assert rows[0]["balance"] == "$0.00"
    assert rows[0]["joined_at"]
    assert "telegram_id" not in rows[0]
    assert client.get("/api/admin/members?page_size=101", headers=headers(m)).status_code == 422


def test_send_only_to_registered_member_and_keep_balances(app_module):
    m = app_module
    user = member(m)
    with m.db() as con:
        con.execute("UPDATE users SET balance_cents=4523 WHERE id=?", (user["id"],))
    client = TestClient(m.api)
    path = f"/api/admin/members/{user['id']}/message"
    assert client.post(path, json={"message": "Hello"}).status_code == 401
    assert client.post(path, headers=headers(m, 202), json={"message": "Hello"}).status_code == 403
    assert client.post("/api/admin/members/9999/message", headers=headers(m), json={"message": "Hello"}).status_code == 404
    for content in ["", "   ", "x" * 4097, "😀" * 2049]:
        assert client.post(path, headers=headers(m), json={"message": content}).status_code == 422
    m.telegram_bot.bot.send_message.assert_not_called()
    response = client.post(path, headers=headers(m), json={"message": "  Hello <Alex> & welcome!  "})
    assert response.status_code == 200
    assert response.json()["message_id"] == 123
    m.telegram_bot.bot.send_message.assert_awaited_once_with(chat_id=202, text="Hello <Alex> & welcome!", parse_mode=None)
    directory = client.get("/api/admin/members", headers=headers(m)).json()
    assert directory["members"][0]["balance_cents"] == 4523


@pytest.mark.parametrize("failure,status", [(Forbidden("blocked"),409), (BadRequest("bad"),400), (RetryAfter(5),429), (TimedOut(),502)])
def test_delivery_errors_do_not_claim_success(app_module, failure, status):
    m = app_module
    user = member(m)
    m.telegram_bot.bot.send_message.side_effect = failure
    response = TestClient(m.api).post(f"/api/admin/members/{user['id']}/message", headers=headers(m), json={"message": "Hello"})
    assert response.status_code == status
    assert m.BOT_TOKEN not in response.text
    if status == 429:
        assert int(response.headers["Retry-After"]) >= 5


def test_existing_database_survives_and_deposits_still_work(app_module):
    m = app_module
    user = member(m)
    joined = user["created_at"]
    with m.db() as con:
        con.execute("UPDATE users SET balance_cents=2500 WHERE id=?", (user["id"],))
        con.execute("INSERT INTO search_requests(user_id,first_name,last_name,created_at,updated_at) VALUES(?,?,?,?,?)", (user["id"], "Archived", "Record", joined, joined))
    m.init_db()
    refreshed = member(m, first_name="Updated")
    assert refreshed["created_at"] == joined
    assert refreshed["balance_cents"] == 2500
    client = TestClient(m.api)
    deposit_request = {"amount_cents":1000,"request_id":str(uuid.uuid4())}
    response = client.post("/api/deposit", headers=headers(m,202), json=deposit_request)
    deposit_id = response.json()["deposit_id"]
    assert response.status_code == 200
    retry = client.post("/api/deposit", headers=headers(m,202), json=deposit_request)
    assert retry.status_code == 200
    assert retry.json()["deposit_id"] == deposit_id
    assert client.get("/api/wallet", headers=headers(m,202)).json()["deposit_address"] == "test-address"
    payload = {"deposit_id":deposit_id,"amount_cents":1500}
    assert client.post("/api/admin/credit-deposit", headers=headers(m,202), json=payload).status_code == 403
    assert len(client.get("/api/admin/deposits", headers=headers(m)).json()["deposits"]) == 1
    assert client.post("/api/admin/credit-deposit", headers=headers(m), json=payload).status_code == 200
    assert client.post("/api/admin/credit-deposit", headers=headers(m), json=payload).status_code == 409
    assert client.get("/api/wallet", headers=headers(m,202)).json()["balance_cents"] == 4000
    assert client.get("/api/wallet", headers=headers(m,202)).json()["deposit_address"] == ""
    assert client.get("/api/deposits", headers=headers(m,202)).json()["deposits"][0]["status"] == "credited"
    assert client.get("/health").status_code == 200
    with m.db() as con:
        assert con.execute("SELECT COUNT(*) FROM search_requests").fetchone()[0] == 1


def test_lookup_routes_removed(app_module):
    client = TestClient(app_module.api)
    for path in ["/api/search", "/api/admin/send-result", "/api/admin/reject"]:
        assert client.post(path, headers=headers(app_module), json={}).status_code == 404
    for path in ["/api/requests", "/api/admin/queue", "/api/admin/request/1", "/api/admin/recent"]:
        assert client.get(path, headers=headers(app_module)).status_code == 404
    html = (Path(__file__).parent / "index.html").read_text().lower()
    assert "peoplefinder" not in html
    assert "searchform" not in html


def test_start_registers_member_without_resetting_account(app_module):
    m = app_module
    update = SimpleNamespace(effective_user=SimpleNamespace(to_dict=lambda: {"id":303,"first_name":"New"}), message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(m.start(update, SimpleNamespace()))
    with m.db() as con:
        assert con.execute("SELECT first_name FROM users WHERE telegram_id=303").fetchone()[0] == "New"

