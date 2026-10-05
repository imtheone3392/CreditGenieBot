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
from pydantic import BaseModel

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
logger = logging.getLogger("creditgenie")


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

            CREATE TABLE IF NOT EXISTS records(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT COLLATE NOCASE NOT NULL,
                last_name TEXT COLLATE NOCASE NOT NULL,
                state TEXT COLLATE NOCASE,
                city TEXT COLLATE NOCASE,
                zip TEXT,
                dob TEXT,
                reference TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query_summary TEXT NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0,
                cost_cents INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )


# -------------------------------------------------
# TELEGRAM MINI APP AUTH
# -------------------------------------------------

def verify_init_data(init_data: str):

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
# USERS
# -------------------------------------------------

def get_or_create_user(tg):

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
            VALUES(?,?,?,?)

            ON CONFLICT(telegram_id)
            DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (
                telegram_id,
                tg.get("username"),
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
# SEARCH REQUEST MODEL
# -------------------------------------------------

class SearchRequest(BaseModel):

    first_name: str
    last_name: str
    state: str = ""
    city: str = ""
    zip: str = ""
    dob: str = ""


# -------------------------------------------------
# TELEGRAM COMMAND: /start
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
        "Search records you own or are "
        "authorized to access.",
        reply_markup=keyboard
    )


# -------------------------------------------------
# TELEGRAM COMMAND: /admin
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

        records_count = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM records
            """
        ).fetchone()["c"]

        searches_count = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM search_history
            """
        ).fetchone()["c"]

    await update.message.reply_text(
        "🛠 CreditGenie Admin\n\n"
        f"Users: {users_count}\n"
        f"Authorized records: {records_count}\n"
        f"Searches: {searches_count}\n\n"
        "/addrecord "
        "First|Last|NV|Las Vegas|89103|"
        "01/01/1990|REF-001|Notes"
    )


# -------------------------------------------------
# TELEGRAM COMMAND: /addrecord
# -------------------------------------------------

async def addrecord(
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

    parts = [
        p.strip()
        for p in raw.split("|")
    ]

    if len(parts) != 8:

        await update.message.reply_text(
            "Usage:\n"
            "/addrecord "
            "First|Last|State|City|ZIP|"
            "DOB|Reference|Notes"
        )

        return

    (
        first,
        last,
        state,
        city,
        zipcode,
        dob,
        reference,
        notes
    ) = parts

    with db() as con:

        con.execute(
            """
            INSERT INTO records(
                first_name,
                last_name,
                state,
                city,
                zip,
                dob,
                reference,
                notes,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                first,
                last,
                state.upper(),
                city,
                zipcode,
                dob,
                reference,
                notes,
                now_iso()
            )
        )

    await update.message.reply_text(
        "✅ Authorized record added."
    )


# -------------------------------------------------
# TELEGRAM APPLICATION
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
        "addrecord",
        addrecord
    )
)


# -------------------------------------------------
# FASTAPI STARTUP / SHUTDOWN
# -------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):

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
# FASTAPI APP
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
# HEALTH CHECK
# -------------------------------------------------

@api.get("/health")
async def health():

    return {
        "ok": True
    }


# -------------------------------------------------
# MINI APP PAGE
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
# SEARCH HISTORY
# -------------------------------------------------

@api.get("/api/history")
async def history(
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
                query_summary,
                result_count,
                cost_cents,
                created_at

            FROM search_history

            WHERE user_id=?

            ORDER BY id DESC

            LIMIT 20
            """,
            (
                user["id"],
            )
        ).fetchall()

    return [
        {
            "query":
                row["query_summary"],

            "result_count":
                row["result_count"],

            "cost":
                money(
                    row["cost_cents"]
                ),

            "created_at":
                row["created_at"]
        }

        for row in rows
    ]


# -------------------------------------------------
# SEARCH
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

    if not first or not last:

        raise HTTPException(
            400,
            "First and last name are required."
        )

    vals = {

        "state":
            req.state
            .strip()
            .upper(),

        "city":
            req.city
            .strip(),

        "zip":
            req.zip
            .strip(),

        "dob":
            req.dob
            .strip()
    }

    clauses = [
        "first_name = ?",
        "last_name = ?"
    ]

    params = [
        first,
        last
    ]

    for column, value in vals.items():

        if value:

            clauses.append(
                f"{column} = ?"
            )

            params.append(
                value
            )

    where = " AND ".join(
        clauses
    )

    with db() as con:

        current = con.execute(
            """
            SELECT *
            FROM users
            WHERE id=?
            """,
            (
                user["id"],
            )
        ).fetchone()

        if (
            SEARCH_COST_CENTS > 0
            and
            current["balance_cents"]
            < SEARCH_COST_CENTS
        ):

            raise HTTPException(
                402,
                "Insufficient balance. "
                f"Search cost is "
                f"{money(SEARCH_COST_CENTS)}."
            )

        rows = con.execute(
            f"""
            SELECT
                id,
                first_name,
                last_name,
                state,
                city,
                zip,
                dob,
                reference,
                notes

            FROM records

            WHERE {where}

            ORDER BY id DESC

            LIMIT 10
            """,
            params
        ).fetchall()

        total = con.execute(
            f"""
            SELECT COUNT(*) AS c

            FROM records

            WHERE {where}
            """,
            params
        ).fetchone()["c"]

        if SEARCH_COST_CENTS > 0:

            con.execute(
                """
                UPDATE users

                SET balance_cents =
                    balance_cents - ?

                WHERE id=?
                """,
                (
                    SEARCH_COST_CENTS,
                    user["id"]
                )
            )

        summary = (
            f"{first} {last}"
        )

        if vals["state"]:
            summary += (
                f", {vals['state']}"
            )

        con.execute(
            """
            INSERT INTO search_history(
                user_id,
                query_summary,
                result_count,
                cost_cents,
                created_at
            )
            VALUES(?,?,?,?,?)
            """,
            (
                user["id"],
                summary,
                total,
                SEARCH_COST_CENTS,
                now_iso()
            )
        )

    return {

        "count":
            total,

        "results":
            [
                dict(row)
                for row in rows
            ]
    }
