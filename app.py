import os
import json
import hmac
import hashlib
import sqlite3
import logging
import time
import uuid
from pathlib import Path
from typing import Literal

from datetime import datetime, timezone
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from telegram.error import Forbidden, BadRequest, RetryAfter, TimedOut, NetworkError, TelegramError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)


# -------------------------------------------------
# ENVIRONMENT
# -------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    ""
).strip()

MINI_APP_URL = os.getenv(
    "MINI_APP_URL",
    ""
).strip()

DB_PATH = os.getenv(
    "DB_PATH",
    "creditgenie.db"
).strip()

# Accept the specific copied form-label typo without mutating the environment.
if DB_PATH.startswith("VALUE") and DB_PATH[5:].strip() == "/var/data/creditgenie.db":
    DB_PATH = "/var/data/creditgenie.db"

BTC_DEPOSIT_ADDRESS = os.getenv(
    "BTC_DEPOSIT_ADDRESS",
    ""
).strip()

SUPPORT_USERNAME = os.getenv(
    "SUPPORT_USERNAME",
    "@YourSupport"
).strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv(
        "ADMIN_IDS",
        ""
    ).split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing"
    )


# -------------------------------------------------
# LOGGING
# -------------------------------------------------

logging.basicConfig(
    level=logging.INFO
)

# Telegram request URLs contain the bot credential; never log them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(
    "creditgenie"
)


# -------------------------------------------------
# HELPERS
# -------------------------------------------------

def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    )


def money(cents):

    return (
        f"${cents / 100:,.2f}"
    )


def db():

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(
        DB_PATH
    )

    con.row_factory = (
        sqlite3.Row
    )

    return con


def is_admin(
    telegram_id
):

    try:

        return int(
            telegram_id
        ) in ADMIN_IDS

    except (
        TypeError,
        ValueError
    ):

        return False


# -------------------------------------------------
# DATABASE
# -------------------------------------------------

def init_db():

    with db() as con:

        con.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS gift_card_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                request_id TEXT NOT NULL,
                brand_id TEXT NOT NULL,
                brand_name TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents BETWEEN 40000 AND 100000),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                delivery_details TEXT,
                refunded_cents INTEGER NOT NULL DEFAULT 0,
                reviewed_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id,request_id)
            );
            CREATE INDEX IF NOT EXISTS idx_gift_orders_user ON gift_card_orders(user_id,id DESC);
            CREATE INDEX IF NOT EXISTS idx_gift_orders_status ON gift_card_orders(status,id);

            CREATE TABLE IF NOT EXISTS member_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
                body TEXT NOT NULL,
                telegram_message_id INTEGER,
                created_at TEXT NOT NULL,
                read_at TEXT,
                UNIQUE(user_id,direction,telegram_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_user ON member_messages(user_id,id);
            CREATE TABLE IF NOT EXISTS bank_job_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                player_id TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                character_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','declined')),
                agreed_price_cents INTEGER NOT NULL CHECK(agreed_price_cents=1300),
                charged_cents INTEGER NOT NULL DEFAULT 0,
                approval_note TEXT,
                reviewed_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_job_pending
                ON bank_job_requests(user_id,role) WHERE status='pending';

            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                first_name TEXT,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id INTEGER NOT NULL,

                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,

                state TEXT,
                city TEXT,
                zip TEXT,
                dob TEXT,

                status TEXT NOT NULL DEFAULT 'pending',

                result_text TEXT,
                admin_note TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY(user_id)
                REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS character_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                character_name TEXT NOT NULL,
                game TEXT NOT NULL,
                character_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','available','unavailable')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_characters_user ON character_requests(user_id,id DESC);
            CREATE INDEX IF NOT EXISTS idx_characters_pending ON character_requests(status,id);

            CREATE TABLE IF NOT EXISTS catalog_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                catalog_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','released','rejected')),
                agreed_price_cents INTEGER NOT NULL CHECK(agreed_price_cents=1300),
                charged_cents INTEGER NOT NULL DEFAULT 0,
                player_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_by INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_active
                ON catalog_orders(user_id,catalog_id) WHERE status IN ('pending','released');
            CREATE TABLE IF NOT EXISTS catalog_charges(
                order_id INTEGER PRIMARY KEY REFERENCES catalog_orders(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                amount_cents INTEGER NOT NULL CHECK(amount_cents=1300),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bitcoin_deposits(
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id INTEGER NOT NULL,

                txid TEXT NOT NULL UNIQUE,

                amount_cents INTEGER NOT NULL DEFAULT 0,

                status TEXT NOT NULL DEFAULT 'pending',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY(user_id)
                REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS
            idx_search_requests_user
            ON search_requests(
                user_id,
                id DESC
            );

            CREATE INDEX IF NOT EXISTS
            idx_search_requests_status
            ON search_requests(
                status,
                id ASC
            );

            CREATE INDEX IF NOT EXISTS
            idx_bitcoin_deposits_user
            ON bitcoin_deposits(
                user_id,
                id DESC
            );

            CREATE INDEX IF NOT EXISTS
            idx_bitcoin_deposits_status
            ON bitcoin_deposits(
                status,
                id ASC
            );
            """
        )

        columns = {row["name"] for row in con.execute("PRAGMA table_info(bitcoin_deposits)")}
        if "requested_amount_cents" not in columns:
            con.execute("ALTER TABLE bitcoin_deposits ADD COLUMN requested_amount_cents INTEGER")


# -------------------------------------------------
# TELEGRAM MINI APP AUTH
# -------------------------------------------------

def verify_init_data(
    init_data: str
):

    if not init_data:

        raise HTTPException(
            status_code=401,
            detail=(
                "Open this page "
                "from Telegram."
            )
        )

    pairs = dict(
        parse_qsl(
            init_data,
            keep_blank_values=True
        )
    )

    received_hash = pairs.pop(
        "hash",
        None
    )

    if not received_hash:

        raise HTTPException(
            status_code=401,
            detail=(
                "Missing Telegram signature."
            )
        )

    data_check_string = "\n".join(
        f"{key}={pairs[key]}"
        for key in sorted(
            pairs
        )
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_hash,
        received_hash
    ):

        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid Telegram signature."
            )
        )

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid Telegram session.")
    age = time.time() - auth_date
    if auth_date <= 0 or age < -60 or age > 86400:
        raise HTTPException(status_code=401, detail="Session expired. Reopen the Mini App from Telegram.")

    try:

        user = json.loads(
            pairs.get(
                "user",
                "{}"
            )
        )

    except Exception:

        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid Telegram user data."
            )
        )

    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:

        raise HTTPException(
            status_code=401,
            detail=(
                "Telegram user not found."
            )
        )

    return user


# -------------------------------------------------
# REQUIRE ADMIN
# -------------------------------------------------

def require_admin(
    init_data: str
):

    tg = verify_init_data(
        init_data
    )

    if not is_admin(
        tg.get("id")
    ):

        raise HTTPException(
            status_code=403,
            detail=(
                "Admin access required."
            )
        )

    return tg


# -------------------------------------------------
# USER DATABASE
# -------------------------------------------------

def get_or_create_user(
    tg
):

    telegram_id = int(
        tg["id"]
    )

    with db() as con:

        con.execute(
            """
            INSERT INTO users(
                telegram_id,
                username,
                first_name,
                created_at
            )

            VALUES(
                ?, ?, ?, ?
            )

            ON CONFLICT(telegram_id)
            DO UPDATE SET
                username =
                    excluded.username,

                first_name =
                    excluded.first_name
            """,
            (
                telegram_id,

                tg.get(
                    "username"
                ),

                tg.get(
                    "first_name",
                    ""
                ),

                now_iso()
            )
        )

        return con.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id=?
            """,
            (
                telegram_id,
            )
        ).fetchone()


# -------------------------------------------------
# MODELS
# -------------------------------------------------


class BitcoinDepositRequest(BaseModel):
    model_config = {"extra": "forbid"}
    amount_cents: int = Field(strict=True, gt=0, le=1000000)
    request_id: uuid.UUID


class CreditDepositRequest(
    BaseModel
):

    deposit_id: int = Field(
        gt=0
    )

    amount_cents: int = Field(
        gt=0,
        le=1000000
    )


# -------------------------------------------------
# TELEGRAM /start
# -------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    get_or_create_user(update.effective_user.to_dict())

    if not MINI_APP_URL:

        await update.message.reply_text(
            "CreditGenie is online, but "
            "MINI_APP_URL is not configured yet."
        )

        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🧞 Open CreditGenie",
                    web_app=WebAppInfo(
                        url=MINI_APP_URL
                    )
                )
            ]
        ]
    )

    await update.message.reply_text(
        "🧞 CreditGenie\n\n"
        "Open the Mini App to manage "
        "your balance and deposits.",
        reply_markup=keyboard
    )


