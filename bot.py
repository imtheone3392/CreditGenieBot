import os
import sqlite3
import logging
import html
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "creditgenie.db").strip()
SEARCH_COST_CENTS = int(os.getenv("SEARCH_COST_CENTS", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@YourSupport").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it in your .env file.")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("creditgenie")

FIRST, LAST, STATE, CITY, ZIPCODE, DOB, CONFIRM = range(7)

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🔎 Search Records"],
        ["💰 Balance", "➕ Add Funds"],
        ["📜 Search History"],
        ["🎫 Support", "👤 My Account"],
    ],
    resize_keyboard=True,
)

CONFIRM_KB = ReplyKeyboardMarkup(
    [["✅ Run Search", "✏️ Start Over"], ["❌ Cancel"]],
    resize_keyboard=True,
)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            first_name TEXT,
            balance_cents INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS records (
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

        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            query_summary TEXT NOT NULL,
            result_count INTEGER NOT NULL DEFAULT 0,
            cost_cents INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """)

def ensure_user(update: Update) -> sqlite3.Row:
    tg = update.effective_user
    with db() as con:
        con.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (tg.id, tg.username, tg.first_name or "", now_iso()),
        )
        return con.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (tg.id,),
        ).fetchone()

def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)

def clean_optional(text: str) -> str:
    text = text.strip()
    return "" if text in {"-", "skip", "Skip", "SKIP"} else text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await update.message.reply_text(
        "🧞 <b>CreditGenie</b>\n\n"
        "Search records you own or are authorized to access.\n"
        "Choose an option below.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )

async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 <b>CreditGenie Privacy</b>\n\n"
        "Use this bot only with records you own or are authorized to access. "
        "Do not store or search SSNs, passwords, full payment-card numbers, "
        "authentication codes, or other highly sensitive credentials.\n\n"
        "Search history stores only a short query summary, result count, cost, "
        "and timestamp.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )

async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    context.user_data["search"] = {}
    await update.message.reply_text(
        "🔎 <b>Search Records</b>\n\nEnter the <b>first name</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return FIRST

async def got_first(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("First name can't be empty.")
        return FIRST
    context.user_data["search"]["first_name"] = value
    await update.message.reply_text("Enter the <b>last name</b>.", parse_mode=ParseMode.HTML)
    return LAST

async def got_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("Last name can't be empty.")
        return LAST
    context.user_data["search"]["last_name"] = value
    await update.message.reply_text(
        "Enter the 2-letter <b>state</b> (example: NV), or send <code>-</code> to skip.",
        parse_mode=ParseMode.HTML,
    )
    return STATE

async def got_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = clean_optional(update.message.text)
    if value and (len(value) != 2 or not value.isalpha()):
        await update.message.reply_text("Use a 2-letter state code like NV, or - to skip.")
        return STATE
    context.user_data["search"]["state"] = value.upper()
    await update.message.reply_text("Enter the <b>city</b>, or send <code>-</code> to skip.", parse_mode=ParseMode.HTML)
    return CITY

async def got_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["search"]["city"] = clean_optional(update.message.text)
    await update.message.reply_text("Enter the <b>ZIP code</b>, or send <code>-</code> to skip.", parse_mode=ParseMode.HTML)
    return ZIPCODE

async def got_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = clean_optional(update.message.text)
    if value and (not value.isdigit() or len(value) not in {5, 9}):
        await update.message.reply_text("Enter a 5- or 9-digit ZIP, or - to skip.")
        return ZIPCODE
    context.user_data["search"]["zip"] = value
    await update.message.reply_text(
        "Enter the <b>DOB</b> in MM/DD/YYYY format, or send <code>-</code> to skip.",
        parse_mode=ParseMode.HTML,
    )
    return DOB

async def got_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["search"]["dob"] = clean_optional(update.message.text)
    q = context.user_data["search"]
    lines = [
        "🧞 <b>Confirm Search</b>",
        "",
        f"First: <b>{html.escape(q['first_name'])}</b>",
        f"Last: <b>{html.escape(q['last_name'])}</b>",
        f"State: <b>{html.escape(q['state'] or 'Any')}</b>",
        f"City: <b>{html.escape(q['city'] or 'Any')}</b>",
        f"ZIP: <b>{html.escape(q['zip'] or 'Any')}</b>",
        f"DOB: <b>{html.escape(q['dob'] or 'Any')}</b>",
        "",
        f"Search cost: <b>{money(SEARCH_COST_CENTS)}</b>",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=CONFIRM_KB,
    )
    return CONFIRM

def search_records(q: dict):
    clauses = ["first_name = ?", "last_name = ?"]
    params = [q["first_name"], q["last_name"]]
    optional = [
        ("state", "state"),
        ("city", "city"),
        ("zip", "zip"),
        ("dob", "dob"),
    ]
    for key, column in optional:
        if q.get(key):
            clauses.append(f"{column} = ?")
            params.append(q[key])

    sql = f"""
        SELECT id, first_name, last_name, state, city, zip, dob, reference, notes
        FROM records
        WHERE {' AND '.join(clauses)}
        ORDER BY id DESC
        LIMIT 10
    """
    with db() as con:
        rows = con.execute(sql, params).fetchall()
        count_sql = f"SELECT COUNT(*) AS c FROM records WHERE {' AND '.join(clauses)}"
        total = con.execute(count_sql, params).fetchone()["c"]
    return rows, total

async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "✏️ Start Over":
        return await search_start(update, context)
    if update.message.text == "❌ Cancel":
        return await cancel(update, context)
    if update.message.text != "✅ Run Search":
        await update.message.reply_text("Choose Run Search, Start Over, or Cancel.")
        return CONFIRM

    user = ensure_user(update)
    if SEARCH_COST_CENTS > 0 and user["balance_cents"] < SEARCH_COST_CENTS:
        await update.message.reply_text(
            f"Insufficient balance. Search cost is {money(SEARCH_COST_CENTS)} "
            f"and your balance is {money(user['balance_cents'])}.",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    q = context.user_data.get("search", {})
    rows, total = search_records(q)

    with db() as con:
        db_user = con.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (update.effective_user.id,),
        ).fetchone()
        if SEARCH_COST_CENTS > 0:
            con.execute(
                "UPDATE users SET balance_cents = balance_cents - ? WHERE id = ?",
                (SEARCH_COST_CENTS, db_user["id"]),
            )

        summary = f"{q['first_name']} {q['last_name']}"
        if q.get("state"):
            summary += f", {q['state']}"
        con.execute(
            """
            INSERT INTO search_history
                (user_id, query_summary, result_count, cost_cents, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (db_user["id"], summary, total, SEARCH_COST_CENTS, now_iso()),
        )

    if not rows:
        await update.message.reply_text(
            "No authorized records matched that search.",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    parts = [f"🔎 <b>{total} match(es)</b>\n"]
    for row in rows:
        parts.append(
            "\n".join([
                f"<b>Record #{row['id']}</b>",
                f"Name: {html.escape(row['first_name'])} {html.escape(row['last_name'])}",
                f"State: {html.escape(row['state'] or '—')}",
                f"City: {html.escape(row['city'] or '—')}",
                f"ZIP: {html.escape(row['zip'] or '—')}",
                f"DOB: {html.escape(row['dob'] or '—')}",
                f"Reference: {html.escape(row['reference'] or '—')}",
                f"Notes: {html.escape(row['notes'] or '—')}",
            ])
        )
        parts.append("")

    if total > 10:
        parts.append("Showing the first 10 matches.")

    await update.message.reply_text(
        "\n".join(parts),
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("search", None)
    await update.message.reply_text("Search cancelled.", reply_markup=MAIN_KB)
    return ConversationHandler.END

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    await update.message.reply_text(
        f"💰 <b>Your Balance</b>\n\n{money(user['balance_cents'])}",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )

async def add_funds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "➕ <b>Add Funds</b>\n\n"
        "Payment processing is not enabled in this starter build.\n"
        f"Contact support: {html.escape(SUPPORT_USERNAME)}",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    with db() as con:
        rows = con.execute(
            """
            SELECT query_summary, result_count, cost_cents, created_at
            FROM search_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (user["id"],),
        ).fetchall()

    if not rows:
        await update.message.reply_text("📜 No search history yet.", reply_markup=MAIN_KB)
        return

    out = ["📜 <b>Recent Search History</b>\n"]
    for r in rows:
        out.append(
            f"• {html.escape(r['query_summary'])} — "
            f"{r['result_count']} result(s) — {money(r['cost_cents'])}\n"
            f"  <code>{html.escape(r['created_at'])}</code>"
        )
    await update.message.reply_text(
        "\n\n".join(out),
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🎫 <b>Support</b>\n\nContact: {html.escape(SUPPORT_USERNAME)}",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    username = f"@{update.effective_user.username}" if update.effective_user.username else "Not set"
    await update.message.reply_text(
        "👤 <b>My Account</b>\n\n"
        f"Telegram ID: <code>{update.effective_user.id}</code>\n"
        f"Username: {html.escape(username)}\n"
        f"Balance: <b>{money(user['balance_cents'])}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin access required.")
        return
    with db() as con:
        users = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        records = con.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"]
        searches = con.execute("SELECT COUNT(*) AS c FROM search_history").fetchone()["c"]
    await update.message.reply_text(
        "🛠 <b>CreditGenie Admin</b>\n\n"
        f"Users: <b>{users}</b>\n"
        f"Authorized records: <b>{records}</b>\n"
        f"Searches: <b>{searches}</b>\n\n"
        "<b>Commands</b>\n"
        "<code>/addrecord First|Last|NV|Las Vegas|89103|01/01/1990|REF-001|Notes</code>\n"
        "<code>/addcredit TELEGRAM_ID DOLLARS</code>",
        parse_mode=ParseMode.HTML,
    )

async def addrecord(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin access required.")
        return
    raw = update.message.text.partition(" ")[2].strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 8:
        await update.message.reply_text(
            "Usage:\n/addrecord First|Last|State|City|ZIP|DOB|Reference|Notes"
        )
        return
    first, last, state, city, zipcode, dob, reference, notes = parts
    if not first or not last:
        await update.message.reply_text("First and last name are required.")
        return
    with db() as con:
        con.execute(
            """
            INSERT INTO records
            (first_name, last_name, state, city, zip, dob, reference, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (first, last, state.upper(), city, zipcode, dob, reference, notes, now_iso()),
        )
    await update.message.reply_text("✅ Authorized record added.")

async def addcredit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin access required.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /addcredit TELEGRAM_ID DOLLARS")
        return
    try:
        telegram_id = int(context.args[0])
        dollars = float(context.args[1])
        cents = round(dollars * 100)
    except ValueError:
        await update.message.reply_text("Invalid Telegram ID or amount.")
        return
    if cents == 0:
        await update.message.reply_text("Amount cannot be zero.")
        return

    with db() as con:
        row = con.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if not row:
            await update.message.reply_text(
                "User not found. They must open the bot and press /start first."
            )
            return
        con.execute(
            "UPDATE users SET balance_cents = balance_cents + ? WHERE telegram_id = ?",
            (cents, telegram_id),
        )
        new_balance = con.execute(
            "SELECT balance_cents FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()["balance_cents"]

    await update.message.reply_text(
        f"✅ Balance updated.\nNew balance: {money(new_balance)}"
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use the menu below or send /start.",
        reply_markup=MAIN_KB,
    )

def build_app():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    search_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^🔎 Search Records$"), search_start),
            CommandHandler("search", search_start),
        ],
        states={
            FIRST: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_first)],
            LAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_last)],
            STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_state)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_city)],
            ZIPCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_zip)],
            DOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_dob)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, run_search)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("privacy", privacy))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("addrecord", addrecord))
    app.add_handler(CommandHandler("addcredit", addcredit))
    app.add_handler(search_conv)

    app.add_handler(MessageHandler(filters.Regex(r"^💰 Balance$"), balance))
    app.add_handler(MessageHandler(filters.Regex(r"^➕ Add Funds$"), add_funds))
    app.add_handler(MessageHandler(filters.Regex(r"^📜 Search History$"), history))
    app.add_handler(MessageHandler(filters.Regex(r"^🎫 Support$"), support))
    app.add_handler(MessageHandler(filters.Regex(r"^👤 My Account$"), account))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
    return app

if __name__ == "__main__":
    build_app().run_polling(drop_pending_updates=True)
