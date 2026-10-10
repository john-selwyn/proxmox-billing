# Windows 11 checkout and RDP

Windows ordering is disabled by default (`WINDOWS_ORDERING_ENABLED=False`).
Plans must have at least 2 vCPU, 4 GB RAM, and 64 GB storage. Windows orders use
the provisioner's generated Admin password rather than an SSH public key.

The provisioner must return `rdp_access` with exactly `host`, `port`, and
`username` (`Admin`) in provisioning/runtime responses. A missing field preserves
the stored endpoint; explicit null clears it. Passwords belong only in the
authenticated `/api/internal/v1/vps/<order>/windows-credentials/` response:
`{"billing_order_id": <order>, "rdp_access": {"host": ..., "port": ..., "username": "Admin", "password": ...}}`.

The customer reveal action requires login, CSRF-protected POST, order ownership,
an active Windows order, and verified payment evidence. Responses are uncached.
The billing database never stores the password. The browser clears the displayed
password after 60 seconds or leaving the page. A password changed inside Windows
does not update the original provisioning password.

## Deploy on the billing server

These steps apply to `/opt/billing` on `feature/shared-ip-ssh` at `effe772` with
the three previously patched Windows files and migration 0010. If other tracked
files are modified, preserve and review them before switching branches.

Preserve the known live edits in a named stash, including the untracked migration:

```sh
cd /opt/billing
git status --short
git stash push -u -m 'live Windows checkout edits before tested RDP branch' -- billing/forms.py billing/models.py billing/views.py billing/migrations/0010_alter_order_operating_system.py
git stash list -1
git fetch origin codex/windows-rdp
git switch --track origin/codex/windows-rdp
./venv/bin/python manage.py migrate billing
./venv/bin/python manage.py check
```

Keep the stash and existing `.before-windows` copies. The branch includes those
Windows changes, so do not pop the stash over it. Migration 0010 has already
been applied on this server; Django should apply only the new RDP field migration.

Restart the billing application using its existing service or deployment process.
Do not enable Windows ordering until the provisioner template mapping, credential
endpoint, RDP relay, and router forwarding are deployed and verified. Use a paid
test order to check the resulting Admin password and public Remote Desktop
connection. Confirm an existing Linux order still connects through SSH.

Once those checks pass, set `WINDOWS_ORDERING_ENABLED=True` in the server's `.env`
and restart the billing application. Never commit `.env` or encryption keys.

## Validation

The billing suite passes on an isolated SQLite test database. Run PostgreSQL tests
against a separate test database in staging; local SQLite checks do not validate
PostgreSQL locking or a live Proxmox/EdgeRouter deployment.


## Progress reaches 100% but RDP/power controls require refresh

Problem reported 2026-10-10: a new Windows order completed and worked, but the
open management page stayed at Preparing Remote Desktop until manually refreshed.
Confirmed code cause: customer provisioning-status JSON omitted rdp_host/rdp_port;
the shared poller checked only ssh_host before announcing access/reloading. Power
controls were conditional on ACTIVE at initial server render.

Resolution: include the stored RDP endpoint in owner-scoped status JSON (no password),
select SSH/RDP readiness according to OS, and reload when a pending page first
observes ACTIVE. The active page keeps polling delayed access and reloads when its
endpoint appears. A fully rendered ready page skips provisioning polling. Failure
stops polling; transient errors retry. Existing runtime-state polling is retained.

Validation: 145 Django tests passed with in-memory SQLite; migration consistency
and system checks passed. Six JavaScript tests execute the actual inline poller
with browser/network/timer mocks, covering activation, delayed RDP, Linux SSH,
ready-page stability, failure and fetch retry:

```sh
node --test billing/test_provisioning_poll.cjs
```

Deployment on billing-server /opt/billing, after confirming a clean checkout on
codex/windows-rdp: fast-forward the branch, run manage.py check, restart
billing.service, verify active. No schema migration or new static asset required.
Record the pre-update commit for rollback. Reverse only the polling-fix commit if
necessary; do not reset unrelated changes or remove stored endpoints. Production
browser verification remains pending until deployed. No new payment is needed:
open an existing active Windows page to verify endpoints, and observe the next
normal provisioning run for the transition behavior.


## Clear remote-access waiting stages - 2026-10-10

The user confirmed a later Windows order worked with the background retry worker,
but requested clearer waiting feedback before remote details appear. The UI now
labels the percent as VPS creation, separately shows VPS running and Remote
Desktop/SSH preparation, and displays elapsed waiting time plus a countdown to
the next status check. It explicitly describes automatic retries and updates.
The countdown is for the next poll, not a promise of connection readiness.

The per-order waiting start is retained only as a timestamp in sessionStorage
(no credentials), survives refresh in that tab, and clears on ready/failure.
Storage failure falls back to an in-memory timestamp. After five minutes the
message continues automatic checks and gives the order reference for support.
Animation respects reduced-motion preferences. Timers stop on ready, failure
or page exit; ready-page polling and existing power controls retain their behavior.

Verification: all 146 Django SQLite tests and nine JavaScript poller tests passed;
system and migration checks passed. Production visual verification pending.
This is presentation only: no change to worker retries, port reservations, or
relay configuration. The source of earlier RDP delay remains distinct from UI.

Deployment on billing-server:

```sh
cd /opt/billing
git pull --ff-only origin codex/windows-rdp
./venv/bin/python manage.py check
./venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart billing.service
systemctl is-active billing.service
```

collectstatic is required for the added styles; a stylesheet version query avoids
old browser cache. No database migration. Refresh an already-open page once to
load updated inline code. For rollback, revert only this UI commit after review,
collectstatic again, restart and check the service; preserve .env and endpoints.
No new payment is required for deployment; observe the next normal provisioning
run for pending-to-ready feedback.
