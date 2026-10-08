# Gift card purchases

Members select a brand, a value per card from $400.00 through $1,000.00, and an integer quantity from 1 through 50. Each order contains the same brand and value per card. Quantities 1–9 receive 40% off (pay 60%); quantities 10–50 receive 65% off (pay 35%) the entire order. The server calculates and stores the actual wallet charge, rounded to the nearest cent (half up). Checkout shows quantity, total card value, discount, and wallet payment. For example, 2 × $400 cards have an $800 face value and cost $480; 50 × $1,000 cards cost $17,500. Only confirmed wallet balance can be spent; pending deposits are not funds. Each purchase is charged immediately and enters the admin queue.

In **Admin Control Center → Gift Cards**, enter the actual redemption details and region, then select **Approve & Send Details**. The buyer can view the saved details in **My Gift Cards**. Telegram sends an order-status notification without exposing the redemption codes. Rejection requires a reason and refunds only the actual wallet charge, not the undiscounted card value. Reviewed orders cannot be reversed through this workflow.

The catalog contains 122 normalized names found in historical Paxful payment-method lists. The source URLs and qualifications are recorded in `gift_cards.json`. Paxful's marketplace is discontinued, so this is not a complete verified current catalog or inventory feed. A historically listed brand may no longer issue cards. There is no Paxful integration and no automatic card procurement or validity check. Admins must fulfill with valid cards; unavailable orders must be rejected and refunded. Admins must supply all cards in the requested quantity. The ambiguous historical entry “Telecom” was not included.

Purchase retries use a UUID scoped to the authenticated member. SQLite immediate transactions make the wallet debit and order insertion atomic, prevent overspending, and ensure each rejection refunds only once. Codes are returned only to their authenticated owner or an admin. Gift-card endpoints use `Cache-Control: no-store`. Existing wallet, deposits, member messaging and profile features retain their behavior.

No new environment variables or dependencies are required. Include `gift_cards.json` with `app.py` in deployment. Startup adds quantity, discount percentage and charged amount to existing orders in an atomic migration. Legacy orders remain quantity 1 with no discount and retain their original charge and refund amounts. Repeated startup does not reprice orders.

Run isolated tests (mocked Telegram, temporary databases):

```sh
python -m pytest -q test_gift_cards.py test_admin_workflows.py test_admin_members.py
```

New orders require confirmation of the server-calculated charge. Stale checkout totals are rejected before any debit. Existing orders retain their original discounts and refund amounts.
