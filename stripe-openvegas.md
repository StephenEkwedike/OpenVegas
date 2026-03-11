# OpenVegas Stripe Billing Plan (Enhanced, Billing-Only Scope)

## Summary
1. Implement Stripe for:
   - User `$V` card top-ups.
   - Org sponsorship monthly subscription billing.
2. Keep this rollout **billing-only**:
   - no user premium-entitlement model,
   - no `isPremium`-style feature gating.
3. Treat Stripe as canonical external billing truth and local DB as synchronized projection.
4. Webhook processing is authoritative and idempotent; redirect/verify endpoints are UX-only helpers.

## Enhancements Included
1. Canonical source-of-truth model clarified.
2. SQL-enforced state transitions (no code-only transition rules).
3. Webhook event-id idempotency table + payload hash tracking.
4. Strong idempotency model for top-up create (`idempotency_key` + payload hash).
5. Unique constraints for Stripe IDs.
6. Org subscription projection expanded to include cancel/period fields and raw Stripe status.
7. Stripe Billing Portal session route added for org self-service management.
8. Reconciliation requirements expanded (payments + ledger + org budget).
9. Consumer premium abstractions explicitly removed (`isPremium`, `subscriptionPlan`).
10. Shared Stripe-to-DB sync helper required for org subscription projection (single logic path).
11. Expanded Stripe status handling (`incomplete`, `incomplete_expired`, `unpaid`, `paused`) in projections/checks.

## Required Inputs
1. Stripe keys and receiving account:
   - `STRIPE_SECRET_KEY`
   - `STRIPE_PUBLISHABLE_KEY`
   - `STRIPE_WEBHOOK_SECRET`
   - `STRIPE_ORG_PRICE_ID`
   - Stripe account id (`acct_...`) for the OpenVegas account receiving funds.
2. URL settings:
   - `APP_BASE_URL`
   - `CHECKOUT_SUCCESS_URL`
   - `CHECKOUT_CANCEL_URL`
3. Commercial defaults:
   - `V_PER_USD=100`
   - top-up min/max USD bounds
   - minimum org monthly budget

## Dependency Pin (Before Adapter Coding)
1. Pin `stripe-python` to an exact version before implementation to avoid request-option call-shape drift.
2. Planned pin for this rollout: `stripe==14.3.0`.

```toml
# pyproject.toml
[project]
dependencies = [
  # ...
  "stripe==14.3.0",
]
```

## Public Interface Changes
1. `POST /billing/topups/checkout`
2. `GET /billing/topups/{topup_id}`
3. `POST /billing/webhook/stripe`
4. `POST /billing/orgs/{org_id}/subscription/checkout`
5. `GET /billing/orgs/{org_id}/subscription/status`
6. `POST /billing/orgs/{org_id}/portal-session`
7. CLI:
   - `openvegas deposit <amount>`
   - `openvegas deposit-status <topup_id>`

## Code Snippets

### 1) Environment additions (`.env.example`)
```env
# Stripe runtime
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_PUBLISHABLE_KEY=pk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
STRIPE_ORG_PRICE_ID=price_xxx

# Checkout URLs
APP_BASE_URL=http://127.0.0.1:8085
CHECKOUT_SUCCESS_URL=http://127.0.0.1:8085/ui?checkout=success
CHECKOUT_CANCEL_URL=http://127.0.0.1:8085/ui?checkout=cancel

# Pricing and bounds
V_PER_USD=100
TOPUP_MIN_USD=1
TOPUP_MAX_USD=500
ORG_MONTHLY_MIN_USD=10
```

