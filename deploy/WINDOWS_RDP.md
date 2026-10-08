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
