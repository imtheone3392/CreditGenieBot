import os
import json
import hmac
import hashlib
import sqlite3
import logging

from datetime import datetime, timezone
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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

SEARCH_COST_CENTS = 1200

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

    if not user.get("id"):

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

class SearchRequest(
    BaseModel
):

    first_name: str = Field(
        min_length=1,
        max_length=80
    )

    last_name: str = Field(
        min_length=1,
        max_length=80
    )

    state: str = Field(
        default="",
        max_length=2
    )

    city: str = Field(
        default="",
        max_length=100
    )

    zip: str = Field(
        default="",
        max_length=20
    )

    dob: str = Field(
        default="",
        max_length=20
    )


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


class AdminResultRequest(
    BaseModel
):

    request_id: int = Field(
        gt=0
    )

    result_text: str = Field(
        min_length=1,
        max_length=12000
    )

    admin_note: str = Field(
        default="",
        max_length=2000
    )


class AdminRejectRequest(
    BaseModel
):

    request_id: int = Field(
        gt=0
    )

    reason: str = Field(
        default=(
            "Request could not "
            "be completed."
        ),
        max_length=2000
    )


# -------------------------------------------------
# TELEGRAM /start
# -------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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
        "your balance and search requests.",
        reply_markup=keyboard
    )


# -------------------------------------------------
# TELEGRAM /admin
# -------------------------------------------------

async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "Admin access required."
        )

        return

    with db() as con:

        users_count = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM users
            """
        ).fetchone()["c"]

        pending_count = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM search_requests
            WHERE status='pending'
            """
        ).fetchone()["c"]

        completed_count = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM search_requests
            WHERE status='completed'
            """
        ).fetchone()["c"]

        pending_deposits = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM bitcoin_deposits
            WHERE status='pending'
            """
        ).fetchone()["c"]

    await update.message.reply_text(
        "🛠 CreditGenie Admin\n\n"

        f"Users: {users_count}\n"
        f"Pending searches: {pending_count}\n"
        f"Completed searches: {completed_count}\n"
        f"Pending deposits: {pending_deposits}\n\n"

        "Commands:\n"
        "/queue\n"
        "/request 17\n"
        "/sendresult 17|Result\n"
        "/reject 17|Reason"
    )


# -------------------------------------------------
# TELEGRAM /queue
# -------------------------------------------------

async def queue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "Admin access required."
        )

        return

    with db() as con:

        rows = con.execute(
            """
            SELECT
                sr.id,
                sr.first_name,
                sr.last_name,
                sr.state,
                sr.city,
                sr.zip,
                sr.dob,
                sr.created_at,

                u.first_name
                    AS user_first_name,

                u.username

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.status='pending'

            ORDER BY sr.id ASC

            LIMIT 20
            """
        ).fetchall()

    if not rows:

        await update.message.reply_text(
            "✅ No pending search requests."
        )

        return

    parts = [
        "📥 Pending Search Queue"
    ]

    for row in rows:

        who = (
            row["user_first_name"]
            or
            "User"
        )

        if row["username"]:

            who += (
                f" (@{row['username']})"
            )

        location = ", ".join(
            x
            for x in [
                row["city"],
                row["state"],
                row["zip"]
            ]
            if x
        ) or "No location"

        parts.append(
            f"\n"
            f"#{row['id']} — "
            f"{row['first_name']} "
            f"{row['last_name']}\n"

            f"{location}\n"

            f"DOB: "
            f"{row['dob'] or '—'}\n"

            f"Requested by: "
            f"{who}\n"

            f"/request "
            f"{row['id']}"
        )

    await update.message.reply_text(
        "\n".join(parts)
    )


# -------------------------------------------------
# TELEGRAM /request
# -------------------------------------------------

