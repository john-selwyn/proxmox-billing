# Customer billing portal

This Django application manages customers, orders, invoices, and billing-side
provisioning integration with the existing VM100 API. VM100 alone owns Proxmox.
There is no payment gateway or public payment-confirmation endpoint yet.

## Local development (PowerShell)

Use the existing virtual environment and an untracked `.env` with your own
`SECRET_KEY`. For local development, set `DEBUG=True`; this selects SQLite.
The committed `.env.example` remains the production configuration template.

```powershell
$env:DEBUG = 'True'
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py runserver
```

Create and activate real VPSPlan records through Django admin. Prices are PHP,
matching the existing application. No sample plans or customer records are seeded.

```powershell
$env:DEBUG = 'True'
.\venv\Scripts\python.exe manage.py check
.\venv\Scripts\python.exe manage.py test
.\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
```

These commands select local SQLite explicitly, even if `.env` contains production
configuration. Never use production database credentials for tests. The Django
test runner uses a separate temporary test database.

## Billing behavior

- Registration saves the user and customer together. A user-creation signal
  creates profiles for users created through other normal Django entry points.
  Legacy users without profiles are repaired when opening dashboard/account.
- All customer records are scoped to the authenticated user. Account editing
  accepts only the customer's name, company, and phone.
- Plan selection only displays a review; confirmation is a CSRF-protected POST.
- Signed reviews expire after one hour and are bound to the user, plan, cycle,
  and reviewed price. The confirmation service reloads the active database plan
  and calculates the amount again. Price changes require a fresh review.
- Orders and invoices are created in one transaction. New statuses are PENDING.
  Invoice numbers use the order ID; invoices are due one day after order creation.
- The unique nullable order checkout token prevents duplicate records when the
  same confirmation is submitted again. A separate review represents a new order.
- Migration `0002_order_checkout_token` adds only this nullable unique column;
  existing orders and migrations are retained.
- `billing/services.py` creates orders. `billing/payments.py` records already
  verified payments. `billing/provisioning.py` coordinates paid-order dispatch
  and status synchronization. `billing/provisioning_api.py` isolates the HTTP
  protocol and VM100 response parsing.

## Provisioning configuration

Set these only in your untracked environment; never commit the real shared secret:

```dotenv
PROVISIONING_API_URL=
PROVISIONING_ALLOW_HTTP=False
ALLOW_TEST_PAYMENT=False
BILLING_API_SECRET=
PROVISIONING_DEFAULT_OS=Ubuntu 26.04
```

`PROVISIONING_API_URL` is the origin only, e.g. `https://provisioner.example.com`
(no `/api/` path, credentials, query, or fragment). The service appends:

- `POST /api/internal/provision/`
- `GET /api/internal/provision/<billing_order_id>/status/`

The client sends bearer authentication and JSON, uses 5-second connection and
20-second read timeouts, disables redirects, and retains TLS certificate checks.
It ignores environment proxies/netrc to keep internal credentials on the intended
connection. HTTP is allowed only when `DEBUG=True` or the explicit environment
setting `PROVISIONING_ALLOW_HTTP=True` is enabled. The setting defaults to False;
only `true` (case-insensitive, with surrounding whitespace ignored) enables it.
Other values leave it disabled. Use this opt-in only for a temporary trusted
internal LAN/testing network; production should use HTTPS and keep it False.
Plain HTTP does not encrypt the bearer secret. This opt-in does not enable DEBUG,
change database selection, bypass authentication, or enable test payments.
Test payments require the separate `ALLOW_TEST_PAYMENT` switch. All other API
validation and TLS certificate checks are unchanged.
Timeouts are network inactivity limits, not a total job-completion deadline.

CPU, RAM (GB), storage (GB), plan name, cycle, and customer/order-derived VPS name
come from database records. The OS comes from the server setting above. The
payload is saved on the first dispatch and reused for every retry, even if the
plan is subsequently edited. No browser resource values are used.

The adapter handles the confirmed VM100 top-level response fields `status`,
`vps_id`, `vmid`, and optional `billing_order_id`/`success`. Status is matched
case-insensitively. `Running`/`ready` -> ACTIVE, `Provisioning`/in-progress ->
PROVISIONING, and `Failed` -> FAILED. Unknown or malformed responses are errors,
not success. If provided, the billing order ID must match the requested order.
Previously stored VPS/VM IDs cannot be silently replaced with different IDs.