# -------------------------------------------------
# TELEGRAM /admin
# -------------------------------------------------

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin access required.")
        return
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        deposits = con.execute("SELECT COUNT(*) FROM bitcoin_deposits WHERE status='pending'").fetchone()[0]
    await update.message.reply_text(
        f"🛠 CreditGenie Admin\n\nMembers: {users}\nPending deposits: {deposits}\n\n"
        "Open the Mini App's Admin Control Center to view members, send individual messages, and review deposits."
    )


# -------------------------------------------------
# TELEGRAM BOT
# -------------------------------------------------

telegram_bot = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .build()
)

telegram_bot.add_handler(
    CommandHandler(
        "start",
        start
    )
)

telegram_bot.add_handler(
    CommandHandler(
        "admin",
        admin
    )
)


# -------------------------------------------------
# FASTAPI STARTUP
# -------------------------------------------------

@asynccontextmanager
async def lifespan(
    app: FastAPI
):

    init_db()

    await telegram_bot.initialize()

    await telegram_bot.start()

    if telegram_bot.updater:

        await telegram_bot.updater.start_polling(
            drop_pending_updates=False
        )

    try:

        yield

    finally:

        if telegram_bot.updater:

            await telegram_bot.updater.stop()

        await telegram_bot.stop()

        await telegram_bot.shutdown()


# -------------------------------------------------
# FASTAPI
# -------------------------------------------------

api = FastAPI(
    title="CreditGenie",
    lifespan=lifespan
)


# -------------------------------------------------
# ROOT
# -------------------------------------------------

@api.get("/")
async def root():

    return {
        "ok": True,
        "service": "CreditGenie"
    }


# -------------------------------------------------
# HEALTH
# -------------------------------------------------

@api.get("/health")
async def health():

    with db() as con:
        con.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return {"ok": True}


# -------------------------------------------------
# MINI APP
# -------------------------------------------------

@api.get("/app")
async def mini_app():

    return FileResponse(
        "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"}
    )