async def request_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "Admin access required."
        )

        return

    if (
        not context.args
        or
        not context.args[0].isdigit()
    ):

        await update.message.reply_text(
            "Usage: /request 17"
        )

        return

    request_id = int(
        context.args[0]
    )

    with db() as con:

        row = con.execute(
            """
            SELECT
                sr.*,

                u.telegram_id,

                u.first_name
                    AS user_first_name,

                u.username

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.id=?
            """,
            (
                request_id,
            )
        ).fetchone()

    if not row:

        await update.message.reply_text(
            "Request not found."
        )

        return

    who = (
        row["user_first_name"]
        or
        "User"
    )

    if row["username"]:

        who += (
            f" (@{row['username']})"
        )

    await update.message.reply_text(
        f"🔎 Request #{row['id']}\n\n"

        f"Status: {row['status']}\n"

        f"First: {row['first_name']}\n"
        f"Last: {row['last_name']}\n"

        f"State: {row['state'] or '—'}\n"
        f"City: {row['city'] or '—'}\n"
        f"ZIP: {row['zip'] or '—'}\n"
        f"DOB: {row['dob'] or '—'}\n"

        f"Requested by: {who}\n\n"

        f"Complete:\n"
        f"/sendresult {row['id']}|Result\n\n"

        f"Reject:\n"
        f"/reject {row['id']}|Reason"
    )


# -------------------------------------------------
# TELEGRAM /sendresult
# -------------------------------------------------

async def sendresult(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "Admin access required."
        )

        return

    raw = (
        update.message.text
        .partition(" ")[2]
        .strip()
    )

    (
        request_id_text,
        sep,
        result_text
    ) = raw.partition("|")

    if (
        not sep
        or
        not request_id_text.strip().isdigit()
        or
        not result_text.strip()
    ):

        await update.message.reply_text(
            "Usage:\n"
            "/sendresult 17|Result"
        )

        return

    request_id = int(
        request_id_text.strip()
    )

    result_text = (
        result_text.strip()
    )

    with db() as con:

        row = con.execute(
            """
            SELECT
                sr.id,
                sr.status,
                u.telegram_id

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.id=?
            """,
            (
                request_id,
            )
        ).fetchone()

        if not row:

            await update.message.reply_text(
                "Request not found."
            )

            return

        if row["status"] != "pending":

            await update.message.reply_text(
                f"Request #{request_id} "
                f"is already {row['status']}."
            )

            return

        con.execute(
            """
            UPDATE search_requests

            SET
                status='completed',
                result_text=?,
                admin_note=NULL,
                updated_at=?

            WHERE id=?
            """,
            (
                result_text,
                now_iso(),
                request_id
            )
        )

    try:

        await context.bot.send_message(
            chat_id=row["telegram_id"],

            text=(
                f"✅ CreditGenie request "
                f"#{request_id} is complete.\n\n"

                "Open the Mini App "
                "to view your result."
            )
        )

    except Exception as exc:

        logger.warning(
            "Could not notify user "
            "for request %s: %s",
            request_id,
            exc
        )

    await update.message.reply_text(
        f"✅ Request #{request_id} "
        "completed and saved."
    )


# -------------------------------------------------
# TELEGRAM /reject
# -------------------------------------------------

