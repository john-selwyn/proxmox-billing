# Generated for durable VM100 power-operation tracking
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0005_order_provisioning_live_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="VPSPowerOperation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("idempotency_key", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("remote_operation_id", models.UUIDField(blank=True, editable=False, null=True, unique=True)),
                ("action", models.CharField(choices=[("start", "start"), ("shutdown", "shutdown"), ("reboot", "reboot")], max_length=8)),
                ("status", models.CharField(choices=[("pending", "pending"), ("running", "running"), ("succeeded", "succeeded"), ("failed", "failed"), ("unknown", "unknown")], default="pending", max_length=9)),
                ("result", models.CharField(blank=True, choices=[("executed", "executed"), ("noop", "noop")], max_length=8, null=True)),
                ("observed_state", models.CharField(choices=[("running", "running"), ("stopped", "stopped"), ("paused", "paused"), ("suspended", "suspended"), ("unknown", "unknown")], default="unknown", max_length=9)),
                ("observed_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=40)),
                ("sync_attempts", models.PositiveSmallIntegerField(default=0)),
                ("next_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="power_operations", to="billing.order")),
            ],
            options={
                "indexes": [models.Index(fields=["order", "status", "updated_at"], name="billing_power_work_idx")],
                "constraints": [
                    models.UniqueConstraint(condition=models.Q(("status__in", ["pending", "running", "unknown"])), fields=("order",), name="one_unresolved_billing_power_per_order"),
                    models.CheckConstraint(condition=models.Q(("action__in", ["start", "shutdown", "reboot"])), name="billing_power_valid_action"),
                    models.CheckConstraint(condition=models.Q(("status__in", ["pending", "running", "succeeded", "failed", "unknown"])), name="billing_power_valid_status"),
                ],
            },
        ),
    ]
