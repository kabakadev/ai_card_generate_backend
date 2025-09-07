[Phase 1] Created feature branch. Will align env and config with working IntaSend backend. No routes yet.

[Phase 1] Added IntaSend env keys to app.config (no refactor). Startup prints masked sanity log.
No CORS changes. No routes added. requirements.txt: ensured python-dotenv, added requests.

# 📒 IntaSend Billing Integration Journal

This file tracks the integration of **IntaSend Hosted Checkout (M-Pesa)** into Flashlearn.
It serves both as a **roadmap** and a **logbook** of wins, challenges, and decisions.

---

## ✅ Phase 1 — Environment & Config Harmonization

**Goal:**
Load IntaSend env vars, verify startup log, keep CORS intact.

**Checklist**

- [x] `.env` contains `INTASEND_PUBLIC_KEY`, `INTASEND_SECRET_KEY`, `INTASEND_TEST_MODE`.
- [x] `.env.example` committed with dummy values.
- [x] `config.py` loads env and logs a **masked** sanity line.
- [x] Dependencies: `python-dotenv`, `requests` added to `requirements.txt`.
- [x] CORS allows `localhost` + `127.0.0.1`.

**Notes & Logs**

```
[2025-09-06 15:36:16,315] WARNING in config: [Billing] IntaSend test_mode=False public=********85d3 secret=********10ad currency=KES plan=100
```

---

## ✅ Phase 2 — Models & Migrations

**Goal:**
Add DB tables for `Subscription`, `PaymentTransaction`, `UsageLimits`.

**Checklist**

- [x] Alembic migration created/applied.
- [x] Models split into `models/billing/`.
- [x] Unique constraint on `(user_id, month_key)` in `UsageLimits`.
- [x] Verified with `flask db upgrade`.

**Notes & Logs**

---

## ⏳ Phase 3 — Services (dark, no routes yet)

**Goal:**
Port service logic from working backend.

**Checklist**

- [ ] `services/intasend_client.py` (checkout + verify).
- [ ] `services/subscription_manager.py`.
- [ ] `services/usage_tracker.py`.
- [ ] Imports adjusted for Flashlearn structure.
- [ ] REPL/pytest confirms functions work.

## **Notes & Logs**

[Phase 4] Implemented billing routes (no webhooks):

- POST /billing/checkout → creates hosted checkout + pending tx
- POST /billing/verify → verifies invoice_id with IntaSend and activates subscription
- GET /billing/status → returns sub + usage; includes optional auto-reconcile for latest pending tx
- Deferred webhooks (kept class placeholder for future)
- Frontend: after redirect, call /billing/verify with invoice_id, then poll /billing/status

## ⏳ Phase 5 — AI Gating

**Goal:**
Gate `/ai/generate` behind free quota or active subscription.

**Checklist**

- [ ] Pre-check with `usage_tracker.can_generate`.
- [ ] Block with 402/403 when quota exhausted.
- [ ] Increment usage after successful generation.
- [ ] Logs show `{user_id, used, remaining}`.

**Notes & Logs**

---

## ⏳ Phase 6 — Frontend API + UI

**Goal:**
Expose upgrade CTA and show usage.

**Checklist**

- [ ] `src/utils/billingApi.js` (`createCheckout`, `getBillingStatus`).
- [ ] Upgrade button in `NavBar.jsx`.
- [ ] Billing modal/page.
- [ ] AI modal shows free prompts left; blocks & prompts upgrade.

**Notes & Screenshots**

---

## ⏳ Phase 7 — End-to-End QA

**Goal:**
Validate flow: checkout → webhook → status → AI gating.

**Checklist**

- [ ] Happy path works (payment → subscription active).
- [ ] Free quota enforced for inactive users.
- [ ] Duplicate webhooks idempotent.
- [ ] Failed/abandoned payments handled.
- [ ] Month rollover creates fresh `UsageLimits`.

**Evidence**

- [ ] Screenshots of frontend gating.
- [ ] Logs of webhook + status.
- [ ] Curl output for `/billing/status`.

---

## ⏳ Phase 8 — Hardening

**Goal:**
Production safety.

**Checklist**

- [ ] Idempotency enforced by `api_ref`.
- [ ] Observability: structured logs.
- [ ] Rate limiting on checkout route.
- [ ] Secrets only in env, not repo.

**Notes**

---

## ⏳ Phase 9 — Documentation & PR Gates

**Goal:**
Ensure clear review + rollback plan.

**Checklist**

- [ ] Journal updated for each phase.
- [ ] PR template/checklist followed.
- [ ] Rollback plan defined.

**Notes**

---

## ⏳ Phase 10 — Cutover

**Goal:**
Flip live with confidence.

**Checklist**

- [ ] Test mode verified in staging.
- [ ] Live test payment succeeds.
- [ ] Monitoring in place.
- [ ] Grace period considered.

**Notes**

---

# 📝 Running Log

Use this section for quick entries as you progress.

- **2025-09-06**: Phase 1 done. Env vars wired. Masked log confirmed. No regressions.
- **…**