Raw errors, response bodies, IP addresses and current-step descriptions are not
stored or shown. Instead, safe categories such as TIMEOUT, CONNECTION_FAILED,
HTTP_503, INVALID_JSON, REMOTE_FAILED or REMOTE_ID_MISMATCH are recorded. Correlate
with VM100 by billing order ID for detailed infrastructure diagnostics. Customer
pages show only payment/provisioning state and a support reference.

## Payment boundary and lifecycle

A future real gateway adapter must verify the server-side webhook signature,
settled payment, PHP currency, amount, and order mapping **before** calling:

```python
from billing.payments import record_verified_payment
record_verified_payment(
    order_id=verified_order_id,
    provider=verified_provider,
    transaction_id=verified_transaction_id,
    amount=verified_amount,
)
```

This is an internal trusted-code interface, not webhook verification itself.
Nothing in a browser request can invoke it. The function compares the amount to
both order and invoice and rejects reused transaction IDs or an additional
successful payment on an already-paid invoice.

Payment, invoice PAID state and order PAID state are committed atomically.
`transaction.on_commit` dispatches afterward, so no database transaction spans
HTTP. Provisioning requires a PAID invoice and matching SUCCESS payment with paid
timestamps, not merely `Order.status = PAID`.

Normal transitions:

```text
PENDING -> PAID -> PROVISIONING -> ACTIVE
                             -> FAILED
FAILED -- explicit paid-order retry --> PROVISIONING
```

On ACTIVE, one subscription is created/reused, starting at activation, with the
next bill one calendar month/year later (clamped for month-end/leap days).
Existing subscription dates are preserved. Invoice/payment stay PAID/SUCCESS
when provisioning fails: a VM error is not a payment failure or refund.

A short transaction claims a five-minute request lease. Concurrent calls skip a
live lease; expired leases are recoverable. Lease IDs prevent a late worker from
overwriting a newer result. Successful dispatch is not automatically repeated.
An HTTP timeout/connection failure leaves PROVISIONING with a safe error because
VM100 might already have accepted the request. Status GET should reconcile that
outcome first. Explicit retries reuse the same order ID and payload, relying on
VM100's confirmed idempotency for the unavoidable ambiguous-network case.

