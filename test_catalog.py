"""Run with python -m unittest test_catalog. Uses only an isolated test database."""
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

os.environ["BOT_TOKEN"] = "123456:test-only-token"
os.environ["ADMIN_IDS"] = "900"
os.environ["DB_PATH"] = "/tmp/creditgenie-test-not-initialized.db"
import app
from fastapi.testclient import TestClient


def headers(user_id):
    values = {"auth_date": str(int(time.time())),
              "user": json.dumps({"id": user_id, "first_name": "Test", "username": "test_user"})}
    checked = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", app.BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(values)}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, "DB_PATH", self.tmp.name + "/test.db")
        self.db_patch.start()
        app.init_db()
        self.sender = AsyncMock()
        self.bot_patch = patch.object(type(app.telegram_bot.bot), "send_message", self.sender)
        self.bot_patch.start()
        self.client = TestClient(app.api, raise_server_exceptions=False)
        self.member = headers(100)
        self.admin = headers(900)
        self.client.get("/api/me", headers=self.member)
        self.set_balance(3000)

    def tearDown(self):
        self.client.close()
        self.bot_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def set_balance(self, cents):
        with app.db() as con:
            con.execute("UPDATE users SET balance_cents=? WHERE telegram_id=100", (cents,))

    def balance(self):
        return self.client.get("/api/wallet", headers=self.member).json()["balance_cents"]

    def submit(self, catalog_id="nova-scout"):
        response = self.client.post("/api/catalog/orders", headers=self.member,
            json={"catalog_id": catalog_id, "agreed_price_cents": 1300})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def decide(self, order_id, status="released", auth=None):
        return self.client.post(f"/api/admin/catalog/orders/{order_id}/decision",
            headers=auth or self.admin, json={"status": status})

    def test_approval_charges_once_and_delivers_owner_card(self):
        order = self.submit()["order"]
        self.assertEqual(self.balance(), 3000)
        self.assertEqual(self.decide(order["id"]).status_code, 200)
        self.assertEqual(self.balance(), 1700)
        again = self.decide(order["id"]).json()
        self.assertTrue(again["already_processed"])
        self.assertEqual(self.balance(), 1700)
        history = self.client.get("/api/catalog/orders", headers=self.member).json()["orders"]
        self.assertTrue(history[0]["player"]["player_id"].startswith("CG-"))
        self.assertEqual(history[0]["charged_cents"], 1300)
        self.assertEqual(self.client.get("/api/catalog/orders", headers=headers(200)).json()["orders"], [])
        self.assertEqual(self.sender.await_count, 1)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM catalog_charges").fetchone()[0], 1)

    def test_decline_is_free_and_cannot_be_reapproved(self):
        order = self.submit()["order"]
        self.assertEqual(self.decide(order["id"], "rejected").status_code, 200)
        self.assertEqual(self.balance(), 3000)
        self.assertEqual(self.decide(order["id"]).status_code, 409)

    def test_insufficient_balance_stays_pending_and_exact_balance_works(self):
        self.set_balance(1299)
        order = self.submit()["order"]
        self.assertEqual(self.decide(order["id"]).status_code, 409)
        self.assertEqual(self.balance(), 1299)
        history = self.client.get("/api/catalog/orders", headers=self.member).json()["orders"]
        self.assertEqual(history[0]["status"], "pending")
        self.assertIsNone(history[0]["player"])
        self.sender.assert_not_awaited()
        self.set_balance(1300)
        self.assertEqual(self.decide(order["id"]).status_code, 200)
        self.assertEqual(self.balance(), 0)

    def test_duplicate_submit_and_approval_races(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            orders = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(orders[0]["order"]["id"], orders[1]["order"]["id"])
        order_id = orders[0]["order"]["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.decide(order_id), range(2)))
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(self.balance(), 1700)
        self.assertEqual(self.sender.await_count, 1)

    def test_two_orders_cannot_overdraw_wallet(self):
        self.set_balance(1300)
        ids = [self.submit(key)["order"]["id"] for key in ("nova-scout", "ember-mage")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.decide, ids))
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        self.assertEqual(self.balance(), 0)

    def test_database_failure_rolls_back_charge_and_delivery(self):
        order_id = self.submit()["order"]["id"]
        with app.db() as con:
            con.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON catalog_charges "
                        "BEGIN SELECT RAISE(ABORT, 'test failure'); END;")
        self.assertEqual(self.decide(order_id).status_code, 500)
        self.assertEqual(self.balance(), 3000)
        with app.db() as con:
            row = con.execute("SELECT status,player_json FROM catalog_orders WHERE id=?", (order_id,)).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["player_json"])

    def test_notification_failure_preserves_card_and_does_not_recharge(self):
        self.sender.side_effect = RuntimeError("mock Telegram failure")
        order_id = self.submit()["order"]["id"]
        response = self.decide(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["notification_sent"])
        self.assertEqual(self.decide(order_id).status_code, 200)
        self.assertEqual(self.balance(), 1700)
        self.assertEqual(self.sender.await_count, 1)

    def test_auth_and_fixed_catalog_price(self):
        self.assertEqual(self.client.get("/api/catalog").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/catalog/orders", headers=self.member).status_code, 403)
        order_id = self.submit()["order"]["id"]
        self.assertEqual(self.decide(order_id, auth=self.member).status_code, 403)
        for payload in (
            {"catalog_id": "nova-scout", "agreed_price_cents": 1},
            {"catalog_id": "unknown", "agreed_price_cents": 1300},
            {"catalog_id": "nova-scout", "agreed_price_cents": 1300, "player": {"name": "arbitrary"}},
        ):
            self.assertEqual(self.client.post("/api/catalog/orders", headers=self.member, json=payload).status_code, 422)
        self.assertEqual(self.balance(), 3000)

    def test_schema_upgrade_is_additive_and_repeatable(self):
        self.submit()
        app.init_db()
        app.init_db()
        self.assertEqual(self.balance(), 3000)
        self.assertEqual(len(self.client.get("/api/catalog/orders", headers=self.member).json()["orders"]), 1)
        for path in ("/api/me", "/api/wallet", "/api/deposits", "/health",
                     "/api/admin/members", "/api/admin/deposits", "/api/admin/character-requests"):
            self.assertEqual(self.client.get(path, headers=self.admin).status_code, 200, path)
        self.assertEqual(self.client.get("/characters").status_code, 200)


if __name__ == "__main__":
    unittest.main()