# -------------------------------------------------
# CURRENT USER
# -------------------------------------------------

@api.get("/api/me")
async def me(
    x_telegram_init_data: str = Header(
        default=""
    )
):

    tg = verify_init_data(
        x_telegram_init_data
    )

    user = get_or_create_user(
        tg
    )

    return {
        "first_name":
            user["first_name"],

        "balance":
            money(
                user["balance_cents"]
            ),

        "balance_cents":
            user["balance_cents"],


        "support":
            SUPPORT_USERNAME,

        "is_admin":
            is_admin(
                tg["id"]
            )
    }


# =================================================
# BITCOIN WALLET API
# =================================================


# -------------------------------------------------
# WALLET
# -------------------------------------------------

@api.get("/api/wallet")
async def wallet(
    x_telegram_init_data: str = Header(
        default=""
    )
):

    tg = verify_init_data(
        x_telegram_init_data
    )

    user = get_or_create_user(
        tg
    )

    return {
        "ok": True,

        "balance":
            money(
                user["balance_cents"]
            ),

        "balance_cents":
            user["balance_cents"],

        "deposit_address": BTC_DEPOSIT_ADDRESS if has_pending_deposit(user["id"]) else "",

    }


# -------------------------------------------------
# CREATE DEPOSIT REQUEST
# -------------------------------------------------

def has_pending_deposit(user_id):
    with db() as con:
        return con.execute("SELECT 1 FROM bitcoin_deposits WHERE user_id=? AND status='pending' LIMIT 1", (user_id,)).fetchone() is not None


@api.post("/api/deposit")
async def submit_bitcoin_deposit(req: BitcoinDepositRequest, x_telegram_init_data: str = Header(default="")):
    tg = verify_init_data(x_telegram_init_data)
    user = get_or_create_user(tg)
    if not BTC_DEPOSIT_ADDRESS:
        raise HTTPException(status_code=503, detail="Bitcoin deposits are not configured yet.")
    # Internal request marker preserves legacy NOT NULL/UNIQUE txid schema.
    marker = f"request:{user['id']}:{req.request_id}"
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("SELECT * FROM bitcoin_deposits WHERE txid=?", (marker,)).fetchone()
        if existing:
            if existing["requested_amount_cents"] != req.amount_cents:
                raise HTTPException(status_code=409, detail="This request was already saved with a different amount.")
            deposit_id, status = existing["id"], existing["status"]
        else:
            cur = con.execute("""INSERT INTO bitcoin_deposits
                (user_id,txid,requested_amount_cents,amount_cents,status,created_at,updated_at)
                VALUES(?,?,?,0,'pending',?,?)""",
                (user["id"],marker,req.amount_cents,now_iso(),now_iso()))
            deposit_id, status = cur.lastrowid, "pending"
    return {"ok": True, "deposit_id": deposit_id, "status": status,
            "requested_amount_cents": req.amount_cents, "deposit_address": BTC_DEPOSIT_ADDRESS,
            "message": "Deposit request saved. Your balance is credited only after admin verifies receipt."}


# -------------------------------------------------
# USER DEPOSIT HISTORY
# -------------------------------------------------

@api.get("/api/deposits")
async def my_bitcoin_deposits(
    x_telegram_init_data: str = Header(
        default=""
    )
):

    tg = verify_init_data(
        x_telegram_init_data
    )

    user = get_or_create_user(
        tg
    )

    with db() as con:

        rows = con.execute(
            """
            SELECT
                id,
                txid,
                requested_amount_cents,
                amount_cents,
                status,
                created_at,
                updated_at

            FROM bitcoin_deposits

            WHERE user_id=?

            ORDER BY id DESC

            LIMIT 25
            """,
            (
                user["id"],
            )
        ).fetchall()

    return {
        "ok": True,

        "deposits": [
            {
                **dict(row),
                "txid": None if row["txid"].startswith("request:") else row["txid"],

                "amount":
                    money(
                        row[
                            "amount_cents"
                        ]
                    )
            }

            for row in rows
        ]
    }


# =================================================
# ADMIN MINI APP API
# =================================================


# -------------------------------------------------
# ADMIN STATS
# -------------------------------------------------

