import os
import json
import hmac
import hashlib
import sqlite3
import logging
import time
from pathlib import Path

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


class BitcoinDepositRequest(
    BaseModel
):

    txid: str = Field(
        min_length=10,
        max_length=150
    )


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
            drop_pending_updates=True
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
        media_type="text/html"
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

        "deposit_address":
            BTC_DEPOSIT_ADDRESS,

    }


# -------------------------------------------------
# SUBMIT BITCOIN TRANSACTION
# -------------------------------------------------

@api.post("/api/deposit")
async def submit_bitcoin_deposit(
    req: BitcoinDepositRequest,

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

    txid = (
        req.txid
        .strip()
        .lower()
    )

    if (
        len(txid) != 64
        or
        any(
            c not in "0123456789abcdef"
            for c in txid
        )
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Enter a valid Bitcoin "
                "transaction ID."
            )
        )

    with db() as con:

        existing = con.execute(
            """
            SELECT id
            FROM bitcoin_deposits
            WHERE txid=?
            """,
            (
                txid,
            )
        ).fetchone()

        if existing:

            raise HTTPException(
                status_code=409,
                detail=(
                    "This Bitcoin transaction "
                    "has already been submitted."
                )
            )

        cur = con.execute(
            """
            INSERT INTO bitcoin_deposits(
                user_id,
                txid,
                amount_cents,
                status,
                created_at,
                updated_at
            )

            VALUES(
                ?,
                ?,
                0,
                'pending',
                ?,
                ?
            )
            """,
            (
                user["id"],
                txid,
                now_iso(),
                now_iso()
            )
        )

        deposit_id = (
            cur.lastrowid
        )

    return {
        "ok": True,

        "deposit_id":
            deposit_id,

        "status":
            "pending",

        "message":
            (
                "Bitcoin transaction submitted "
                "for verification."
            )
    }


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
    return {"ok": True, "member_id": member_id, "message_id": sent.message_id}
