"""Reconcile Stripe billing invariants for topups and org invoice credits."""

from __future__ import annotations

import asyncio
import os

import asyncpg


QUERY_TOPUPS = """
SELECT ft.id
FROM fiat_topups ft
LEFT JOIN ledger_entries le
  ON le.reference_id = 'fiat_topup:' || ft.id::text
 AND le.entry_type = 'fiat_topup'
WHERE ft.status = 'paid'
GROUP BY ft.id
HAVING COUNT(le.id) <> 1
"""


QUERY_INVOICE_DUPES = """
SELECT reference_id, COUNT(*) AS cnt
FROM org_budget_ledger
WHERE source = 'stripe_subscription'
  AND reference_id LIKE 'stripe_invoice:%'
GROUP BY reference_id
HAVING COUNT(*) <> 1
"""


QUERY_SUB_PROJECTION = """
SELECT org_id, stripe_subscription_status, has_active_subscription, current_period_end
FROM org_sponsorships
WHERE (
    stripe_subscription_status IN ('active', 'trialing')
    AND current_period_end > now()
    AND has_active_subscription = FALSE
) OR (
    (stripe_subscription_status NOT IN ('active', 'trialing') OR current_period_end <= now())
    AND has_active_subscription = TRUE
)
"""


async def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is required")

    conn = await asyncpg.connect(dsn)
    try:
        bad_topups = await conn.fetch(QUERY_TOPUPS)
        bad_invoices = await conn.fetch(QUERY_INVOICE_DUPES)
        bad_projection = await conn.fetch(QUERY_SUB_PROJECTION)
    finally:
        await conn.close()

    failures = 0
    if bad_topups:
        failures += 1
        print(f"FAIL: {len(bad_topups)} paid topups with non-1 ledger settlement")
        for row in bad_topups:
            print(dict(row))
    else:
        print("OK: paid topups each reconcile to exactly one fiat_topup ledger entry")

    if bad_invoices:
        failures += 1
        print(f"FAIL: {len(bad_invoices)} invoice references with duplicate/missing org budget credits")
        for row in bad_invoices:
            print(dict(row))
    else:
        print("OK: invoice-based org budget credits are exactly-once")

    if bad_projection:
        failures += 1
        print(f"FAIL: {len(bad_projection)} org subscription projection inconsistencies")
        for row in bad_projection:
            print(dict(row))
    else:
        print("OK: org subscription projection flags are consistent")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

