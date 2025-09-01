# 💳 Freemium + M-Pesa (IntaSend) Integration Plan

This doc explains how we’ll add a **freemium model with paid subscription (KES 100/month)** to Flashlearn using **IntaSend Hosted Checkout (M-Pesa)**. It covers the **roadmap**, the **files we’ll add**, and **how they plug into the current codebase**.

> **Freemium rule:** Each user gets **5 AI generation prompts per month** for free. After that, they must subscribe (KES 100/month) to continue generating AI cards.

---

## 🧭 Roadmap (Backend → Frontend → Rollout)

### Phase 1 — Backend Foundations

1. Add **config + env** for IntaSend and billing.
2. Add **models** (or reuse existing migrations) for `Subscription`, `PaymentTransaction`, and **monthly usage** tracking (`UsageLimits` or `AIGeneration` counter per month).
3. Add **services**:

   - `services/intasend_client.py` (create/verify checkout)
   - `services/subscription_manager.py` (activate/expire subscription)
   - `services/usage_tracker.py` (count AI prompts; enforce free cap)

4. Add **routes**:

   - `POST /billing/checkout` – create hosted checkout session
   - `GET /billing/status` – fetch subscription/payment status
   - `POST /billing/webhooks/intasend` – webhook receiver (verifies events)

5. **Gate AI generation** in `routes/ai_routes.py` using usage tracker + subscription status.

### Phase 2 — Frontend Integration

1. Add **billing UI**:

   - “Upgrade” button in `NavBar.jsx`
   - New **Billing** page/modal showing status, “Subscribe KES 100”, and remaining free prompts

2. Add **API calls** to payments:

   - `utils/billingApi.js` (`createCheckout()`, `getBillingStatus()`)

3. Update **AI modal** to show: “X free prompts left this month”; block and prompt to upgrade when exhausted.
4. Handle **checkout redirect & return**:

   - Open hosted checkout URL
   - After return, **poll `/billing/status`** and show success/failure

### Phase 3 — Rollout & QA

1. Seed test users, simulate payments with test keys.
2. Test webhook path + signature verification.
3. Add basic audit logs.
4. Monitor errors & retries.

---

## 🗂️ Files to Add (Backend)

> Base paths relative to `flashlearn-backend/`

### 1) Config & Requirements

- **`requirements.txt`**
  Add (if not already present): `requests`, `python-dotenv` (optional), `pytz`/`pendulum` (optional for monthly windows).

- **`.env` (or environment variables)**

  ```
  INTASEND_API_KEY=...
  INTASEND_PUBLIC_KEY=...
  INTASEND_TEST=true         # true in dev; false in prod
  BILLING_PLAN_MONTHLY_KES=100
  BILLING_CURRENCY=KES
  APP_BASE_URL=http://127.0.0.1:5000
  FRONTEND_BASE_URL=http://127.0.0.1:5173
  ```

- **`config.py`**

  - Add getters for the above.
  - Ensure CORS allows your frontend origin(s).

### 2) Models (if not already in `models.py`)

Check your migrations: you already have:

```
migrations/versions/
  ac20f62bf26c_add_payments_user_credits_ai_generations.py
```

If models aren’t present yet, add minimal ones (or align to your separate project):

- **`Subscription`**

  - `id`, `user_id`, `status` (`active`, `expired`, `canceled`), `plan` (`monthly`), `current_period_start`, `current_period_end`, timestamps

- **`PaymentTransaction`**

  - `id`, `user_id`, `amount`, `currency`, `status` (`initiated`, `pending`, `succeeded`, `failed`), `provider` (`intasend`), `provider_ref`, `api_ref`, `plan_type`, timestamps, **idempotency key** (optional)

- **`UsageLimits`** (or reuse `AIGeneration` rows to count)

  - `id`, `user_id`, `month_key` (e.g., `YYYY-MM`), `ai_prompt_count`, `free_quota` (default 5)

> **Monthly window**: compute `month_key = now().strftime('%Y-%m')` and track counts per month. No cron needed; just new rows per month.

### 3) Services

Create a `services/` package mirroring your separate backend:

- **`services/intasend_client.py`**

  - Wrap **Hosted Checkout** creation:

    - Input: `user_id`, `amount`, `currency`, optional `metadata` (`plan_type`, email)
    - Return: `{ checkout_url, api_ref }`

  - Verification helper (given `api_ref`):

    - Query IntaSend API, return `{status, provider_ref, ...}`

- **`services/subscription_manager.py`**

  - `activate(user_id, plan='monthly')` → sets `status='active'`, sets `current_period_start` and `current_period_end` (+30 days)
  - `expire_if_needed()` (optional helper)
  - `get_status(user_id)` → `active`/`inactive`, ends at, plan

- **`services/usage_tracker.py`**

  - `get_month_key(now)`
  - `get_usage(user_id, month_key)` → count so far
  - `increment(user_id, month_key, n=1)`
  - `can_generate(user_id)` → if usage < 5 **OR** subscription is active

### 4) Routes

Create a new folder or add to `routes/`:

- **`routes/payments_routes.py`**

  - `POST /billing/checkout`

    - Auth required.
    - Creates IntaSend checkout (KES 100, monthly), saves `PaymentTransaction` with `status='initiated'`, returns `{checkout_url, api_ref}`

  - `GET /billing/status`

    - Auth required.
    - Returns `{subscription_status, current_period_end, month_free_limit, month_used, month_remaining}`

  - `POST /billing/webhooks/intasend`

    - **No auth** (provider callback)
    - Verify signature/secret per IntaSend docs
    - Update `PaymentTransaction` → `succeeded`/`failed`
    - On `succeeded`, call `subscription_manager.activate(user_id)`

- **Wire routes in `app.py`**

  ```python
  from routes.payments_routes import BillingCheckout, BillingStatus, IntaSendWebhook
  api.add_resource(BillingCheckout, "/billing/checkout")
  api.add_resource(BillingStatus, "/billing/status")
  api.add_resource(IntaSendWebhook, "/billing/webhooks/intasend")
  ```

### 5) Gate AI Generation (very important)

In `routes/ai_routes.py` (AIGenerateFlashcards.post):

- Before generating:

  - `if not usage_tracker.can_generate(user_id): return 402 Payment Required` (or 403 with `{code:'PAYWALL'}` body)

- After a **successful** generation (whether inserting or just returning preview):

  - Increment usage **only when a prompt is actually used**:

    - We recommend counting **on every POST** (both preview and direct insert), or **only on inserts** — pick one and be consistent.

> For MVP: count on **any generation request** (preview or insert). Free tier = 5 generation calls per month.

---

## 🗂️ Files to Add (Frontend)

> Base paths relative to `flashlearn-frontend/`

### 1) Billing API

- **`src/utils/billingApi.js`**

  - `createCheckout()` → `POST /billing/checkout` → `{ checkout_url }`
  - `getBillingStatus()` → `GET /billing/status` → `{ subscription_status, month_remaining, ... }`

### 2) Billing UI

- **NavBar**: add “Upgrade” button

  - `src/components/NavBar.jsx` → opens Billing modal/page.

- **Billing Modal/Page**

  - `src/components/Billing/BillingDialog.jsx` (or `BillingPage.jsx`)
  - Shows:

    - Plan: “Monthly — KES 100”
    - Status: `Active`/`Inactive`
    - Free prompts remaining this month
    - Button: “Subscribe (KES 100)” → calls `createCheckout()`; open returned `checkout_url` in new tab/window.

  - After redirect back, **poll** `/billing/status` to confirm activation.

### 3) AI UI Gate

- **AIGenerateModal.jsx**:

  - Fetch `/billing/status` on open (or use cached status in context)
  - Show: “You have **N free prompts** left this month”
  - If `month_remaining === 0` **and** `subscription_status!=='active'` → disable generate buttons; show “Upgrade to continue” with CTA opening Billing.

### 4) User Context (optional enhancement)

- **`src/components/context/UserContext.jsx`**:

  - Add lightweight billing state:

    - `billingStatus` object cached for the session
    - Expose `refreshBillingStatus()`

  - Minimizes repeated calls from AI modal, dashboard, etc.

---

## 🔌 Backend ↔ Frontend Contract

### `POST /billing/checkout` (auth required)

**Request:**