### 2) Migration `013_stripe_billing.sql`
```sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'fiat_topup_status') THEN
    CREATE TYPE fiat_topup_status AS ENUM (
      'created', 'checkout_created', 'paid', 'failed', 'reversed'
    );
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS fiat_topups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  amount_usd NUMERIC(18,6) NOT NULL CHECK (amount_usd > 0),
  v_credit NUMERIC(18,6) NOT NULL CHECK (v_credit > 0),
  currency TEXT NOT NULL DEFAULT 'usd',
  status fiat_topup_status NOT NULL DEFAULT 'created',
  idempotency_key TEXT NOT NULL,
  idempotency_payload_hash TEXT NOT NULL,
  stripe_customer_id TEXT,
  stripe_checkout_session_id TEXT UNIQUE,
  stripe_payment_intent_id TEXT UNIQUE,
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_fiat_topups_user_created
  ON fiat_topups(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE org_sponsorships
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT UNIQUE,
  ADD COLUMN IF NOT EXISTS stripe_price_id TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_status TEXT,
  ADD COLUMN IF NOT EXISTS has_active_subscription BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS uq_org_sponsorships_stripe_customer_id
ON org_sponsorships(stripe_customer_id)
WHERE stripe_customer_id IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'ck_org_sponsorships_stripe_status'
  ) THEN
    ALTER TABLE org_sponsorships
      ADD CONSTRAINT ck_org_sponsorships_stripe_status
      CHECK (
        stripe_subscription_status IS NULL OR stripe_subscription_status IN (
          'inactive',
          'incomplete',
          'incomplete_expired',
          'trialing',
          'active',
          'past_due',
          'canceled',
          'unpaid',
          'paused'
        )
      );
  END IF;
END $$;

INSERT INTO wallet_accounts (account_id, balance)
VALUES ('fiat_reserve', 0)
ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations(version)
VALUES ('013_stripe_billing')
ON CONFLICT (version) DO NOTHING;
```

### 3) Startup schema compatibility gate (`server/services/dependencies.py`)
```python
await require_migration_min(db, "013_stripe_billing")
await require_tables(db, {"fiat_topups", "stripe_webhook_events"})
await require_columns(
    db,
    {
        ("org_sponsorships", "stripe_subscription_status"),
        ("org_sponsorships", "has_active_subscription"),
        ("org_sponsorships", "cancel_at_period_end"),
        ("org_sponsorships", "current_period_end"),
    },
)
```

### 4) Canonical payload hash helper for idempotency
```python
import hashlib
import json
from decimal import Decimal

def canonical_payload_hash(payload: dict) -> str:
    def norm(v):
        if isinstance(v, Decimal):
            return format(v.normalize(), "f")
        if isinstance(v, dict):
            return {k: norm(v[k]) for k in sorted(v.keys())}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v

    canonical = json.dumps(norm(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
```

### 5) Stripe gateway adapter (`openvegas/payments/stripe_gateway.py`)
```python
import os
from decimal import Decimal
import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

class StripeGateway:
    @staticmethod
    def create_topup_checkout(*, customer_id: str, amount_usd: Decimal, topup_id: str) -> dict:
        cents = int((amount_usd * Decimal("100")).quantize(Decimal("1")))
        s = stripe.checkout.Session.create(
            mode="payment",
            customer=customer_id,
            client_reference_id=topup_id,
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "OpenVegas $V Top-up"},
                    "unit_amount": cents,
                },
                "quantity": 1,
            }],
            success_url=os.environ["CHECKOUT_SUCCESS_URL"],
            cancel_url=os.environ["CHECKOUT_CANCEL_URL"],
            metadata={"topup_id": topup_id},
            idempotency_key=f"topup-checkout:{topup_id}",
        )
        return {"id": s.id, "url": s.url, "payment_intent": s.payment_intent}

    @staticmethod
    def create_org_subscription_checkout(
        *,
        customer_id: str,
        price_id: str,
        org_id: str,
        checkout_attempt_id: str,
    ) -> dict:
        s = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            client_reference_id=org_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=os.environ["CHECKOUT_SUCCESS_URL"],
            cancel_url=os.environ["CHECKOUT_CANCEL_URL"],
            metadata={"org_id": org_id, "purpose": "org_sponsorship"},
            subscription_data={
                "metadata": {"org_id": org_id, "purpose": "org_sponsorship"},
            },  # persisted onto created Subscription for customer.subscription.* webhooks
            idempotency_key=f"org-sub-checkout:{org_id}:{checkout_attempt_id}",
        )
        return {"id": s.id, "url": s.url, "subscription": s.subscription}

    @staticmethod
    def create_billing_portal(customer_id: str) -> str:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=os.environ["APP_BASE_URL"] + "/ui",
        )
        return portal.url

    @staticmethod
    def construct_event(raw_body: bytes, signature: str) -> dict:
        return stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=signature,
            secret=os.environ["STRIPE_WEBHOOK_SECRET"],
        )
```

