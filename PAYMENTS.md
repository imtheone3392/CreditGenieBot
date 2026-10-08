# Member payments

Members send existing confirmed USD wallet balance to another registered member. The Send Payment section displays the signed-in member's own Member ID, which they can share to receive money. Enter an exact recipient Member ID, check the recipient name and ID, enter an amount, and confirm the named recipient and total. There is no transfer fee. Completed payments are immediate and cannot be canceled through the app. This is an internal balance transfer, not an on-chain Bitcoin transaction or external withdrawal.

The server validates Telegram authentication, binds the sender to the authenticated account, rejects self-payments, unknown members, noninteger cents, and insufficient funds. A SQLite immediate transaction debits the sender, credits the recipient, and saves the receipt together. Shared wallet spending cannot overdraw through concurrent transfers or purchases. Integer amounts are bounded to JavaScript's safe integer range; recipient balance overflow is rejected. Pending deposits remain unavailable until credited.

A sender-scoped UUID deduplicates retries. The client retains the exact payment draft in member-scoped local storage before submitting. Unknown network outcomes lock the payment details and offer Retry Same Payment, including after reopening. Completed transfers appear in both members' private, paginated histories. No transfer is sent automatically on page load, and no Telegram notifications are sent by this feature.

New endpoints: GET /api/transfers/recipient/{member_id}, POST /api/transfers, GET /api/transfers?page=1. Recipient lookup exposes only first name and Member ID, not balances, Telegram IDs, or messages. The existing /api/me response includes the current member's ID. Startup creates an additive wallet_transfers table and indexes without changing existing balances or orders.

Validation: python -m pytest -q test_wallet_transfers.py test_gift_cards.py test_admin_workflows.py test_admin_members.py; node test_transfer_ui.cjs. Tests use temporary databases, signed dummy sessions, and mocked Telegram; no real member payments are made.


## Member directory and payment requests

Members can browse all registered members in pages of 25, search names, usernames or Member IDs, and select Send or Request. Only member names, saved Telegram usernames, and Member IDs are exposed in the directory. Exact username lookup is case-insensitive and accepts an optional @ prefix. If multiple saved profiles have the same username, lookup refuses to choose one; use the stable Member ID. Accounts update their saved username when they open the app.

Requesting payment does not debit or reserve any money. Only the requested payer can approve and pay or decline; only the requester can cancel a pending request. Request amounts and parties are immutable. Paying atomically moves wallet balance, records the transfer, and marks the request paid. Repeated approval cannot charge twice. Insufficient funds leave the request pending. One pending request per requester/payer pair limits duplicate requests. Each party has a private, paginated request history; the existing payment history also records approved request payments.

Additional endpoints: GET /api/payment-members, GET /api/transfers/resolve?username=..., GET/POST /api/payment-requests, POST /api/payment-requests/{id}/decision. Requests appear in-app and refresh automatically; there are no automatic Telegram messages. Run test_payment_requests.py alongside the existing wallet/accounting suites.
