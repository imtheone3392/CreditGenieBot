import os, json, hmac, hashlib, sqlite3, logging
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
# CreditGenie Telegram bot
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN','').strip()
MINI_APP_URL = os.getenv('MINI_APP_URL','').strip()
DB_PATH = os.getenv('DB_PATH','creditgenie.db').strip()
SEARCH_COST_CENTS = int(os.getenv('SEARCH_COST_CENTS','0'))
SUPPORT_USERNAME = os.getenv('SUPPORT_USERNAME','@YourSupport').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN is missing')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('creditgenie')

def now_iso(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def money(c): return f'${c/100:,.2f}'
def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as con:
        con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_id INTEGER NOT NULL UNIQUE,
          username TEXT, first_name TEXT,
          balance_cents INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS records(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          first_name TEXT COLLATE NOCASE NOT NULL,
          last_name TEXT COLLATE NOCASE NOT NULL,
          state TEXT COLLATE NOCASE, city TEXT COLLATE NOCASE,
          zip TEXT, dob TEXT, reference TEXT, notes TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS search_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          query_summary TEXT NOT NULL,
          result_count INTEGER NOT NULL DEFAULT 0,
          cost_cents INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );''')

def verify_init_data(init_data: str):
    if not init_data:
        raise HTTPException(401,'Open this page from Telegram.')
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop('hash', None)
    if not received: raise HTTPException(401,'Missing Telegram signature.')
    check = '\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs))
    secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise HTTPException(401,'Invalid Telegram signature.')
    try: user = json.loads(pairs.get('user','{}'))
    except Exception: raise HTTPException(401,'Invalid Telegram user data.')
    if not user.get('id'): raise HTTPException(401,'Telegram user not found.')
    return user

def get_or_create_user(tg):
    tid = int(tg['id'])
    with db() as con:
        con.execute('''INSERT INTO users(telegram_id,username,first_name,created_at)
          VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET
          username=excluded.username, first_name=excluded.first_name''',
          (tid,tg.get('username'),tg.get('first_name',''),now_iso()))
        return con.execute('SELECT * FROM users WHERE telegram_id=?',(tid,)).fetchone()

class SearchRequest(BaseModel):
    first_name: str
    last_name: str
    state: str=''
    city: str=''
    zip: str=''
    dob: str=''

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not MINI_APP_URL:
        await update.message.reply_text('CreditGenie is online, but MINI_APP_URL is not configured yet.')
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('🧞 Open CreditGenie Search', web_app=WebAppInfo(url=MINI_APP_URL))]])
    await update.message.reply_text('🧞 CreditGenie\n\nSearch records you own or are authorized to access.', reply_markup=kb)

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text('Admin access required.'); return
    with db() as con:
        u=con.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
        r=con.execute('SELECT COUNT(*) c FROM records').fetchone()['c']
        s=con.execute('SELECT COUNT(*) c FROM search_history').fetchone()['c']
    await update.message.reply_text(f'🛠 CreditGenie Admin\n\nUsers: {u}\nAuthorized records: {r}\nSearches: {s}\n\n/addrecord First|Last|NV|Las Vegas|89103|01/01/1990|REF-001|Notes')

async def addrecord(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text('Admin access required.'); return
    raw = update.message.text.partition(' ')[2].strip(); parts=[p.strip() for p in raw.split('|')]
    if len(parts)!=8:
        await update.message.reply_text('Usage:\n/addrecord First|Last|State|City|ZIP|DOB|Reference|Notes'); return
    first,last,state,city,zipcode,dob,reference,notes=parts
    with db() as con:
        con.execute('''INSERT INTO records(first_name,last_name,state,city,zip,dob,reference,notes,created_at)
          VALUES(?,?,?,?,?,?,?,?,?)''',(first,last,state.upper(),city,zipcode,dob,reference,notes,now_iso()))
    await update.message.reply_text('✅ Authorized record added.')

telegram_bot = Application.builder().token(BOT_TOKEN).build()
telegram_bot.add_handler(CommandHandler('start', start))
telegram_bot.add_handler(CommandHandler('admin', admin))
telegram_bot.add_handler(CommandHandler('addrecord', addrecord))

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await telegram_bot.initialize(); await telegram_bot.start(); await telegram_bot.updater.start_polling(drop_pending_updates=True)
    yield
    await telegram_bot.updater.stop(); await telegram_bot.stop(); await telegram_bot.shutdown()

api = FastAPI(title='CreditGenie', lifespan=lifespan)

@api.get('/')
async def root(): return {'ok':True,'service':'CreditGenie'}
@api.get('/health')
async def health(): return {'ok':True}
@api.get('/app')
async def mini_app(): return FileResponse('index.html', media_type='text/html')

@api.get('/api/me')
async def me(x_telegram_init_data: str=Header(default='')):
    tg=verify_init_data(x_telegram_init_data); user=get_or_create_user(tg)
    return {'first_name':user['first_name'],'balance':money(user['balance_cents']),'search_cost':money(SEARCH_COST_CENTS),'support':SUPPORT_USERNAME}

@api.get('/api/history')
async def history(x_telegram_init_data: str=Header(default='')):
    tg=verify_init_data(x_telegram_init_data); user=get_or_create_user(tg)
    with db() as con:
        rows=con.execute('''SELECT query_summary,result_count,cost_cents,created_at FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT 20''',(user['id'],)).fetchall()
    return [{'query':r['query_summary'],'result_count':r['result_count'],'cost':money(r['cost_cents']),'created_at':r['created_at']} for r in rows]

@api.post('/api/search')
async def search(req: SearchRequest, x_telegram_init_data: str=Header(default='')):
    tg=verify_init_data(x_telegram_init_data); user=get_or_create_user(tg)
    first,last=req.first_name.strip(),req.last_name.strip()
    if not first or not last: raise HTTPException(400,'First and last name are required.')
    vals={'state':req.state.strip().upper(),'city':req.city.strip(),'zip':req.zip.strip(),'dob':req.dob.strip()}
    clauses=['first_name = ?','last_name = ?']; params=[first,last]
    for col,val in vals.items():
        if val: clauses.append(f'{col} = ?'); params.append(val)
    where=' AND '.join(clauses)
    with db() as con:
        current=con.execute('SELECT * FROM users WHERE id=?',(user['id'],)).fetchone()
        if SEARCH_COST_CENTS>0 and current['balance_cents']<SEARCH_COST_CENTS:
            raise HTTPException(402,f'Insufficient balance. Search cost is {money(SEARCH_COST_CENTS)}.')
        rows=con.execute(f'''SELECT id,first_name,last_name,state,city,zip,dob,reference,notes FROM records WHERE {where} ORDER BY id DESC LIMIT 10''',params).fetchall()
        total=con.execute(f'SELECT COUNT(*) c FROM records WHERE {where}',params).fetchone()['c']
        if SEARCH_COST_CENTS>0:
            con.execute('UPDATE users SET balance_cents=balance_cents-? WHERE id=?',(SEARCH_COST_CENTS,user['id']))
        summary=f'{first} {last}'+(f", {vals['state']}" if vals['state'] else '')
        con.execute('''INSERT INTO search_history(user_id,query_summary,result_count,cost_cents,created_at) VALUES(?,?,?,?,?)''',(user['id'],summary,total,SEARCH_COST_CENTS,now_iso()))
    return {'count':total,'results':[dict(r) for r in rows]}