### 6) SQL-guarded transition helper
```python
async def transition_topup(tx, topup_id: str, from_status: str, to_status: str) -> None:
    row = await tx.fetchrow(
        """
        UPDATE fiat_topups
        SET status = $3, updated_at = now()
        WHERE id = $1 AND status = $2
        RETURNING id
        """,
        topup_id, from_status, to_status,
    )
    if not row:
        raise ValueError(f"Illegal topup transition {from_status}->{to_status} ({topup_id})")
```

### 7) Webhook idempotency + settlement path (`openvegas/payments/service.py`)
```python
async def settle_topup_from_webhook(self, event: dict):
    event_id = event["id"]
    event_type = event["type"]
    session = event["data"]["object"]
    payload_hash = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()

    async with self.db.transaction() as tx:
        existing = await tx.fetchrow(
            """
            SELECT event_id, payload_hash
            FROM stripe_webhook_events
            WHERE event_id = $1
            """,
            event_id,
        )
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise ValueError(f"Webhook payload hash mismatch for event {event_id}")
            return {"status": "duplicate"}

        ins = await tx.fetchrow(
            """
            INSERT INTO stripe_webhook_events(event_id, event_type, payload_hash)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            RETURNING event_id
            """,
            event_id, event_type, payload_hash,
        )
        if not ins:
            return {"status": "duplicate"}

        if session.get("payment_status") != "paid":
            return {"status": "not-paid"}

        row = await tx.fetchrow(
            """
            UPDATE fiat_topups
            SET status = 'paid',
                stripe_payment_intent_id = $2,
                updated_at = now()
            WHERE stripe_checkout_session_id = $1
              AND status IN ('created', 'checkout_created')
            RETURNING id, user_id, v_credit
            """,
            session["id"], session.get("payment_intent"),
        )
        if not row:
            return {"status": "already-settled-or-missing"}

        await self.wallet.fund_from_card(
            account_id=f"user:{row['user_id']}",
            amount_v=Decimal(str(row["v_credit"])),
            reference_id=f"fiat_topup:{row['id']}",
            tx=tx,
        )
        return {"status": "paid", "topup_id": str(row["id"])}
```

### 8) Top-up create idempotency semantics
```python
payload_hash = canonical_payload_hash({"amount_usd": str(amount_usd), "currency": "usd"})
existing = await tx.fetchrow(
    """
    SELECT id, status, idempotency_payload_hash
    FROM fiat_topups
    WHERE user_id = $1 AND idempotency_key = $2
    FOR UPDATE
    """,
    user_id, idempotency_key,
)
if existing:
    if existing["idempotency_payload_hash"] != payload_hash:
        raise ValueError("IDEMPOTENCY_PAYLOAD_CONFLICT")
    return {"topup_id": str(existing["id"]), "status": str(existing["status"])}
```

### 9) Org subscription projection sync (webhook)
```python
# customer.subscription.updated / deleted
await tx.execute(
    """
    UPDATE org_sponsorships
    SET stripe_subscription_status = $2,
        has_active_subscription = $3,
        stripe_price_id = COALESCE($4, stripe_price_id),
        cancel_at_period_end = $5,
        current_period_end = to_timestamp($6),
        updated_at = now()
    WHERE stripe_subscription_id = $1
    """,
    sub_id,
    sub_status,                         # raw Stripe status string
    has_active_subscription,
    stripe_price_id,
    bool(cancel_at_period_end),
    current_period_end_unix,
)
```

### 10) Shared Stripe sync helper (single source of projection logic)
```python
def compute_has_active_subscription(subscription: dict) -> bool:
    status = str(subscription.get("status", "inactive"))
    status_ok = status in {"active", "trialing"}
    current_period_end = subscription.get("current_period_end")
    if not current_period_end:
        return status_ok
    return status_ok and datetime.fromtimestamp(current_period_end, tz=timezone.utc) > datetime.now(timezone.utc)

async def sync_org_sponsorship_from_subscription(tx, *, org_id: str, subscription: dict) -> None:
    item0 = (subscription.get("items", {}) or {}).get("data", [{}])[0]
    price_id = ((item0 or {}).get("price") or {}).get("id")
    await tx.execute(
        """
        UPDATE org_sponsorships
        SET stripe_subscription_id = $2,
            stripe_subscription_status = $3,
            has_active_subscription = $4,
            stripe_price_id = COALESCE($5, stripe_price_id),
            cancel_at_period_end = $6,
            current_period_end = to_timestamp($7),
            updated_at = now()
        WHERE org_id = $1
        """,
        org_id,
        subscription["id"],
        subscription["status"],
        compute_has_active_subscription(subscription),
        price_id,
        bool(subscription.get("cancel_at_period_end", False)),
        subscription.get("current_period_end"),
    )
```

