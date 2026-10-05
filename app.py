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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "creditgenie.db").strip()
SEARCH_COST_CENTS = int(os.getenv("SEARCH_COST_CENTS", "0"))
SUPPORT_USERNAME = os.getenv(
    "SUPPORT_USERNAME",
    "@YourSupport"
).strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")


# -------------------------------------------------
# LOGGING
# -------------------------------------------------

logging.basicConfig(level=logging.INFO)

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
    return f"${cents / 100:,.2f}"


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def is_admin(telegram_id):
    try:
        return int(telegram_id) in ADMIN_IDS
    except (TypeError, ValueError):
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
            401,
            "Open this page from Telegram."
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
            401,
            "Missing Telegram signature."
        )


    data_check_string = "\n".join(
        f"{key}={pairs[key]}"
        for key in sorted(pairs)
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
            401,
            "Invalid Telegram signature."
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
            401,
            "Invalid Telegram user data."
        )


    if not user.get("id"):

        raise HTTPException(
            401,
            "Telegram user not found."
        )


    return user


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

                username = excluded.username,
                first_name = excluded.first_name
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
            WHERE telegram_id = ?
            """,
            (
                telegram_id,
            )
        ).fetchone()


# -------------------------------------------------
# SEARCH REQUEST MODEL
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
                    "🧞 Open CreditGenie Search",
                    web_app=WebAppInfo(
                        url=MINI_APP_URL
                    )
                )
            ]
        ]
    )


    await update.message.reply_text(
        "🧞 CreditGenie\n\n"
        "Submit a search request and "
        "check its status in the Mini App.",
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


    await update.message.reply_text(
        "🛠 CreditGenie Admin\n\n"

        f"Users: {users_count}\n"

        f"Pending requests: "
        f"{pending_count}\n"

        f"Completed requests: "
        f"{completed_count}\n\n"

        "Commands:\n"

        "/queue\n"

        "/request 17\n"

        "/sendresult 17|Your result text\n"

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
                ON u.id = sr.user_id

            WHERE sr.status='pending'

            ORDER BY sr.id ASC

            LIMIT 10
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
                ON u.id = sr.user_id

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
        f"🔎 Request "
        f"#{row['id']}\n\n"

        f"Status: "
        f"{row['status']}\n"

        f"First: "
        f"{row['first_name']}\n"

        f"Last: "
        f"{row['last_name']}\n"

        f"State: "
        f"{row['state'] or '—'}\n"

        f"City: "
        f"{row['city'] or '—'}\n"

        f"ZIP: "
        f"{row['zip'] or '—'}\n"

        f"DOB: "
        f"{row['dob'] or '—'}\n"

        f"Requested by: "
        f"{who}\n\n"

        f"Complete:\n"

        f"/sendresult "
        f"{row['id']}|"
        f"Your result text\n\n"

        f"Reject:\n"

        f"/reject "
        f"{row['id']}|Reason"
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
            "/sendresult "
            "17|Your result text"
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
                ON u.id = sr.user_id

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

                f"{result_text}\n\n"

                "Open the Mini App to view "
                "it in your request history."
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
        if sep
        else
        "Request could not be completed."
    )


    with db() as con:

        row = con.execute(
            """
            SELECT
                sr.id,
                u.telegram_id

            FROM search_requests sr

            JOIN users u
                ON u.id = sr.user_id

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
    .token(
        BOT_TOKEN
    )
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


    first = req.first_name.strip()

    last = req.last_name.strip()

    state = (
        req.state
        .strip()
        .upper()
    )

    city = req.city.strip()

    zipcode = req.zip.strip()

    dob = req.dob.strip()


    if not first or not last:

        raise HTTPException(
            400,
            "First and last name are required."
        )


    if state and (
        len(state) != 2
        or
        not state.isalpha()
    ):

        raise HTTPException(
            400,
            "State must be a "
            "2-letter abbreviation."
        )


    with db() as con:

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
                429,
                "You already have "
                "5 pending requests. "
                "Please wait for an admin response."
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


    return {

        "ok":
            True,

        "request_id":
            request_id,

        "status":
            "pending",

        "message":
            "Search request submitted "
            "to the admin queue."
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
