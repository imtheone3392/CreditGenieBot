# Manual Bitcoin withdrawals

Admins use **Withdrawals & Balances** to see all members in pages of 25, including available wallet balance and funds reserved for withdrawals. They select a member and enter the USD deduction, exact BTC amount the member will receive, and an optional request note. There is no withdrawal fee. The admin is responsible for determining the BTC quote and paying network fees externally.

Creation sends a Telegram notification and saves a private request in the member's **Withdrawals** section. It does not change their balance. The member can decline or accept with their own Bitcoin mainnet receiving address. Acceptance confirms both amounts and atomically reserves the USD amount by deducting it from spendable balance. The address is checksum-validated and appears in the admin queue. The quote and address cannot subsequently be edited.

An admin may also approve a requested withdrawal without member acceptance by entering the member’s mainnet BTC receiving address and confirming immediate reservation of the USD balance. Direct approval atomically checks and reserves the available funds, records a distinct audit event, and notifies the member of the address and note. A member-submitted address on an already accepted withdrawal cannot be changed.

The admin must add a note to approve. The approval note is saved to member history and included in a Telegram notification. Approval does not send Bitcoin or deduct again. The admin sends Bitcoin manually using their own wallet, then confirms it was sent and records the 64-character transaction ID and a final note. The application records the payment; it does not verify the transaction on-chain or control any Bitcoin wallet.

An admin can cancel a request while it remains requested. After acceptance or direct approval, rejection requires confirmation that no Bitcoin has been sent and releases the reserved balance exactly once. Paid withdrawals cannot be rejected or refunded through this workflow. Requests, approvals, rejections, and manual payment receipts are immutable on retry. A mismatched retry is rejected. Only one active withdrawal is allowed per member. Failed Telegram delivery leaves the request or decision visible in the app; it does not roll back the financial state.

The existing deposit address, credentials, gift card pricing, $5 member transfer fee, and prior records are unchanged. Tests use a temporary database and mocked Telegram delivery. No production funds or notifications are used for verification.

Run `python -m pytest -q test_withdrawals.py` and `node test_withdrawal_ui.cjs` for the withdrawal checks. Address checksum rules follow Base58Check, BIP173 and BIP350. A valid checksum does not prove ownership; members and admins must review the receiving address before payment.