### 11) No paid-state mutation during checkout creation
```python
# Good pattern: creating checkout may persist customer id, but does not mark subscription active.
if not sponsorship["stripe_customer_id"]:
    customer = stripe.Customer.create(email=owner_email, metadata={"org_id": org_id})
    await tx.execute(
        "UPDATE org_sponsorships SET stripe_customer_id=$2, updated_at=now() WHERE org_id=$1",
        org_id, customer["id"],
    )

# no writes to has_active_subscription / stripe_subscription_status here
```

### 12) FastAPI route snippets (`server/routes/payments.py`)
```python
@router.post("/billing/topups/checkout")
async def topup_checkout(req: TopupCheckoutRequest, user=Depends(get_current_user)):
    return await get_billing_service().create_topup_checkout(
        user_id=user["user_id"],
        amount_usd=Decimal(str(req.amount_usd)),
        idempotency_key=req.idempotency_key,
    )

@router.post("/billing/orgs/{org_id}/portal-session")
async def org_portal_session(org_id: str, user=Depends(get_current_user)):
    await require_org_admin(org_id, user["user_id"], db=get_db())
    return await get_billing_service().create_org_billing_portal(org_id, user["user_id"])

@router.post("/billing/webhook/stripe")
async def stripe_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("stripe-signature", "")
    event = get_billing_service().stripe_gateway.construct_event(raw, sig)
    return await get_billing_service().handle_event(event)
```

### 13) Webhook dispatch uses shared sync helper
```python
if event_type == "checkout.session.completed":
    await settle_topup_from_event(tx, event_obj)
elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
    org_id = resolve_org_id_from_subscription(event_obj)
    await sync_org_sponsorship_from_subscription(tx, org_id=org_id, subscription=event_obj)
elif event_type == "invoice.paid":
    await apply_org_budget_credit_once(tx, invoice=event_obj, webhook_event_id=event_id)
elif event_type == "invoice.payment_failed":
    await mark_org_subscription_past_due(tx, invoice=event_obj)
```

### 14) Deterministic org resolver (subscription metadata first)
```python
def resolve_org_id_from_subscription(subscription: dict) -> str:
    metadata = subscription.get("metadata") or {}
    org_id = metadata.get("org_id")
    if org_id:
        return org_id
    raise ValueError("Missing org_id in subscription metadata")
```

### 15) Idempotent invoice.paid budget crediting (independent guard)
```python
async def apply_org_budget_credit_once(tx, *, invoice: dict, webhook_event_id: str):
    subscription_id = invoice.get("subscription")
    amount_paid = Decimal(invoice.get("amount_paid", 0)) / Decimal("100")

    org = await tx.fetchrow(
        "SELECT org_id FROM org_sponsorships WHERE stripe_subscription_id = $1",
        subscription_id,
    )
    if not org:
        return {"status": "org-not-found"}

    # Independent idempotency: budget credit is keyed by Stripe invoice id.
    # Safe even if webhook event dedupe is bypassed.
    await tx.execute(
        """
        INSERT INTO org_budget_ledger (org_id, source, delta_usd, reference_id)
        VALUES ($1, 'stripe_subscription', $2, $3)
        ON CONFLICT DO NOTHING
        """,
        org["org_id"],
        amount_paid,
        f"stripe_invoice:{invoice['id']}",
    )
    return {"status": "credited"}
```

### 16) Card-only settlement assumption
```text
Top-up settlement in this phase assumes Checkout Sessions constrained to:
payment_method_types=["card"].

If delayed payment methods are enabled later, webhook handling must add:
- checkout.session.async_payment_succeeded
- checkout.session.async_payment_failed
```

### 17) Stripe SDK idempotency request-options compatibility note
```python
# IMPORTANT: pin stripe-python to an exact version in dependencies before implementation.
# Current repo spec is a range (stripe>=8.0), so exact runtime call shape is not deterministic.

# For modern stripe-python (v8+), request options such as idempotency key are passed as keyword args:
stripe.checkout.Session.create(..., idempotency_key=f"topup-checkout:{topup_id}")

# Fallback request-options style (if required by SDK version):
stripe.checkout.Session.create(
    ...,
    options={"idempotency_key": f"topup-checkout:{topup_id}"},
)
```

