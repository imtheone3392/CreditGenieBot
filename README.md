# CreditGenie Telegram Bot

CreditGenie is a starter Telegram bot for searching records that you own or are authorized to access.

## Included

- Home menu
- Guided record search
- Balance
- Search pricing
- Search history
- Account page
- Support link
- Basic admin stats
- Admin command to add authorized records
- Admin command to add user credit
- SQLite database
- Dockerfile

## Safety / data rules

Do not use this project to store, search, sell, or expose SSNs, passwords, authentication codes,
full payment-card data, stolen identity information, or similar highly sensitive credentials.

Use it only with records you own or are authorized to access.

## 1. Install Python

Use Python 3.11+.

## 2. Create a virtual environment

### Windows

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure the bot

Copy `.env.example` to `.env`.

```bash
cp .env.example .env
```

Edit `.env` and paste the NEW token that BotFather gave you:

```env
BOT_TOKEN=your_new_token_here
ADMIN_IDS=your_telegram_numeric_id
SEARCH_COST_CENTS=0
SUPPORT_USERNAME=@YourSupport
DB_PATH=creditgenie.db
```

Never post your bot token in chat, screenshots, GitHub, or public messages.

### Find your Telegram numeric ID

Open your bot, press Start, then use Telegram's official/user-ID helper of your choice, or temporarily
look at the `update.effective_user.id` value in logs if you are developing locally.

## 4. Run CreditGenie

```bash
python bot.py
```

Then open your bot in Telegram and send:

```text
/start
```

## 5. Admin commands

First, put your Telegram numeric ID in `ADMIN_IDS` and restart the bot.

Admin dashboard:

```text
/admin
```

Add an authorized record:

```text
/addrecord Jane|Doe|NV|Las Vegas|89103|01/01/1990|REF-001|Customer file
```

Add user credit:

```text
/addcredit 123456789 25
```

That example adds $25.00 to Telegram user `123456789`.

## 6. Search pricing

Set:

```env
SEARCH_COST_CENTS=250
```

to charge $2.50 from a user's internal bot balance for each completed search.

Leave it at `0` while testing.

## 7. Put it online

You can run this continuously on any VPS or container host that supports Python/Docker.

With Docker:

```bash
docker build -t creditgenie .
docker run --env-file .env -v "$(pwd)/data:/app/data" creditgenie
```

For persistent Docker storage, you can also set:

```env
DB_PATH=/app/data/creditgenie.db
```

## Useful commands

- `/start` — home menu
- `/search` — start a search
- `/cancel` — cancel a search
- `/privacy` — privacy/data-use notice
- `/admin` — admin stats
- `/addrecord ...` — add an authorized record
- `/addcredit ...` — change a user's internal balance

## Next upgrades

Good next additions are:

- PostgreSQL instead of SQLite
- Secure web-based admin dashboard
- CSV import for authorized records
- Telegram Stars or a legitimate payment processor
- Support-ticket workflow
- Role-based admin permissions
- Encrypted database backups
- Audit logs