@api.get("/api/admin/stats")
async def admin_stats(x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pending_deposits = con.execute("SELECT COUNT(*) FROM bitcoin_deposits WHERE status='pending'").fetchone()[0]
    return {"ok": True, "users": users, "pending_deposits": pending_deposits}


# -------------------------------------------------
# ADMIN BITCOIN DEPOSIT QUEUE
# -------------------------------------------------

@api.get("/api/admin/deposits")
async def admin_deposits(
    x_telegram_init_data: str = Header(
        default=""
    )
):

    require_admin(
        x_telegram_init_data
    )

    with db() as con:

        rows = con.execute(
            """
            SELECT
                d.id,
                d.txid,
                d.user_id,
                d.requested_amount_cents,
                d.amount_cents,
                d.status,
                d.created_at,
                d.updated_at,

                u.telegram_id,
                u.first_name,
                u.username

            FROM bitcoin_deposits d

            JOIN users u
                ON u.id=d.user_id

            WHERE d.status='pending'

            ORDER BY
                d.created_at ASC,
                d.id ASC
            """
        ).fetchall()

    return {
        "ok": True,

        "count":
            len(rows),

        "deposits": [
            {
                **dict(row),
                "txid": None if row["txid"].startswith("request:") else row["txid"],

                "amount":
                    money(
                        row[
                            "amount_cents"
                        ]
                    )
            }

            for row in rows
        ]
    }


# -------------------------------------------------
# ADMIN CREDIT DEPOSIT
# -------------------------------------------------

@api.post(
    "/api/admin/credit-deposit"
)
async def credit_deposit(
    req: CreditDepositRequest,

    x_telegram_init_data: str = Header(
        default=""
    )
):

    admin_tg = require_admin(
        x_telegram_init_data
    )

    with db() as con:

        con.execute("BEGIN IMMEDIATE")

        deposit = con.execute(
            """
            SELECT
                d.*,
                u.telegram_id

            FROM bitcoin_deposits d

            JOIN users u
                ON u.id=d.user_id

            WHERE d.id=?
            """,
            (
                req.deposit_id,
            )
        ).fetchone()

        if not deposit:

            raise HTTPException(
                status_code=404,
                detail=(
                    "Deposit not found."
                )
            )

        if (
            deposit["status"]
            !=
            "pending"
        ):

            raise HTTPException(
                status_code=409,
                detail=(
                    "Deposit has already "
                    "been processed."
                )
            )

        con.execute(
            """
            UPDATE users

            SET balance_cents =
                balance_cents + ?

            WHERE id=?
            """,
            (
                req.amount_cents,
                deposit["user_id"]
            )
        )

        con.execute(
            """
            UPDATE bitcoin_deposits

            SET
                amount_cents=?,
                status='credited',
                updated_at=?

            WHERE
                id=?
                AND status='pending'
            """,
            (
                req.amount_cents,
                now_iso(),
                req.deposit_id
            )
        )

        updated_user = con.execute(
            """
            SELECT balance_cents
            FROM users
            WHERE id=?
            """,
            (
                deposit["user_id"],
            )
        ).fetchone()

    try:

        await telegram_bot.bot.send_message(
            chat_id=deposit[
                "telegram_id"
            ],

            text=(
                "₿ Bitcoin Deposit Credited\n\n"

                f"Amount credited: "
                f"{money(req.amount_cents)}\n"

                f"New balance: "
                f"{money(updated_user['balance_cents'])}\n\n"

                "Open CreditGenie to continue."
            )
        )

    except Exception as exc:

        logger.warning(
            "Could not notify user for deposit %s (%s)",
            req.deposit_id,
            type(exc).__name__
        )

    logger.info(
        "Deposit %s credited by admin %s",
        req.deposit_id,
        admin_tg.get("id")
    )

    return {
        "ok": True,

        "deposit_id":
            req.deposit_id,

        "status":
            "credited",

        "credited":
            money(
                req.amount_cents
            ),

        "balance":
            money(
                updated_user[
                    "balance_cents"
                ]
            )
    }


# Admin-only member directory and individual Telegram messages.
class MemberResponse(BaseModel):
    id: int
    first_name: str | None
    username: str | None
    balance_cents: int
    balance: str
    joined_at: str


class MembersResponse(BaseModel):
    members: list[MemberResponse]
    total: int
    page: int
    page_size: int


class AdminMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)

    @field_validator("message")
    @classmethod
    def clean_message(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Enter a message before sending.")
        # Telegram counts UTF-16 units for its message length limit.
        if len(value.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Message is too long (maximum 4096 characters).")
        return value


class AdminMessageResponse(BaseModel):
    ok: bool
    member_id: int
    message_id: int


@api.get("/api/admin/members", response_model=MembersResponse)
async def admin_members(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    x_telegram_init_data: str = Header(default=""),
):
    require_admin(x_telegram_init_data)
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = con.execute(
            "SELECT id, first_name, username, balance_cents, created_at AS joined_at "
            "FROM users ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "members": [{**dict(row), "balance": money(row["balance_cents"])} for row in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@api.post("/api/admin/members/{member_id}/message", response_model=AdminMessageResponse)
async def admin_message_member(
    member_id: int,
    req: AdminMessageRequest,
    x_telegram_init_data: str = Header(default=""),
):
    admin_tg = require_admin(x_telegram_init_data)
    with db() as con:
        member = con.execute("SELECT telegram_id FROM users WHERE id=?", (member_id,)).fetchone()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found.")
    try:
        sent = await telegram_bot.bot.send_message(
            chat_id=member["telegram_id"], text=req.message, parse_mode=None,
        )
    except Forbidden:
        raise HTTPException(status_code=409, detail="This member blocked the bot or has not allowed messages. Ask them to open CreditGenieBot and press Start.")
    except BadRequest:
        raise HTTPException(status_code=400, detail="Telegram could not accept this message. Check the message and ask the member to press Start in the bot.")
    except RetryAfter as exc:
        delay = exc.retry_after
        seconds = int(delay.total_seconds() if hasattr(delay, "total_seconds") else delay) + 1
        raise HTTPException(status_code=429, detail=f"Telegram rate limit reached. Wait {seconds} seconds before trying again.", headers={"Retry-After": str(seconds)})
    except (TimedOut, NetworkError):
        raise HTTPException(status_code=502, detail="Telegram did not confirm delivery. The message may have arrived; check with the member before resending.")
    except TelegramError:
        raise HTTPException(status_code=502, detail="Telegram could not send the message. Try again later.")
    logger.info("Admin %s sent a message to member %s", admin_tg["id"], member_id)
    with db() as con:
        con.execute("INSERT INTO member_messages(user_id,direction,body,telegram_message_id,created_at) VALUES(?,'outgoing',?,?,?)",
                    (member_id, req.message, sent.message_id, now_iso()))
    return {"ok": True, "member_id": member_id, "message_id": sent.message_id}


# Fictional-character availability requests; independent of archived lookup data.
class CharacterRequestInput(BaseModel):
    model_config = {"extra": "forbid"}
    character_name: str = Field(min_length=1, max_length=80)
    game: str = Field(min_length=1, max_length=80)
    character_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")

    @field_validator("character_name", "game", "character_id", mode="before")
    @classmethod
    def clean_character_field(cls, value):
        if not isinstance(value, str):
            raise ValueError("Enter text for each character field.")
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Enter a single line for each character field.")
        return value


class CharacterDecisionInput(BaseModel):
    model_config = {"extra": "forbid"}
    status: Literal["available", "unavailable"]


@api.post("/api/character-requests")
async def create_character_request(req: CharacterRequestInput, x_telegram_init_data: str = Header(default="")):
    verify_init_data(x_telegram_init_data)
    raise HTTPException(status_code=410, detail="Use the Bank Job character request form. Previous requests are read-only.")


@api.get("/api/character-requests")
async def my_character_requests(x_telegram_init_data: str = Header(default="")):
    user = get_or_create_user(verify_init_data(x_telegram_init_data))
    with db() as con:
        rows = con.execute(
            "SELECT id,character_name,game,character_id,status,created_at,updated_at "
            "FROM character_requests WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],),
        ).fetchall()
    return {"requests": [dict(row) for row in rows]}


@api.get("/api/admin/character-requests")
async def admin_character_requests(x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM character_requests WHERE status='pending'").fetchone()[0]
        rows = con.execute(
            "SELECT c.id,c.character_name,c.game,c.character_id,c.status,c.created_at,u.first_name,u.username "
            "FROM character_requests c JOIN users u ON u.id=c.user_id "
            "WHERE c.status='pending' ORDER BY c.id ASC LIMIT 100"
        ).fetchall()
    return {"requests": [dict(row) for row in rows], "total": total}


@api.post("/api/admin/character-requests/{request_id}/decision")
async def decide_character_request(request_id: int, req: CharacterDecisionInput, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    raise HTTPException(status_code=410, detail="Previous requests are read-only. Review Bank Job requests in the new queue.")


# Separate fictional-character catalog. No personal-data search or free-text delivery.
CATALOG_PRICE_CENTS = 1300
CHARACTER_CATALOG = {
    "nova-scout": {"name": "Nova", "role": "Scout", "ability": "Star dash",
                   "strength": 4, "agility": 9, "magic": 5},
    "atlas-guardian": {"name": "Atlas", "role": "Guardian", "ability": "Stone shield",
                       "strength": 9, "agility": 4, "magic": 5},
    "ember-mage": {"name": "Ember", "role": "Mage", "ability": "Fire spark",
                   "strength": 4, "agility": 5, "magic": 9},
}


class CatalogOrderInput(BaseModel):
    model_config = {"extra": "forbid"}
    catalog_id: Literal["nova-scout", "atlas-guardian", "ember-mage"]
    agreed_price_cents: Literal[1300]


class CatalogDecisionInput(BaseModel):
    model_config = {"extra": "forbid"}
    status: Literal["released", "rejected"]


def catalog_order_view(row):
    result = dict(row)
    player_json = result.pop("player_json", None)
    result["player"] = json.loads(player_json) if player_json else None
    result["character"] = CHARACTER_CATALOG[result["catalog_id"]]["name"]
    return result


@api.get("/characters")
async def character_catalog_page():
    return FileResponse("catalog.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@api.get("/api/catalog")
async def character_catalog(x_telegram_init_data: str = Header(default="")):
    verify_init_data(x_telegram_init_data)
    return {"price_cents": CATALOG_PRICE_CENTS,
            "characters": [{"id": key, **value} for key, value in CHARACTER_CATALOG.items()]}


@api.post("/api/catalog/orders")
async def submit_catalog_order(req: CatalogOrderInput, x_telegram_init_data: str = Header(default="")):
    verify_init_data(x_telegram_init_data)
    raise HTTPException(status_code=410, detail="The character catalog is closed. No charge was made.")


@api.get("/api/catalog/orders")
async def my_catalog_orders(x_telegram_init_data: str = Header(default="")):
    user = get_or_create_user(verify_init_data(x_telegram_init_data))
    with db() as con:
        rows = con.execute(
            "SELECT * FROM catalog_orders WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (user["id"],),
        ).fetchall()
    return {"orders": [catalog_order_view(row) for row in rows]}


@api.get("/api/admin/catalog/orders")
async def admin_catalog_orders(x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    with db() as con:
        rows = con.execute(
            "SELECT o.*,u.first_name,u.username,u.balance_cents FROM catalog_orders o "
            "JOIN users u ON u.id=o.user_id WHERE o.status='pending' ORDER BY o.id LIMIT 100"
        ).fetchall()
        total = con.execute("SELECT COUNT(*) FROM catalog_orders WHERE status='pending'").fetchone()[0]
    return {"orders": [catalog_order_view(row) for row in rows], "total": total}


@api.post("/api/admin/catalog/orders/{order_id}/decision")
async def decide_catalog_order(order_id: int, req: CatalogDecisionInput,
                               x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    raise HTTPException(status_code=410, detail="The character catalog is closed. No charge was made.")

# Bank Job: server-created fictional records, separate from previous profile requests.
BANK_JOB_PRICE = 1300
BANK_JOB_ROLES = {
    'driver': {'alias': 'Ghost', 'name': 'Alex Ghost Mercer', 'fictional_birthday': '1995-06-14', 'role': 'Driver', 'state': 'Neon'},
    'scout': {'alias': 'Night', 'name': 'Riley Night Vale', 'fictional_birthday': '1997-09-22', 'role': 'Scout', 'state': 'Harbor'},
    'planner': {'alias': 'Cipher', 'name': 'Morgan Cipher Reed', 'fictional_birthday': '1993-02-18', 'role': 'Planner', 'state': 'Summit'},
}


class BankJobRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    alias: str = Field(min_length=1, max_length=80)
    agreed_price_cents: Literal[1300]


class BankJobDecision(BaseModel):
    model_config = {'extra': 'forbid'}
    status: Literal['approved', 'declined']
    note: str = Field(min_length=1, max_length=1500)

    @field_validator('note')
    @classmethod
    def note_not_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('Write a note for the requester.')
        return value


def bank_job_view(row):
    item = dict(row)
    item['character'] = json.loads(item.pop('character_json'))
    return item


def bank_job_alias_role(alias: str):
    normalized = alias.strip().casefold()
    for role, character in BANK_JOB_ROLES.items():
        if character['alias'].casefold() == normalized:
            return role
    raise HTTPException(404, 'No Bank Job profile matches that alias.')


@api.get('/api/bank-job/profiles/search')
async def search_bank_job_alias(name: str = Query(min_length=1, max_length=80),
                                birth_year: int = Query(ge=1900, le=2200),
                                state: str = Query(min_length=1, max_length=40),
                                x_telegram_init_data: str = Header(default='')):
    verify_init_data(x_telegram_init_data)
    for profile in BANK_JOB_ROLES.values():
        if (name.strip().casefold() in (profile['name'].casefold(), profile['alias'].casefold())
                and birth_year == int(profile['fictional_birthday'][:4])
                and state.strip().casefold() == profile['state'].casefold()):
            return {'profile': profile, 'price_cents': BANK_JOB_PRICE}
    raise HTTPException(404, 'No stored fictional Bank Job profile matches these details.')


@api.post('/api/bank-job/requests')
async def request_bank_job(req: BankJobRequest, x_telegram_init_data: str = Header(default='')):
    user = get_or_create_user(verify_init_data(x_telegram_init_data))
    role = bank_job_alias_role(req.alias)
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute("SELECT * FROM bank_job_requests WHERE user_id=? AND role=? AND status='pending'",
                               (user['id'], role)).fetchone()
        if existing:
            return {'ok': True, 'already_pending': True, 'request': bank_job_view(existing)}
        stamp = now_iso()
        cur = con.execute('INSERT INTO bank_job_requests(user_id,player_id,role,character_json,agreed_price_cents,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                          (user['id'], 'BJ-' + uuid.uuid4().hex[:16].upper(), role,
                           json.dumps(BANK_JOB_ROLES[role]), BANK_JOB_PRICE, stamp, stamp))
        row = con.execute('SELECT * FROM bank_job_requests WHERE id=?', (cur.lastrowid,)).fetchone()
    return {'ok': True, 'already_pending': False, 'request': bank_job_view(row)}


@api.get('/api/bank-job/requests')
async def my_bank_job_requests(x_telegram_init_data: str = Header(default='')):
    user = get_or_create_user(verify_init_data(x_telegram_init_data))
    with db() as con:
        rows = con.execute('SELECT * FROM bank_job_requests WHERE user_id=? ORDER BY id DESC LIMIT 100', (user['id'],)).fetchall()
    return {'requests': [bank_job_view(row) for row in rows]}


@api.get('/api/admin/bank-job/requests')
async def bank_job_queue(x_telegram_init_data: str = Header(default='')):
    require_admin(x_telegram_init_data)
    with db() as con:
        rows = con.execute("SELECT b.*,u.first_name,u.username,u.balance_cents FROM bank_job_requests b JOIN users u ON u.id=b.user_id WHERE b.status='pending' ORDER BY b.id LIMIT 100").fetchall()
        total = con.execute("SELECT COUNT(*) FROM bank_job_requests WHERE status='pending'").fetchone()[0]
    return {'requests': [bank_job_view(row) for row in rows], 'total': total}


@api.post('/api/admin/bank-job/requests/{request_id}/decision')
async def bank_job_decision(request_id: int, req: BankJobDecision, x_telegram_init_data: str = Header(default='')):
    admin_user = require_admin(x_telegram_init_data)
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT b.*,u.telegram_id FROM bank_job_requests b JOIN users u ON u.id=b.user_id WHERE b.id=?', (request_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Bank Job request not found.')
        if row['status'] != 'pending':
            if row['status'] == req.status and row['approval_note'] == req.note:
                return {'ok': True, 'already_processed': True, 'status': row['status'], 'notification_sent': False}
            raise HTTPException(409, 'Request already reviewed. Refresh the queue.')
        charged = 0
        if req.status == 'approved':
            changed = con.execute('UPDATE users SET balance_cents=balance_cents-? WHERE id=? AND balance_cents>=?',
                                  (BANK_JOB_PRICE, row['user_id'], BANK_JOB_PRICE))
            if changed.rowcount != 1:
                raise HTTPException(409, 'Member needs $13.00 in their wallet. Request remains pending; no charge made.')
            charged = BANK_JOB_PRICE
        con.execute('UPDATE bank_job_requests SET status=?,charged_cents=?,approval_note=?,reviewed_by=?,updated_at=? WHERE id=?',
                    (req.status, charged, req.note, admin_user['id'], now_iso(), request_id))
    # Save the decision before trying Telegram. A failed notification never repeats a charge.
    sent = False
    character = json.loads(row['character_json'])
    try:
        await telegram_bot.bot.send_message(chat_id=row['telegram_id'], parse_mode=None,
            text=(f"Bank Job request #{request_id}: {req.status}\n"
                  f"Fictional character: {character['name']}\nPlayer ID: {row['player_id']}\n"
                  f"Fictional birthday: {character['fictional_birthday']}\nRole: {character['role']}\n"
                  f"Wallet charge: {money(charged)}\n\nAdmin note:\n{req.note}\n\nSaved in My Profile Searches."),
            connect_timeout=5, read_timeout=10)
        sent = True
    except TelegramError as exc:
        logger.warning('Bank Job %s notification unconfirmed (%s)', request_id, type(exc).__name__)
    return {'ok': True, 'already_processed': False, 'status': req.status, 'notification_sent': sent}


async def receive_member_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message or update.effective_chat.type != 'private':
        return
    user = get_or_create_user(update.effective_user.to_dict())
    body = update.message.text or update.message.caption or '[Attachment received in Telegram; text replies are shown here.]'
    with db() as con:
        con.execute("INSERT OR IGNORE INTO member_messages(user_id,direction,body,telegram_message_id,created_at) VALUES(?,'incoming',?,?,?)",
                    (user['id'], body, update.message.message_id, now_iso()))


telegram_bot.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, receive_member_message))


@api.get('/api/admin/inbox')
async def admin_inbox(page: int = Query(default=1, ge=1), x_telegram_init_data: str = Header(default='')):
    require_admin(x_telegram_init_data)
    with db() as con:
        total = con.execute('SELECT COUNT(DISTINCT user_id) FROM member_messages').fetchone()[0]
        rows = con.execute("""SELECT u.id,u.first_name,u.username,m.body,m.created_at,
            (SELECT COUNT(*) FROM member_messages unread WHERE unread.user_id=u.id AND unread.direction='incoming' AND unread.read_at IS NULL) AS unread
            FROM users u JOIN member_messages m ON m.id=(SELECT MAX(id) FROM member_messages WHERE user_id=u.id)
            ORDER BY m.id DESC LIMIT 50 OFFSET ?""", ((page-1)*50,)).fetchall()
    return {'threads': [dict(row) for row in rows], 'total': total, 'page': page}


@api.get('/api/admin/members/{member_id}/messages')
async def admin_conversation(member_id: int, before_id: int = Query(default=0, ge=0), x_telegram_init_data: str = Header(default='')):
    require_admin(x_telegram_init_data)
    with db() as con:
        member = con.execute('SELECT id,first_name,username FROM users WHERE id=?', (member_id,)).fetchone()
        if not member:
            raise HTTPException(404, 'Member not found.')
        rows = con.execute('SELECT * FROM member_messages WHERE user_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 100',
                           (member_id, before_id, before_id)).fetchall()
    return {'member': dict(member), 'messages': [dict(row) for row in reversed(rows)], 'has_more': len(rows)==100}


class ReadMessages(BaseModel):
    through_id: int = Field(gt=0)


@api.post('/api/admin/members/{member_id}/messages/read')
async def read_conversation(member_id: int, req: ReadMessages, x_telegram_init_data: str = Header(default='')):
    require_admin(x_telegram_init_data)
    with db() as con:
        con.execute("UPDATE member_messages SET read_at=? WHERE user_id=? AND id<=? AND direction='incoming' AND read_at IS NULL",
                    (now_iso(), member_id, req.through_id))
    return {'ok': True}



# Gift cards: wallet-funded orders with manual fulfillment.
GIFT_CARD_MIN_CENTS = 40000
GIFT_CARD_MAX_CENTS = 100000
_GIFT_CARD_DATA = json.loads(Path(__file__).with_name('gift_cards.json').read_text())
GIFT_CARD_BRANDS = {
    hashlib.sha256(name.encode()).hexdigest()[:16]: name
    for name in sorted(_GIFT_CARD_DATA['brands'], key=str.casefold)
}


@api.middleware('http')
async def gift_card_no_cache(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(('/api/gift-cards', '/api/admin/gift-cards')):
        response.headers['Cache-Control'] = 'no-store'
    return response


class GiftCardPurchase(BaseModel):
    model_config = {'extra': 'forbid'}
    brand_id: str = Field(min_length=1, max_length=40)
    amount_cents: int = Field(strict=True, ge=GIFT_CARD_MIN_CENTS, le=GIFT_CARD_MAX_CENTS)
    request_id: uuid.UUID


class GiftCardDecision(BaseModel):
    model_config = {'extra': 'forbid'}
    status: Literal['approved', 'rejected']
    details: str = Field(min_length=1, max_length=3000)

    @field_validator('details')
    @classmethod
    def details_required(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('Enter gift card details or a rejection reason.')
        return value


@api.get('/api/gift-cards')
async def gift_card_catalog(x_telegram_init_data: str = Header(default='')):
    verify_init_data(x_telegram_init_data)
    return {'brands': [{'id': key, 'name': name} for key, name in GIFT_CARD_BRANDS.items()],
            'min_cents': GIFT_CARD_MIN_CENTS, 'max_cents': GIFT_CARD_MAX_CENTS,
            'currency': 'USD'}


@api.post('/api/gift-cards/orders')
async def buy_gift_card(req: GiftCardPurchase, x_telegram_init_data: str = Header(default='')):
    user = get_or_create_user(verify_init_data(x_telegram_init_data))
    if req.brand_id not in GIFT_CARD_BRANDS:
        raise HTTPException(422, 'Choose a gift card from the catalog.')
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT * FROM gift_card_orders WHERE user_id=? AND request_id=?',
                               (user['id'], str(req.request_id))).fetchone()
        if existing:
            if existing['brand_id'] != req.brand_id or existing['amount_cents'] != req.amount_cents:
                raise HTTPException(409, 'This purchase was already saved with different details.')
            return {'ok': True, 'already_processed': True, 'order': dict(existing)}
        # Debit and order creation commit together. Competing purchases cannot overspend.
        changed = con.execute('UPDATE users SET balance_cents=balance_cents-? WHERE id=? AND balance_cents>=?',
                              (req.amount_cents, user['id'], req.amount_cents))
        if changed.rowcount != 1:
            raise HTTPException(409, 'Insufficient balance. Add funds and wait for deposit approval before purchasing.')
        stamp = now_iso()
        cur = con.execute('''INSERT INTO gift_card_orders
            (user_id,request_id,brand_id,brand_name,amount_cents,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)''',
            (user['id'], str(req.request_id), req.brand_id, GIFT_CARD_BRANDS[req.brand_id], req.amount_cents, stamp, stamp))
        row = con.execute('SELECT * FROM gift_card_orders WHERE id=?', (cur.lastrowid,)).fetchone()
    return {'ok': True, 'already_processed': False, 'order': dict(row)}


@api.get('/api/gift-cards/orders')
async def my_gift_card_orders(page: int = Query(default=1, ge=1), x_telegram_init_data: str = Header(default='')):
    user = get_or_create_user(verify_init_data(x_telegram_init_data))
    with db() as con:
        rows = con.execute('SELECT * FROM gift_card_orders WHERE user_id=? ORDER BY id DESC LIMIT 25 OFFSET ?',
                           (user['id'], (page-1)*25)).fetchall()
        total = con.execute('SELECT COUNT(*) FROM gift_card_orders WHERE user_id=?', (user['id'],)).fetchone()[0]
    return {'orders': [dict(row) for row in rows], 'page': page, 'total': total}


@api.get('/api/admin/gift-cards/orders')
async def admin_gift_card_orders(page: int = Query(default=1, ge=1),
                                 status: Literal['pending', 'approved', 'rejected'] = 'pending',
                                 x_telegram_init_data: str = Header(default='')):
    require_admin(x_telegram_init_data)
    with db() as con:
        rows = con.execute('''SELECT g.*,u.first_name,u.username,u.telegram_id FROM gift_card_orders g
            JOIN users u ON u.id=g.user_id WHERE g.status=? ORDER BY g.id DESC LIMIT 25 OFFSET ?''',
            (status, (page-1)*25)).fetchall()
        total = con.execute('SELECT COUNT(*) FROM gift_card_orders WHERE status=?', (status,)).fetchone()[0]
    return {'orders': [dict(row) for row in rows], 'page': page, 'total': total}


@api.post('/api/admin/gift-cards/orders/{order_id}/decision')
async def decide_gift_card(order_id: int, req: GiftCardDecision, x_telegram_init_data: str = Header(default='')):
    reviewer = require_admin(x_telegram_init_data)
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('''SELECT g.*,u.telegram_id FROM gift_card_orders g
            JOIN users u ON u.id=g.user_id WHERE g.id=?''', (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Gift card order not found.')
        if row['status'] != 'pending':
            if row['status'] == req.status and row['delivery_details'] == req.details:
                return {'ok': True, 'already_processed': True, 'status': row['status'], 'notification_sent': False}
            raise HTTPException(409, 'This order was already reviewed. Refresh the queue.')
        refunded = row['amount_cents'] if req.status == 'rejected' else 0
        if refunded:
            con.execute('UPDATE users SET balance_cents=balance_cents+? WHERE id=?', (refunded, row['user_id']))
        con.execute('''UPDATE gift_card_orders SET status=?,delivery_details=?,refunded_cents=?,reviewed_by=?,updated_at=?
            WHERE id=?''', (req.status, req.details, refunded, reviewer['id'], now_iso(), order_id))
    # Only notify after the durable decision. Codes stay in the owner's authenticated history.
    sent = False
    try:
        await telegram_bot.bot.send_message(chat_id=row['telegram_id'], parse_mode=None,
            text=(f"Gift card order #{order_id}: {req.status}\n{row['brand_name']} · {money(row['amount_cents'])}\n"
                  + ('Your wallet has been refunded. ' if refunded else '')
                  + 'Open CreditGenie → My Gift Cards to view the details.'),
            connect_timeout=5, read_timeout=10)
        sent = True
    except TelegramError as exc:
        logger.warning('Gift card order %s notification unconfirmed (%s)', order_id, type(exc).__name__)
    return {'ok': True, 'already_processed': False, 'status': req.status, 'notification_sent': sent}