async def reject(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "Admin access required."
        )

        return

    raw = (
        update.message.text
        .partition(" ")[2]
        .strip()
    )

    (
        request_id_text,
        sep,
        reason
    ) = raw.partition("|")

    if not request_id_text.strip().isdigit():

        await update.message.reply_text(
            "Usage:\n"
            "/reject 17|Reason"
        )

        return

    request_id = int(
        request_id_text.strip()
    )

    reason = (
        reason.strip()
        if sep and reason.strip()
        else
        "Request could not be completed."
    )

    with db() as con:

        row = con.execute(
            """
            SELECT
                sr.id,
                sr.status,
                u.telegram_id

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.id=?
            """,
            (
                request_id,
            )
        ).fetchone()

        if not row:

            await update.message.reply_text(
                "Request not found."
            )

            return

        if row["status"] != "pending":

            await update.message.reply_text(
                f"Request #{request_id} "
                f"is already {row['status']}."
            )

            return

        con.execute(
            """
            UPDATE search_requests

            SET
                status='rejected',
                result_text=NULL,
                admin_note=?,
                updated_at=?

            WHERE id=?
            """,
            (
                reason,
                now_iso(),
                request_id
            )
        )

    try:

        await context.bot.send_message(
            chat_id=row["telegram_id"],

            text=(
                f"❌ CreditGenie request "
                f"#{request_id} was not completed.\n\n"

                f"{reason}"
            )
        )

    except Exception as exc:

        logger.warning(
            "Could not notify user "
            "for request %s: %s",
            request_id,
            exc
        )

    await update.message.reply_text(
        f"Request #{request_id} "
        "marked rejected."
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

telegram_bot.add_handler(
    CommandHandler(
        "queue",
        queue
    )
)

telegram_bot.add_handler(
    CommandHandler(
        "request",
        request_detail
    )
)

telegram_bot.add_handler(
    CommandHandler(
        "sendresult",
        sendresult
    )
)

telegram_bot.add_handler(
    CommandHandler(
        "reject",
        reject
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

    return {
        "ok": True
    }


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

        "search_cost":
            money(
                SEARCH_COST_CENTS
            ),

        "support":
            SUPPORT_USERNAME,

        "is_admin":
            is_admin(
                tg["id"]
            )
    }


# -------------------------------------------------
# CREATE SEARCH REQUEST
# -------------------------------------------------

@api.post("/api/search")
async def search(
    req: SearchRequest,

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

    first = (
        req.first_name
        .strip()
    )

    last = (
        req.last_name
        .strip()
    )

    state = (
        req.state
        .strip()
        .upper()
    )

    city = (
        req.city
        .strip()
    )

    zipcode = (
        req.zip
        .strip()
    )

    dob = (
        req.dob
        .strip()
    )

    if not first or not last:

        raise HTTPException(
            status_code=400,
            detail=(
                "First and last name "
                "are required."
            )
        )

    if state and (
        len(state) != 2
        or
        not state.isalpha()
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "State must be a "
                "2-letter abbreviation."
            )
        )

    with db() as con:

        current_user = con.execute(
            """
            SELECT
                id,
                balance_cents

            FROM users

            WHERE id=?
            """,
            (
                user["id"],
            )
        ).fetchone()

        if not current_user:

            raise HTTPException(
                status_code=404,
                detail="User not found."
            )

        if (
            current_user["balance_cents"]
            <
            SEARCH_COST_CENTS
        ):

            raise HTTPException(
                status_code=402,
                detail=(
                    "Insufficient Bitcoin balance. "
                    f"This search costs "
                    f"{money(SEARCH_COST_CENTS)}."
                )
            )

        pending_count = con.execute(
            """
            SELECT COUNT(*) AS c

            FROM search_requests

            WHERE
                user_id=?
                AND status='pending'
            """,
            (
                user["id"],
            )
        ).fetchone()["c"]

        if pending_count >= 5:

            raise HTTPException(
                status_code=429,
                detail=(
                    "You already have "
                    "5 pending requests."
                )
            )

        charged = con.execute(
            """
            UPDATE users

            SET balance_cents =
                balance_cents - ?

            WHERE
                id=?
                AND balance_cents >= ?
            """,
            (
                SEARCH_COST_CENTS,
                user["id"],
                SEARCH_COST_CENTS
            )
        )

        if charged.rowcount != 1:

            raise HTTPException(
                status_code=402,
                detail=(
                    "Insufficient Bitcoin balance."
                )
            )

        cur = con.execute(
            """
            INSERT INTO search_requests(
                user_id,
                first_name,
                last_name,
                state,
                city,
                zip,
                dob,
                status,
                created_at,
                updated_at
            )

            VALUES(
                ?, ?, ?, ?, ?, ?, ?,
                'pending',
                ?, ?
            )
            """,
            (
                user["id"],
                first,
                last,
                state,
                city,
                zipcode,
                dob,
                now_iso(),
                now_iso()
            )
        )

        request_id = (
            cur.lastrowid
        )

        updated_user = con.execute(
            """
            SELECT balance_cents
            FROM users
            WHERE id=?
            """,
            (
                user["id"],
            )
        ).fetchone()

    return {
        "ok": True,

        "request_id":
            request_id,

        "status":
            "pending",

        "charged":
            money(
                SEARCH_COST_CENTS
            ),

        "balance":
            money(
                updated_user[
                    "balance_cents"
                ]
            ),

        "message":
            (
                f"{money(SEARCH_COST_CENTS)} "
                "was charged and your search "
                "was submitted to the admin queue."
            )
    }


# -------------------------------------------------
# USER SEARCH REQUESTS
# -------------------------------------------------

@api.get("/api/requests")
async def my_requests(
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
                first_name,
                last_name,
                state,
                city,
                zip,
                dob,
                status,
                result_text,
                admin_note,
                created_at,
                updated_at

            FROM search_requests

            WHERE user_id=?

            ORDER BY id DESC

            LIMIT 20
            """,
            (
                user["id"],
            )
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


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

        "search_cost":
            money(
                SEARCH_COST_CENTS
            )
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
async def admin_stats(
    x_telegram_init_data: str = Header(
        default=""
    )
):

    require_admin(
        x_telegram_init_data
    )

    with db() as con:

        users = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM users
            """
        ).fetchone()["c"]

        pending = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM search_requests
            WHERE status='pending'
            """
        ).fetchone()["c"]

        completed = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM search_requests
            WHERE status='completed'
            """
        ).fetchone()["c"]

        rejected = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM search_requests
            WHERE status='rejected'
            """
        ).fetchone()["c"]

        pending_deposits = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM bitcoin_deposits
            WHERE status='pending'
            """
        ).fetchone()["c"]

    return {
        "ok": True,
        "users": users,
        "pending": pending,
        "completed": completed,
        "rejected": rejected,
        "pending_deposits":
            pending_deposits
    }


