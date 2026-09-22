# Customer billing portal

This Django application stops at **Customer -> Order -> Invoice -> Pending Payment**.
It does not call Proxmox, a provisioning server, or a payment provider.

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
- `billing/services.py` is the boundary for billing operations. Future verified
  payment processing and authenticated provisioning integration remain separate.

## Remaining work and operational limits

No payments are collected and no services are provisioned. Later work must add a
real payment provider, verified server-side webhooks, and an authenticated API to
the existing provisioning service. Customer input must never mark invoices paid.

Production deployment remains outside this change. Review HTTPS, secure session
and CSRF cookies, allowed hosts, static-file serving, and authentication rate
limiting before exposing the portal publicly. The existing environment separation
and production connection settings are preserved. PostgreSQL concurrency was not
exercised locally; tests use SQLite. No production systems are accessed.

## Files changed for this implementation

Backend and configuration:
- `billing/apps.py`
- `billing/forms.py` (new)
- `billing/models.py`
- `billing/services.py` (new)
- `billing/signals.py` (new)
- `billing/tests.py`
- `billing/urls.py`
- `billing/views.py`
- `billing/migrations/0002_order_checkout_token.py` (new)
- `billing_project/settings.py`

Frontend:
- `billing/static/billing/portal.css` (new)
- `billing/templates/billing/base.html` (new)
- `billing/templates/billing/home.html`
- `billing/templates/billing/register.html`
- `billing/templates/billing/plans.html`
- `billing/templates/billing/select_plan.html`
- `billing/templates/billing/dashboard.html` (new)
- `billing/templates/billing/account.html` (new)
- `billing/templates/billing/review_order.html` (new)
- `billing/templates/billing/checkout_error.html` (new)
- `billing/templates/billing/orders.html` (new)
- `billing/templates/billing/order_table.html` (new)
- `billing/templates/billing/order_detail.html` (new)
- `billing/templates/billing/invoices.html` (new)
- `billing/templates/billing/invoice.html` (new)
- `billing/templates/billing/pagination.html` (new)
- `billing/templates/registration/login.html`

Documentation:
- `README.md` (new)

The local ignored `db.sqlite3` was migrated in place. No database file, virtual
environment, secret, or `.env` file belongs in the commit.