### 18) Success/verify UI is informational only (never fulfillment trigger)
```python
# success page endpoint: informational status lookup only
@router.get("/billing/topups/{topup_id}/verify")
async def verify_topup_status(topup_id: str, user=Depends(get_current_user)):
    row = await get_db().fetchrow(
        """
        SELECT id, status, v_credit, stripe_checkout_session_id, updated_at
        FROM fiat_topups
        WHERE id = $1 AND user_id = $2
        """,
        topup_id, user["user_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Top-up not found")
    # no settlement side effects here
    return dict(row)
```

```text
Rule: only webhook handlers can trigger wallet crediting/fulfillment.
UI success/verify routes must be read-only projections for user feedback.
```

### 19) Portal deep-link flows for org admin actions
```python
# Direct admin to targeted Stripe portal actions with flow_data.
portal = stripe.billing_portal.Session.create(
    customer=stripe_customer_id,
    return_url=os.environ["APP_BASE_URL"] + "/ui",
    flow_data={
        "type": "subscription_cancel",
        "subscription_cancel": {"subscription": stripe_subscription_id},
        "after_completion": {
            "type": "redirect",
            "redirect": {"return_url": os.environ["APP_BASE_URL"] + "/ui?billing=done"},
        },
    },
)
```

```python
# Alternative flow for payment method updates:
portal = stripe.billing_portal.Session.create(
    customer=stripe_customer_id,
    return_url=os.environ["APP_BASE_URL"] + "/ui",
    flow_data={"type": "payment_method_update"},
)
```

### 20) Generic billing entitlement dependency (for paid org features only)
```python
async def require_active_org_subscription(org_id: str, user_id: str, db) -> None:
    row = await db.fetchrow(
        """
        SELECT has_active_subscription
        FROM org_sponsorships
        WHERE org_id = $1
        """,
        org_id,
    )
    if not row or not bool(row["has_active_subscription"]):
        raise HTTPException(status_code=403, detail="Active org subscription required")
```

### 21) Portal service guard
```python
async def create_org_billing_portal(self, org_id: str, user_id: str):
    sponsorship = await self.db.fetchrow(
        "SELECT stripe_customer_id FROM org_sponsorships WHERE org_id = $1",
        org_id,
    )
    if not sponsorship or not sponsorship["stripe_customer_id"]:
        raise HTTPException(status_code=400, detail="Org has no Stripe customer configured")

    portal_url = self.stripe_gateway.create_billing_portal(sponsorship["stripe_customer_id"])
    return {"url": portal_url}
```

### 22) CLI snippets (`openvegas/cli.py`)
```python
@cli.command()
@click.argument("amount")
def deposit(amount: str):
    async def _run():
        from decimal import Decimal
        c = OpenVegasClient()
        data = await c.create_topup_checkout(Decimal(amount))
        console.print(f"Top-up ID: {data['topup_id']}")
        console.print(f"Checkout URL: {data['checkout_url']}")
    run_async(_run())

@cli.command("deposit-status")
@click.argument("topup_id")
def deposit_status(topup_id: str):
    async def _run():
        c = OpenVegasClient()
        data = await c.get_topup_status(topup_id)
        console.print(f"Status: {data['status']}  Credit: {data.get('v_credit', '0')} $V")
    run_async(_run())
```

### 23) Reconciliation query snippets
```sql
-- paid topups must map to one fiat_topup ledger entry
SELECT ft.id
FROM fiat_topups ft
LEFT JOIN ledger_entries le
  ON le.reference_id = 'fiat_topup:' || ft.id::text
 AND le.entry_type = 'fiat_topup'
WHERE ft.status = 'paid'
GROUP BY ft.id
HAVING COUNT(le.id) <> 1;
```

```sql
-- invoice-level budget credits should be exactly-once by Stripe invoice id
SELECT reference_id, COUNT(*) AS cnt
FROM org_budget_ledger
WHERE source = 'stripe_subscription'
  AND reference_id LIKE 'stripe_invoice:%'
GROUP BY reference_id
HAVING COUNT(*) <> 1;
```

