# Public SSH endpoint rollout

Use `feature/shared-ip-ssh` with the matching branch in `john-selwyn/VPS-server`.
The full deployment guide is [in the provisioner repository](https://github.com/john-selwyn/VPS-server/blob/feature/shared-ip-ssh/deploy/PUBLIC_SSH.md).

This billing branch preserves the deployed `ssh_host` and `ssh_port` fields and
the public-host/port command on the customer VPS page. It also accepts the
provisioner's optional `ssh_access` status/runtime field. Missing fields from
older provisioners retain saved endpoints; an explicit null means access is
still being prepared. A private guest IP remains visible as network information
and is not copied as the public SSH command.

Preserve and inspect the live working tree before merging. Migration
`0009_order_ssh_host_order_ssh_port` is already applied on the billing server;
the checked-in migration adds those same fields. Do not generate a second
migration. Back up the deployed migration before resolving any add/add conflict.
Keep order #17's saved `proxmoxportal.dyndns.org:22015` endpoint.

After merging, in `/opt/billing`:

```sh
venv/bin/python manage.py migrate
venv/bin/python manage.py check
sudo systemctl restart billing.service
systemctl is-active billing.service
```

Deploy billing before enabling `PUBLIC_SSH_ENABLED` in the provisioner.
Adopt the existing port reservation there before automatic allocation starts.
Confirm order #17 still displays its public SSH command, then test a new order
after the one-time relay-key, worker and router-range setup.
