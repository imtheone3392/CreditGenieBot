# CreditGenie Telegram Mini App

CreditGenie provides member accounts, a Bitcoin deposit workflow, and an admin member directory with individual Telegram messaging. Person lookup, search forms, lookup commands, and the external PeopleFinder link have been removed.

## Members and messaging

Open CreditGenieBot in Telegram, press **Start**, then open the Mini App. Members register when they press Start or open the Mini App; existing accounts retain their balance and original joined date.

Admins listed in the existing `ADMIN_IDS` configuration see **Admin Control Center → Admin Members**. Each entry shows first name, Telegram username (when available), balance, and joined date. Use Previous/Next to view every registered member, and Refresh Members for the latest data.

Select **Message Member**, compose the message, and confirm the named recipient. Messages are sent individually through the existing bot. Telegram requires the recipient to have started or otherwise allowed messages from the bot. A blocked bot, rate limit, or delivery failure is shown in the composer. A timeout can mean the message arrived without a response; verify before resending. List refreshes preserve unsent drafts.

Both member endpoints validate the Telegram Mini App signature and require admin authorization. Sessions older than 24 hours must be reopened from Telegram.

- `GET /api/admin/members?page=1&page_size=50` — member directory, maximum page size 100.
- `POST /api/admin/members/{member_id}/message` — JSON body `{"message":"Hello"}`; up to 4096 UTF-16 units.

The directory uses internal member IDs; clients cannot supply an arbitrary Telegram recipient ID.

## Wallet and persistence

Bitcoin address configuration, deposit submission/history, manual admin deposit approval, and stored balances retain their existing behavior. Verify payments on the Bitcoin network before crediting balances. Messaging never debits or credits members.

The SQLite database uses the existing `DB_PATH`. Its parent directory is created if missing. The specific accidental `VALUE` form-label prefix before `/var/data/creditgenie.db` is normalized in application code without modifying the environment. No fallback database is used when opening the configured database fails.

Existing tables and historical records are preserved. Retired lookup records have no active API or bot access. No balances are automatically changed or refunds issued as part of this update.

## Run and deploy

Install Python 3.12 and dependencies:

```bash
pip install -r requirements.txt
uvicorn app:api --host 0.0.0.0 --port 8000
```

`python bot.py` starts the same service. Run a single instance because the service uses Telegram polling and SQLite.

Use the existing deployment configuration: `BOT_TOKEN`, `MINI_APP_URL`, `ADMIN_IDS`, `DB_PATH`, `BTC_DEPOSIT_ADDRESS`, and `SUPPORT_USERNAME`. Keep credentials out of source control. `/app` serves the Mini App; `/health` checks database access. The Docker command honors Render's `PORT` and does not print environment values.

On Render, the existing persistent disk is mounted at `/var/data` and deployment follows commits to `main`. No environment values or bot credentials need to be replaced for this update.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest -q test_admin_members.py
```

Tests use an isolated temporary database, a dummy bot token, signed local test sessions, and a mocked Telegram transport. They do not send real Telegram messages. If the test host uses a SOCKS proxy, install `httpx[socks]` in the test environment too.
