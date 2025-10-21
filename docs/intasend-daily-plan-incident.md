# IntaSend Daily Plan Incident Postmortem

## Context

On 20–21 Oct 2025 we saw repeated reports that the “daily” KES 10 plan was charging successfully on IntaSend, yet the FlashLearn dashboard continued to show the `free` plan. Transaction logs confirmed that payments were created and marked `succeeded`, but the user never gained premium access.

This document captures what went wrong, how we diagnosed it, and the code changes that fixed the issue.

---

## Symptoms Observed

- `/billing/status` kept returning `inactive` even after the customer completed checkout.
- Server logs spammed `Invoice with specified id does not exist` from the IntaSend status API, followed by our own `[payment_utils] Payment still processing after 2 attempts`.
- The `payment_transactions` table showed `status = succeeded` with a valid `api_ref`, but:
  - `provider_ref` (invoice id / receipt) remained empty.
  - The customer’s `subscriptions` row never moved past an expired window ending `2025-10-19`.
- Re-running `/billing/verify` simply echoed `payment_processing`.

---

## Root Causes

### 1. IntaSend Checkout Without Immediate Invoice IDs
IntaSend’s Express Checkout sometimes defers invoice creation until their backend finalises the payment. During that window the status API answers with `Invoice with specified id does not exist`. Our wrapper assumed an invoice id would be returned immediately and treated the absence as a hard failure.

### 2. Missing Publishable Key For REST Fallback
Whenever the SDK was unavailable we fell back to raw HTTP calls, but we only sent the Bearer secret. IntaSend’s REST API requires the `INTASEND_PUBLIC_API_KEY` header (and sometimes the value in the JSON payload) – without it requests returned 401/404, preventing invoice resolution.

### 3. Activation Guardrail Crash
`activate_subscription_for_transaction` tried to extract M-Pesa receipts even when the response structure was `{}`. That triggered `AttributeError: 'NoneType' object has no attribute 'get'`, rolling back the activation and leaving the subscription untouched.

### 4. No Recovery Once a Transaction Was Marked Succeeded
`BillingStatus` trusted `is_active` blindly. If activation failed once (for any reason), subsequent status checks never retried the process, so the user stayed stuck on the free tier even though their transaction was marked `succeeded`.

---

## Fixes Implemented

### IntaSend Client Hardening (`services/intasend_client.py`)
- Always include `INTASEND_PUBLIC_API_KEY`, `Accept`, and `User-Agent` in REST requests.
- Inject the publishable key into the HTTP body when creating a checkout.
- When only a `checkout_id` is available, post to `/api/v1/payment/status/` with the public key to discover the invoice id instead of calling undocumented endpoints.
- Normalise error handling so “invoice not yet created” is treated as a pending state rather than an exception.

### Resilient Subscription Activation
- Sanitised the receipt extractor to cope with missing `invoice` blocks (`services/payment_utils.py`).
- In `/billing/status` we now run a “backstop” activation: if we find a `succeeded` transaction but `is_active` is false, we re-run `activate_subscription_for_transaction`. That converts stranded users automatically and logs the event (`routes/payments_routes.py`).

### Data Validation
- Manually invoked the activation helper for the previously stranded transaction (id 29), confirming it now extends the subscription to `2025-10-21`.
- Verified that new checkout attempts succeed without crashing even when the invoice id is still pending.

---

## Verification Steps

1. Restart backend (or reload gunicorn) so the patched client is in memory.
2. Make a KES 10 payment through the app.
3. Observe logs:
   - Initial status calls may still report “invoice not yet created” for a few seconds (expected).
   - Once IntaSend finalises the invoice, `/billing/status` should log the backstop activation and the dashboard should flip to premium.
4. Confirm database state:
   ```sql
   SELECT status, provider_ref, completed_at FROM payment_transactions WHERE id = <latest_tx>;
   SELECT start_date, end_date, status FROM subscriptions WHERE user_id = <user>;
   ```

---

## Recommended Follow-Up

1. **Configure IntaSend Webhooks** to point at a publicly reachable `/webhooks/intasend` endpoint so successful payments activate instantly instead of relying on client polling.
2. **Add automated smoke tests** that simulate a `succeeded` transaction with no invoice id to ensure `BillingStatus` backstop remains in place.
3. **Monitor IntaSend API changes** – if they add an official “checkout details” endpoint, migrate the resolution logic away from the status POST workaround.
4. **Instrument more telemetry** (e.g. count of backstop activations) to detect future regressions early.

---

## Useful References

- Commit diff touching the critical fixes (run `git show HEAD -- routes/payments_routes.py services/payment_utils.py services/intasend_client.py`).
- IntaSend Express Checkout docs (requires login): <https://payment.intasend.com/>.
- FlashLearn billing endpoints: `routes/payments_routes.py`
- Subscription manager: `services/subscription_manager.py`

