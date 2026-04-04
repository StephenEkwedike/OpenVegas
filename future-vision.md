# OpenVegas Future Vision
## From Terminal Casino Utility to Agent-Native Credit Bank

### TL;DR
OpenVegas starts with a simple behavior loop: run AI work, and when compute runs low, users can top up or play terminal casino games to try extending runway.

Over time, this evolves into:
1. **Agent-aware credit orchestration** (OpenClaw + other agents),
2. **Klarna-like BNPL for credits** (continuation credit),
3. **Social lending rails** (loan credits to trusted friends).

The core idea stays the same: **keep work moving** by turning brittle, stop-start compute billing into a resilient credit system.

---

## Where We Are Today (Grounded in Current Code)

OpenVegas already contains real BNPL/continuation primitives:

- DB tables for continuation and accounting:
  - `user_continuation_credit`
  - `continuation_claim_idempotency`
  - `continuation_accounting_events`
  - migration: `supabase/migrations/032_wallet_bootstrap_and_continuation.sql`
- API routes:
  - `GET /billing/continuation/status`
  - `POST /billing/continuation/claim`
  - in `server/routes/payments.py`
- Service logic:
  - eligibility and risk gates
  - idempotent claim replay
  - automatic principal repayment during top-up settlement
  - methods in `openvegas/payments/service.py`

So this is not a greenfield concept; it is an extension of an existing system.

---

## Vision Pillar 1: Agent-Native Credit Spending (OpenClaw + others)

### Goal
Let autonomous agents run workloads against policy-bounded OpenVegas credit envelopes instead of raw card rails.

### Product Behavior
- User grants an agent a spend envelope (e.g., `500 $V/day`).
- Agent spends against OpenVegas wallet in real-time.
- If balance drops below threshold, system can:
  - suggest top-up,
  - offer continuation credit (if eligible),
  - pause risky workloads.

### Proposed Interface
```http
POST /v1/agent/envelopes
POST /v1/agent/envelopes/{id}/approve
GET  /v1/agent/envelopes/{id}
POST /v1/agent/spend
```

### Example Envelope Contract
```json
{
  "agent_id": "openclaw:workspace-123",
  "max_daily_v": "500.000000",
  "max_single_tx_v": "25.000000",
  "allowed_tools": ["inference.chat", "inference.batch"],
  "risk_tier": "normal",
  "expires_at": "2026-04-30T00:00:00Z"
}
```

### Service Sketch
```python
class AgentBudgetService:
    async def authorize_spend(self, *, user_id: str, agent_id: str, amount_v: Decimal, tool: str) -> dict:
        envelope = await self._load_active_envelope(user_id=user_id, agent_id=agent_id)
        self._assert_tool_allowed(envelope, tool)
        self._assert_within_limits(envelope, amount_v)
        await self._reserve_funds(user_id=user_id, amount_v=amount_v, reason=f"agent:{agent_id}:{tool}")
        return {"approved": True, "reservation_id": "..."}
```

---

## Vision Pillar 2: BNPL-as-a-Product (Klarna-like for Compute Credits)

### Goal
Turn current “continuation claim” into a clear user-facing BNPL product:
- transparent principal,
- repayment waterfall,
- cooldown/risk controls,
- eventual underwriting tiers.

### Strategic Expansion: BNPL Beyond OpenVegas
Long-term, BNPL should not stop at OpenVegas. The target category is:
**"Klarna/CareCredit for AI compute and agent runtime."**

This means partnering with major LLM ecosystems (including OpenAI and Anthropic surfaces) once the core rail is proven.

#### Recommended Sequence
1. **Phase 1: Prove BNPL rail inside OpenVegas**
   - continuation credit performance,
   - default/repayment behavior,
   - fraud/abuse controls,
   - conversion and retention lift.
2. **Phase 2: Productize as external infrastructure**
   - merchant APIs for third-party AI products,
   - settlement + repayment engine,
   - underwriting/risk controls as a service.
3. **Phase 3: Native/white-label major-platform integration**
   - position as financing rail for workflow completion,
   - not as a "gambling feature,"
   - focus on enterprise/workspace and high-intent developer usage.

