# Member payments

Members send existing confirmed USD wallet balance to another registered member. The Send Payment section displays the signed-in member's own Member ID, which they can share to receive money. Enter an exact recipient Member ID, check the recipient name and ID, enter an amount, and confirm the named recipient and total. There is no transfer fee. Completed payments are immediate and cannot be canceled through the app. This is an internal balance transfer, not an on-chain Bitcoin transaction or external withdrawal.

The server validates Telegram authentication, binds the sender to the authenticated account, rejects self-payments, unknown members, noninteger cents, and insufficient funds. A SQLite immediate transaction debits the sender, credits the recipient, and saves the receipt together. Shared wallet spending cannot overdraw through concurrent transfers or purchases. Integer amounts are bounded to JavaScript's safe integer range; recipient balance overflow is rejected. Pending deposits remain unavailable until credited.

A sender-scoped UUID deduplicates retries. The client retains the exact payment draft in member-scoped local storage before submitting. Unknown network outcomes lock the payment details and offer Retry Same Payment, including after reopening. Completed transfers appear in both members' private, paginated histories. No transfer is sent automatically on page load, and no Telegram notifications are sent by this feature.

New endpoints: GET /api/transfers/recipient/{member_id}, POST /api/transfers, GET /api/transfers?page=1. Recipient lookup exposes only first name and Member ID, not balances, Telegram IDs, or messages. The existing /api/me response includes the current member's ID. Startup creates an additive wallet_transfers table and indexes without changing existing balances or orders.

Validation: python -m pytest -q test_wallet_transfers.py test_gift_cards.py test_admin_workflows.py test_admin_members.py; node test_transfer_ui.cjs. Tests use temporary databases, signed dummy sessions, and mocked Telegram; no real member payments are made.