```sql
-- projection consistency: active-like status should align with has_active_subscription
SELECT org_id, stripe_subscription_status, has_active_subscription, current_period_end
FROM org_sponsorships
WHERE (
    stripe_subscription_status IN ('active', 'trialing')
    AND current_period_end > now()
    AND has_active_subscription = FALSE
) OR (
    (stripe_subscription_status NOT IN ('active', 'trialing') OR current_period_end <= now())
    AND has_active_subscription = TRUE
);
```

### 24) Removed legacy abstraction checklist
```text
- Do NOT add user-level fields: isPremium, subscriptionPlan.
- Do NOT introduce premium-only middleware.
- Do NOT write paid-state fields during checkout creation.
- Use org billing entitlement checks only where required by paid org features.
```

## Test Cases and Scenarios
1. Signature validation:
   - invalid signature returns `400`.
2. Webhook replay:
   - duplicate `event_id` returns duplicate/no-op; no double-credit.
   - duplicate `event_id` with different payload hash raises an alertable error path.
3. Top-up payment status:
   - `checkout.session.completed` only settles if `payment_status == "paid"`.
   - non-paid sessions return `not-paid` and do not credit.
4. Top-up idempotency:
   - same key + same payload returns existing row.
   - same key + different payload raises conflict.
5. Transition safety:
   - illegal status transition attempts are rejected.
6. Ledger integrity:
   - each `paid` top-up has exactly one `fiat_topup` ledger entry.
7. Org subscription projection:
   - webhook updates status, cancel flag, period end, and `has_active_subscription` deterministically.
8. Shared sync helper:
   - all subscription webhook paths and reconciliation call the same helper.
9. Checkout write safety:
   - checkout creation persists customer/session ids only, not active status.
10. Portal flow:
   - org billing portal session only for authorized org owner/admin.
11. Stripe request idempotency:
   - checkout create retries use Stripe idempotency keys and do not create duplicate sessions.
12. Checkout correlation:
   - `client_reference_id` is populated and matches internal `topup_id`/`org_id`.
13. Subscription metadata mapping:
   - `subscription_data.metadata.org_id` is present and can be resolved from `customer.subscription.*` events.
14. UI verify semantics:
   - success/verify endpoints are read-only and never perform settlement.
15. Portal deep links:
   - org admin can be directed to specific portal flows (`subscription_cancel`, `payment_method_update`) when desired.
16. Regression:
   - existing mint/game/store/agent flows unchanged.

## Required Fixes Before Execution
1. Keep billing-only abstraction; no consumer premium model fields.
2. Use `STRIPE_ORG_PRICE_ID` instead of premium-tier naming.
3. Use shared Stripe projection sync helper from webhook + reconciliation.
4. Enforce webhook idempotency before side effects.
5. Keep checkout creation side-effect free for paid-state flags.
6. Keep top-up settlement card-only in this phase unless async payment events are implemented.
7. Ensure org subscription Checkout sets both session metadata and `subscription_data.metadata`.
8. Use attempt-scoped org checkout idempotency keys (`org_id + checkout_attempt_id`).
9. Confirm pinned stripe-python SDK idempotency-key call style during implementation.
10. Ensure `resolve_org_id_from_subscription()` uses subscription metadata as the primary mapping source.
11. Keep `invoice.paid` budget crediting independently idempotent by Stripe invoice id.
12. Pin stripe-python to an exact version before implementation and keep idempotency call style aligned with that version.
13. Keep success/verify UI endpoints read-only (informational only).
14. Add portal deep-link flow support for admin subscription-management/cancellation paths.
15. Use `stripe==14.3.0` unless explicitly revalidated and changed before coding.

## Rollout and Verification
1. Apply migration `013_stripe_billing.sql` in Supabase.
2. Run staging with Stripe test keys.
3. Register webhook endpoint and replay sample events from Stripe CLI/dashboard.
4. Execute reconciliation checks with clean output.
5. Canary release for a small user/org cohort.
6. Promote to live keys after canary + reconciliation pass.

## Assumptions and Defaults
1. Billing-only scope for this phase.
2. Stripe receiving account is the one behind backend `STRIPE_SECRET_KEY`.
3. `$V` conversion fixed to `100 $V = $1` in this rollout.
4. Webhook is authoritative settlement path.
5. Refunds are manual support actions (no self-serve refund API in this phase).
6. Paid gating (if any) is org-subscription based, not consumer premium-tier based.