#### Why This Positioning
- OpenAI/Anthropic have enterprise billing and custom pricing motions, but public surfaces today do not imply a simple "drop-in BNPL button" lane.
- The strongest path is to approach from evidence:
  - lower abandonment at spend cliffs,
  - higher paid conversion,
  - predictable repayment and risk outcomes.

#### Product Surface to Build for Partnerships
- **Continuation Credit**: keep workflows moving immediately.
- **Usage Smoothing / Invoicing Rail**: convert bursty usage into predictable repayment.
- **Embedded Financing API**: let AI vendors offer BNPL without building underwriting/collections internally.

#### One-Sentence Framing
**"We become the financing layer for AI usage, starting with continuation credit for agentic workflows and expanding into embedded BNPL across AI platforms."**

### Existing Base
`claim_continuation()` already:
- checks paid-history/cooldown/risk block,
- issues principal into wallet,
- stores idempotent response,
- tracks outstanding principal and repayment events.

### Next API Surface
```http
GET  /billing/continuation/offers
POST /billing/continuation/claim
GET  /billing/continuation/ledger
POST /billing/continuation/cancel
```

### Offer Payload Example
```json
{
  "eligible": true,
  "offers": [
    {
      "offer_id": "cont_offer_basic_50",
      "principal_v": "50.000000",
      "cooldown_hours": 168,
      "fee_v": "0.000000",
      "terms_version": "continuation_v2"
    }
  ],
  "risk_band": "B"
}
```

### Underwriting Sketch
```python
def score_continuation_eligibility(*, paid_topups_90d: Decimal, chargeback_count: int, fraud_flags: int) -> str:
    if fraud_flags > 0 or chargeback_count > 0:
        return "deny"
    if paid_topups_90d >= Decimal("100"):
        return "A"
    if paid_topups_90d >= Decimal("25"):
        return "B"
    return "C"
```

### Repayment Waterfall Hook (extension of existing top-up settlement path)
```python
# called inside top-up settlement transaction
repaid_v, net_credit_v = await self._apply_continuation_repayment(
    tx=tx,
    user_id=user_id,
    gross_v=v_credit_gross,
    source_reference=topup_id,
    reason="topup_settlement",
)
await self.wallet.fund_from_card(
    account_id=f"user:{user_id}",
    amount_v=net_credit_v,
    reference_id=f"fiat_topup:{topup_id}",
    entry_type="fiat_topup",
    tx=tx,
)
```

---

## Vision Pillar 3: Social Credit Lending (Friend-to-Friend)

### Goal
Allow users to lend credits to friends with explicit terms and controlled risk exposure.

### User Story
- Alice offers Bob a credit loan (`100 $V`, optional due date).
- Bob accepts.
- Funds transfer immediately.
- Repayments can happen manually or from future top-ups.
- Every transfer and repayment is ledgered and auditable.

### Proposed Schema (new)
```sql
CREATE TABLE peer_credit_loans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lender_user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  borrower_user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  principal_v NUMERIC(18,6) NOT NULL CHECK (principal_v > 0),
  outstanding_v NUMERIC(18,6) NOT NULL CHECK (outstanding_v >= 0),
  status TEXT NOT NULL CHECK (status IN ('offered','active','repaid','defaulted','cancelled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at TIMESTAMPTZ,
  repaid_at TIMESTAMPTZ
);

CREATE TABLE peer_credit_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loan_id UUID NOT NULL REFERENCES peer_credit_loans(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN ('offer_created','accepted','repayment','writeoff','cancelled')),
  amount_v NUMERIC(18,6),
  actor_user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### API Sketch
```http
POST /billing/peer-loans/offers
POST /billing/peer-loans/{loan_id}/accept
POST /billing/peer-loans/{loan_id}/repay
GET  /billing/peer-loans
GET  /billing/peer-loans/{loan_id}
```

### Accept Flow Sketch
```python
async def accept_peer_loan(*, loan_id: str, borrower_user_id: str):
    async with db.transaction() as tx:
        loan = await lock_offer(tx, loan_id=loan_id, borrower_user_id=borrower_user_id)
        await ledger.transfer(
            tx=tx,
            debit_account=f"user:{loan.lender_user_id}",
            credit_account=f"user:{loan.borrower_user_id}",
            amount=loan.principal_v,
            entry_type="peer_loan_disbursement",
            reference_id=f"peer_loan:{loan.id}",
        )
        await mark_loan_active(tx, loan_id=loan.id)
        await record_peer_event(tx, loan_id=loan.id, event_type="accepted", actor_user_id=borrower_user_id)