# -------------------------------------------------
# ADMIN SEARCH QUEUE
# -------------------------------------------------

@api.get("/api/admin/queue")
async def admin_queue(
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
                sr.id,
                sr.first_name,
                sr.last_name,
                sr.state,
                sr.city,
                sr.zip,
                sr.dob,
                sr.status,
                sr.created_at,
                sr.updated_at,

                u.first_name
                    AS requested_by,

                u.username
                    AS requested_by_username

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.status='pending'

            ORDER BY
                sr.created_at ASC,
                sr.id ASC

            LIMIT 100
            """
        ).fetchall()

    return {
        "ok": True,

        "count":
            len(rows),

        "requests": [
            dict(row)
            for row in rows
        ]
    }


# -------------------------------------------------
# ADMIN REQUEST DETAIL
# -------------------------------------------------

@api.get(
    "/api/admin/request/{request_id}"
)
async def admin_request_detail(
    request_id: int,

    x_telegram_init_data: str = Header(
        default=""
    )
):

    require_admin(
        x_telegram_init_data
    )

    with db() as con:

        row = con.execute(
            """
            SELECT
                sr.id,
                sr.first_name,
                sr.last_name,
                sr.state,
                sr.city,
                sr.zip,
                sr.dob,
                sr.status,
                sr.result_text,
                sr.admin_note,
                sr.created_at,
                sr.updated_at,

                u.first_name
                    AS requested_by,

                u.username
                    AS requested_by_username

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.id=?
            """,
            (
                request_id,
            )
        ).fetchone()

    if not row:

        raise HTTPException(
            status_code=404,
            detail="Request not found."
        )

    return {
        "ok": True,
        "request": dict(row)
    }


# -------------------------------------------------
# ADMIN SEND RESULT
# -------------------------------------------------

@api.post("/api/admin/send-result")
async def admin_send_result(
    req: AdminResultRequest,

    x_telegram_init_data: str = Header(
        default=""
    )
):

    admin_tg = require_admin(
        x_telegram_init_data
    )

    result_text = (
        req.result_text
        .strip()
    )

    admin_note = (
        req.admin_note
        .strip()
    )

    if not result_text:

        raise HTTPException(
            status_code=400,
            detail=(
                "Result text is required."
            )
        )

    with db() as con:

        row = con.execute(
            """
            SELECT
                sr.id,
                sr.status,
                sr.first_name,
                sr.last_name,

                u.telegram_id

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.id=?
            """,
            (
                req.request_id,
            )
        ).fetchone()

        if not row:

            raise HTTPException(
                status_code=404,
                detail=(
                    "Request not found."
                )
            )

        if row["status"] != "pending":

            raise HTTPException(
                status_code=409,
                detail=(
                    f"Request is already "
                    f"{row['status']}."
                )
            )

        con.execute(
            """
            UPDATE search_requests

            SET
                status='completed',
                result_text=?,
                admin_note=?,
                updated_at=?

            WHERE
                id=?
                AND status='pending'
            """,
            (
                result_text,
                admin_note or None,
                now_iso(),
                req.request_id
            )
        )

    try:

        await telegram_bot.bot.send_message(
            chat_id=row["telegram_id"],

            text=(
                "✅ CreditGenie Search Complete\n\n"

                f"Request #{req.request_id}\n"
                f"{row['first_name']} "
                f"{row['last_name']}\n\n"

                "Open CreditGenie "
                "to view the result."
            )
        )

    except Exception as exc:

        logger.warning(
            "Could not notify Telegram user "
            "for request %s: %s",
            req.request_id,
            exc
        )

    logger.info(
        "Request %s completed by admin %s",
        req.request_id,
        admin_tg.get("id")
    )

    return {
        "ok": True,
        "request_id":
            req.request_id,
        "status":
            "completed",
        "message":
            "Result sent successfully."
    }


# -------------------------------------------------
# ADMIN REJECT SEARCH
# -------------------------------------------------

@api.post("/api/admin/reject")
async def admin_reject_request(
    req: AdminRejectRequest,

    x_telegram_init_data: str = Header(
        default=""
    )
):

    admin_tg = require_admin(
        x_telegram_init_data
    )

    reason = (
        req.reason.strip()
        or
        "Request could not be completed."
    )

    with db() as con:

        row = con.execute(
            """
            SELECT
                sr.id,
                sr.status,
                sr.first_name,
                sr.last_name,

                u.telegram_id

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            WHERE sr.id=?
            """,
            (
                req.request_id,
            )
        ).fetchone()

        if not row:

            raise HTTPException(
                status_code=404,
                detail="Request not found."
            )

        if row["status"] != "pending":

            raise HTTPException(
                status_code=409,
                detail=(
                    f"Request is already "
                    f"{row['status']}."
                )
            )

        con.execute(
            """
            UPDATE search_requests

            SET
                status='rejected',
                result_text=NULL,
                admin_note=?,
                updated_at=?

            WHERE
                id=?
                AND status='pending'
            """,
            (
                reason,
                now_iso(),
                req.request_id
            )
        )

    try:

        await telegram_bot.bot.send_message(
            chat_id=row["telegram_id"],

            text=(
                "❌ CreditGenie Request Update\n\n"

                f"Request #{req.request_id}\n"
                f"{row['first_name']} "
                f"{row['last_name']}\n\n"

                f"{reason}"
            )
        )

    except Exception as exc:

        logger.warning(
            "Could not notify Telegram user "
            "for request %s: %s",
            req.request_id,
            exc
        )

    logger.info(
        "Request %s rejected by admin %s",
        req.request_id,
        admin_tg.get("id")
    )

    return {
        "ok": True,
        "request_id":
            req.request_id,
        "status":
            "rejected",
        "message":
            "Request rejected."
    }


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
            "Could not notify user "
            "for deposit %s: %s",
            req.deposit_id,
            exc
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


# -------------------------------------------------
# ADMIN RECENT SEARCH REQUESTS
# -------------------------------------------------

@api.get("/api/admin/recent")
async def admin_recent_requests(
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
                sr.id,
                sr.first_name,
                sr.last_name,
                sr.state,
                sr.city,
                sr.zip,
                sr.dob,
                sr.status,
                sr.created_at,
                sr.updated_at,

                u.first_name
                    AS requested_by,

                u.username
                    AS requested_by_username

            FROM search_requests sr

            JOIN users u
                ON u.id=sr.user_id

            ORDER BY sr.id DESC

            LIMIT 50
            """
        ).fetchall()

    return {
        "ok": True,

        "requests": [
            dict(row)
            for row in rows
        ]
    }