A process exit between payment commit and dispatch leaves the order durably
PAID. Recover with `sync_provisioning ORDER_ID --retry`. A process exit during
HTTP requires waiting five minutes for its lease before retrying. This version
uses management commands for dispatch recovery and polling; it does not install
a background worker or scheduler. Operators must run/schedule status sync for
outstanding orders. Customer page loads do not send provisioning requests.
Status checks have a ten-second cooldown; ACTIVE is terminal for this initial
provisioning tracker (ongoing VPS monitoring is VM100's responsibility).

## TEST-ONLY payment

`ALLOW_TEST_PAYMENT` is an operator-only staging/testing switch and should
normally remain **False** (the default). Only `true`, case-insensitive with
surrounding whitespace ignored, enables it; all other values disable it.
It is independent of DEBUG: an operator can explicitly enable it on VM200 with
`DEBUG=False`, without changing the database selection or enabling debug pages.
Enabling it can create a **real VPS through VM100**. It must never be used as a
real payment mechanism or as evidence that money was received.

The management command remains the only simulated-payment entry point and still
requires `--confirm`. There is no test-payment URL or customer button. Both the
command and underlying payment service check ALLOW_TEST_PAYMENT at runtime.
DEBUG=True alone does not permit test payments. Records are tagged `TEST_ONLY`
with deterministic `test-order-<id>` transaction IDs. When ALLOW_TEST_PAYMENT is
False, those payments cannot authorize provisioning or status synchronization,
even if created while the switch was enabled. Normal verified payments are
unaffected. Disable the switch again after completing the staging test; doing so
does not delete test records or undo any VPS already created.

## First end-to-end test (manual, not executed during implementation)

This test can create a real VPS on VM100. Run it only when you intentionally want
to exercise your existing provisioner, from a separate development billing
instance and with a disposable plan/order. No SSH or Proxmox steps are needed.

1. In the development instance's untracked `.env`, set a local SECRET_KEY,
   `DEBUG=True` for local SQLite, `ALLOW_TEST_PAYMENT=True`, the VM100 origin
   in `PROVISIONING_API_URL`, the matching secret
   in `BILLING_API_SECRET`, and `PROVISIONING_DEFAULT_OS=Ubuntu 26.04`. VM100's
   supplied address is `220.100.130.190`; use its actual reachable scheme/port
   and valid TLS hostname (or a trusted tunnel). Do not paste the secret into
   Git, screenshots, or logs. Ensure VM100 accepts calls from this instance.
2. In PowerShell at the repository root, install the dependency and migrate the
   local database, then start the portal:

   ```powershell
   $env:DEBUG = 'True'
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   .\venv\Scripts\python.exe manage.py migrate
   .\venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
   ```

3. Using an existing local admin (or `manage.py createsuperuser`), create/activate
   an appropriate VPSPlan. Register/sign in as a customer at
   `http://127.0.0.1:8000/`, select a plan/cycle, review and confirm the order.
   Verify the invoice and order are PENDING. Note the numeric order ID.
4. In a second terminal with the same development environment, substitute that
   ID below. The command deliberately acknowledges that provisioning can occur:

   ```powershell
   $env:DEBUG = 'True' # Local SQLite only; keep DEBUG=False on VM200.
   $env:ALLOW_TEST_PAYMENT = 'True'
   .\venv\Scripts\python.exe manage.py confirm_test_payment ORDER_ID --confirm
   ```

   Expect one TEST_ONLY SUCCESS payment, a PAID invoice and a PROVISIONING order
   (or ACTIVE if VM100 immediately reports Running). Repeating this payment
   command must not add another payment/invoice/subscription or dispatch again.
5. Wait at least ten seconds, then synchronize. Repeat as needed while VM100 is
   provisioning, allowing at least ten seconds between checks:

   ```powershell
   .\venv\Scripts\python.exe manage.py sync_provisioning ORDER_ID
   ```

   Reload the order page. Running should become ACTIVE with one subscription;
   Failed should show a safe customer message while payment remains recorded.
6. For a timeout, reconcile with the GET command first. If VM100 reports no order,
   or an operator has resolved a failed provisioning attempt, explicitly retry:

   ```powershell
   .\venv\Scripts\python.exe manage.py sync_provisioning ORDER_ID --retry
   ```

   Keep ALLOW_TEST_PAYMENT=True in this testing environment until test-payment
   provisioning/status checks are complete, then restore it to False.

   It sends the same order ID and frozen payload. A live request lease may defer
   the call for up to five minutes. Check the safe error category in Django admin
   or command output; consult VM100's own logs using the order ID for details.

## Validation and limits

Latest local validation: **63 tests passed**; `manage.py check` reported no
issues; `makemigrations --check --dry-run` reported no changes; `git diff --check`
passed. Migration `0003_order_provisioning_tracking` was applied to the local
SQLite database only. No files were committed or pushed.

Automated tests mock all VM100 HTTP calls and also block the real Requests
transport in the integration suite. They use a temporary SQLite database.
The implementation does not contact VM100, VM200, production PostgreSQL or
Proxmox; no VPS was created. PostgreSQL concurrency still needs staging validation.
A real payment gateway and its verified webhook adapter remain future work.
Production HTTPS/cookies, host configuration, static serving, authentication rate
limiting, and an operator-run polling/recovery schedule remain deployment tasks.

## Files changed for provisioning integration

- `.env.example`
- `requirements.txt`
- `README.md`
- `billing_project/settings.py`
- `billing/admin.py`
- `billing/models.py`
- `billing/services.py` (module description only)
- `billing/payments.py` (new)
- `billing/provisioning.py` (new)
- `billing/provisioning_api.py` (new)
- `billing/test_provisioning.py` (new)
- `billing/migrations/0003_order_provisioning_tracking.py` (new)
- `billing/management/__init__.py` (new)
- `billing/management/commands/__init__.py` (new)
- `billing/management/commands/confirm_test_payment.py` (new)
- `billing/management/commands/sync_provisioning.py` (new)
- `billing/templates/billing/order_detail.html`
- `billing/templates/billing/invoice.html`
- `billing/templates/billing/provisioning_status.html` (new)