```json
{ "plan": "monthly" }
```

**Response:**

```json
{
  "checkout_url": "https://pay.intasend.com/...",
  "api_ref": "IS-XYZ-123",
  "amount": 100,
  "currency": "KES",
  "plan": "monthly"
}
```

### `GET /billing/status` (auth required)

**Response:**

```json
{
  "subscription_status": "active" | "inactive",
  "plan": "monthly" | null,
  "current_period_end": "2025-10-01T00:00:00Z",
  "month_key": "2025-09",
  "month_free_limit": 5,
  "month_used": 3,
  "month_remaining": 2
}
```

### `POST /billing/webhooks/intasend` (no auth)

- IntaSend sends event payload; backend verifies signature.
- On success:

  - Update `PaymentTransaction` → `succeeded`
  - Activate or extend `Subscription`

- Respond `200 OK`.

---

## 🔐 Security & Correctness Notes

- **Webhook verification**: Validate IntaSend signature/secret. Reject unknown origins.
- **Idempotency**: Use `api_ref`/`provider_ref` to ensure one-time activation per payment.
- **Amounts/Currency**: Validate `KES 100` server-side. Don’t trust client payload.
- **Ownership**: Never trust `user_id` from client; take from JWT.
- **CORS**: Allow `http://127.0.0.1:5173` _and_ `http://localhost:5173` in dev.
- **Monthly window**: Use server time (UTC) to compute `month_key = YYYY-MM`.

---

## 🧪 Testing Checklist

- [ ] New user sees **5 free prompts**; can generate until exhausted.
- [ ] After 5 prompts, AI modal blocks; “Upgrade” CTA visible.
- [ ] Checkout flow opens **IntaSend hosted page**, returns to app.
- [ ] On successful payment, `/billing/status` shows `active`, `current_period_end` \~30 days ahead.
- [ ] With active subscription, generation works beyond free cap.
- [ ] Webhook retries handled idempotently.
- [ ] No CORS errors; both `localhost` and `127.0.0.1` origins work.

---

## 🧩 Where Each File Lives (Quick Index)

**Backend**

```
flashlearn-backend/
├─ services/
│  ├─ intasend_client.py
│  ├─ subscription_manager.py
│  └─ usage_tracker.py
├─ routes/
│  ├─ payments_routes.py        # /billing/checkout, /billing/status, /billing/webhooks/intasend
│  └─ ai_routes.py              # gate generation with usage/subscription
├─ models.py                    # Subscription, PaymentTransaction, UsageLimits (if not split)
├─ config.py                    # env & billing config
└─ migrations/                  # add/align as needed
```

**Frontend**

```
flashlearn-frontend/
├─ src/utils/
│  └─ billingApi.js             # createCheckout, getBillingStatus
├─ src/components/Billing/
│  ├─ BillingDialog.jsx         # or BillingPage.jsx
├─ src/components/
│  ├─ NavBar.jsx                # "Upgrade" button opens BillingDialog
│  └─ DeckView/AIGenerateModal.jsx  # shows usage, gates when exhausted
└─ src/components/context/
   └─ UserContext.jsx           # (optional) cache billing status
```

---

## 🧠 Implementation Notes

- **Count usage where it matters.** For MVP, count a “prompt” on **every** AI generation request (`POST /ai/generate`) whether preview or insert. If you prefer, you can count only on inserts — just be consistent in UX copy (“You have 5 _generations_ left” vs “5 _inserts_ left”).
- **Reset is automatic**: Because usage is keyed by `YYYY-MM`, it naturally resets monthly. No cron required.
- **Grace period**: If you want, allow a **24h grace** after subscription expiry to avoid sudden cutoffs (nice-to-have).

---

## 📈 Pricing & Plans (Prototype)

- **Monthly plan:** KES 100
- **Free tier:** 5 prompts / month
- Future:

  - Tiered plans (e.g., 100 / 300 / 700 prompts)
  - One-time top-ups (prepaid prompts)
  - Team plans (shared quota)

---

### Done right, this gives you:

- Predictable freemium limits,
- A clean upgrade path via IntaSend,
- A minimal surface area (3 endpoints + 3 services),
- No state leakage or race conditions in AI flow.
