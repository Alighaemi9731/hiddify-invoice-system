# Targeted owner broadcasts

The web **پیام همگانی** page and the owner Telegram `/broadcast` flow share the same
recipient resolver. The base is present, billable, top-level resellers on enabled panels.
Owners, sub-resellers, exempt accounts and removed admins are excluded.

Choose an audience (debtors, overdue, deferred, suspended, frozen, pending payment,
no debt, or sales/activity), then combine these restrictions:

- One or several panels; no selected panel means all enabled panels.
- Invoice billing month or inclusive range. Billing uses **Gregorian `YYYY-MM`**;
  these are the invoice's service periods, not its send date or suspension date.
  For one month, choose the same start and end month. Either endpoint may be open.
- Current access state, minimum/maximum outstanding amount in Toman, and invoice age.
- Name/identifier search, explicit excluded account IDs, and pending-receipt exclusion.

Debt is the sum of unpaid sent/overdue/enforced invoices **in the selected periods**.
Draft, canceled and paid invoices do not create debt. Invoice age uses the same Tehran
calendar anchor as dunning: the granted deadline, otherwise the actual send timestamp.
Unknown send dates fail age restrictions; a future deadline shields overdue targeting.
The age test must match the same invoice as the overdue/deferred test, not another
invoice on that reseller. “No debt” always requires no open debt in **any** month.

“Suspended/frozen in month X” requires the current restricted state **and** a real,
non-reverted done/partial enforcement action tied to an invoice for month X. Manual
restrictions without an invoice remain available when choosing all months. An independent
access-state restriction can instead select all currently restricted accounts that have
an invoice in the chosen period, regardless of why they were restricted.

Pending-payment targeting by month checks both the payment's invoice pointer and its
multi-invoice settlement coverage. An unallocated receipt cannot prove a month's match.
Pending-receipt exclusion is global and includes the same Telegram person across panels,
so they are not reminded through another account while their receipt is awaiting review.

The preview shows every matched account, unique Telegram recipients, accounts without a
bot connection, period debt and age. A person with multiple matching accounts receives
**one** message; all their matched accounts remain visible in the list/CSV and totals.
An account exclusion removes that account; another eligible account sharing the same
Telegram chat can still receive the message. Exclude all matching accounts to omit that person.
CSV output neutralizes spreadsheet formulas in reseller-controlled names.

The web page requires a fresh preview and a final message confirmation. The bot supports
all audiences, panel multi-selection, paginated months, range entry (including Persian
digits), a complete recipient CSV, and final confirmation. Even inline `/broadcast text`
requires confirmation. A repeated Telegram confirmation button does not send twice.
If recipients or their displayed financial/access state change after preview, confirmation
is rejected and a fresh preview is required. A payment after the send has already started
cannot recall a Telegram message; recipients are resolved immediately before enqueue.

## API

- `GET /api/ops/broadcast/options`: invoice billing periods.
- `POST /api/ops/broadcast/preview`: existing audience/panel/threshold plus optional `filters`.
- `POST /api/ops/broadcast`: same body, `text`, and the preview's `preview_token`.
  A mismatched token returns **409** without scheduling delivery. Legacy callers without
  a token remain compatible; both first-party interfaces supply it.
- `GET /api/ops/broadcast/status`: current/last in-memory delivery progress.

Example preview:

```json
{
  "audience": "overdue",
  "filters": {
    "panel_ids": [1, 3],
    "period_from": "2026-07",
    "period_to": "2026-07",
    "min_debt": 100000,
    "min_age_days": 50,
    "max_age_days": 55,
    "exclude_pending": true
  }
}
```

All conditions intersect. Invalid ranges/unknown filter fields fail validation; they never
silently expand the audience. Existing threshold sales audiences still describe the
**current month's estimated sales**; the period filter independently restricts invoice
history. Sending retains the existing bounded-concurrency Telegram flood-control worker
and live/owner summary. Runs are in memory and do not survive a service restart; this
release adds no database migration.