```

---

## Compatibility with OpenClaw

OpenClaw integration should use a thin adapter, not a rewrite:
```python
class OpenClawCreditsAdapter:
    async def preflight(self, user_id: str, requested_v: Decimal) -> dict:
        # check wallet + continuation eligibility + envelope policy
        ...

    async def settle(self, user_id: str, consumed_v: Decimal, run_id: str) -> None:
        # post usage to OpenVegas ledger with idempotent reference
        ...
```

---

## Rollout Phases

### Phase 1 — Agent envelopes (safe spend controls)
- Add agent envelope APIs + policy checks.
- No change to repayment yet.

### Phase 2 — BNPL productization
- Add offers endpoint + terms surface.
- Keep continuation issuance conservative by default.

### Phase 3 — Social lending beta
- Peer loans for allowlisted users.
- Hard caps + anti-abuse controls.

### Phase 4 — Unified credit intelligence
- Portfolio view: wallet balance, continuation outstanding, peer obligations.
- Smarter auto-suggestions and limits.

---

## Safety, Risk, and Trust Defaults
- Idempotency required on all money-moving endpoints.
- Hard per-user and per-day limits.
- Risk blocklist remains enforceable via env + policy layer.
- All loan/repayment transitions are transactionally consistent and ledger-backed.
- No hidden fees in v1; fees require explicit versioned terms.

---

## Success Criteria
- Agents can spend within policy envelopes without balance drift.
- Continuation credit remains auditable and auto-repaid on top-ups.
- Peer loan lifecycle is fully traceable and reversible only by explicit state transitions.
- No duplicate credit issuance under retries/race conditions.
- User experience remains “work never stalls unexpectedly.”

---

## Important Changes or Additions to Public APIs/Interfaces/Types
Planned (future) additions referenced in the doc:
- Agent budget APIs: `/v1/agent/envelopes*`, `/v1/agent/spend`
- BNPL offer/ledger APIs: `/billing/continuation/offers`, `/billing/continuation/ledger`, optional cancel endpoint
- Social lending APIs: `/billing/peer-loans/*`
- New domain entities:
  - `ContinuationOffer`
  - `PeerCreditLoan`
  - `PeerCreditEvent`
  - `AgentSpendEnvelope`
- No immediate breaking changes to existing `/billing/continuation/status|claim` endpoints; these remain compatibility anchors.

---

## Test Cases and Scenarios (for later implementation)
1. **Idempotent continuation claim replay**
   - Same idempotency key returns stored response.
2. **Continuation repayment waterfall correctness**
   - Top-up repays principal first; only net credits wallet.
3. **Cooldown and risk gating**
   - Ineligible users receive deterministic deny reasons.
4. **Agent envelope guardrails**
   - Spend rejected when over per-tx/per-day/tool policy.
5. **Peer loan accept and repay**
   - Ledger entries and loan status transitions are atomic.
6. **Concurrency**
   - Double-accept and double-repay attempts do not duplicate funds.
7. **Auditability**
   - Every issuance/repayment/writeoff maps to an accounting event.

---

## Assumptions and Defaults
- Audience is engineering; depth is implementation-oriented.
- Existing continuation architecture (migration `032_*` + service methods) is the base, not replaced.
- BNPL v1 keeps fee model at `0` until terms/versioning is formalized.
- Social lending launches as allowlisted beta with strict caps.
- “OpenClaw support” is adapter-based integration on top of existing wallet/billing services.
- This document is visionary + technically grounded; it does not execute schema/API changes yet.
