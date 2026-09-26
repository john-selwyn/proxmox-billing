import uuid

from django.db import models
from django.contrib.auth.models import User


class VPSPlan(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    cpu = models.PositiveIntegerField(help_text="Number of vCPU cores")
    ram = models.PositiveIntegerField(help_text="RAM in GB")
    storage = models.PositiveIntegerField(help_text="Storage in GB")

    monthly_price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    yearly_price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Customer(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="customer_profile"
    )

    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=50, blank=True)
    company = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name


class Order(models.Model):

    checkout_token = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    # Billing-side request tracking, not a copy of the provisioning server's VPS model.
    provisioning_status = models.CharField(max_length=30, blank=True, editable=False)
    provisioning_vps_id = models.CharField(max_length=100, blank=True, editable=False)
    provisioning_vmid = models.CharField(max_length=100, blank=True, editable=False)
    provisioning_progress = models.PositiveSmallIntegerField(default=0, editable=False)
    provisioning_step = models.CharField(max_length=255, blank=True, editable=False)
    provisioning_ip_address = models.GenericIPAddressField(null=True, blank=True, editable=False)
    provisioning_error = models.CharField(max_length=200, blank=True, editable=False)
    provisioning_payload = models.JSONField(default=dict, blank=True, editable=False)
    provisioning_lease = models.UUIDField(null=True, blank=True, editable=False)
    provisioning_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    provisioning_checked_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PAID = "PAID", "Paid"
        PROVISIONING = "PROVISIONING", "Provisioning"
        ACTIVE = "ACTIVE", "Active"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    class BillingCycle(models.TextChoices):
        MONTHLY = "MONTHLY", "Monthly"
        YEARLY = "YEARLY", "Yearly"

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="orders"
    )

    plan = models.ForeignKey(
        VPSPlan,
        on_delete=models.PROTECT,
        related_name="orders"
    )

    billing_cycle = models.CharField(
        max_length=20,
        choices=BillingCycle.choices
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Order #{self.id}"


class Invoice(models.Model):

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PAID = "PAID", "Paid"
        VOID = "VOID", "Void"
        OVERDUE = "OVERDUE", "Overdue"

    order = models.OneToOneField(
        Order,
        on_delete=models.PROTECT,
        related_name="invoice"
    )

    invoice_number = models.CharField(
        max_length=50,
        unique=True
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )

    due_date = models.DateTimeField()

    paid_at = models.DateTimeField(
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.invoice_number


class XenditCheckout(models.Model):
    class Status(models.TextChoices):
        CREATING = "CREATING", "Creating checkout"
        ACTIVE = "ACTIVE", "Awaiting payment"
        UNKNOWN = "UNKNOWN", "Needs reconciliation"
        VERIFYING = "VERIFYING", "Verifying payment"
        COMPLETED = "COMPLETED", "Payment confirmed"
        EXPIRED = "EXPIRED", "Expired"
        CANCELED = "CANCELED", "Canceled"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="checkouts")
    provider = models.CharField(max_length=20, default="XENDIT", editable=False)
    reference_id = models.CharField(max_length=100, unique=True, editable=False)
    payment_session_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    payment_link_url = models.URLField(max_length=2048, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATING)
    verified_payment_id = models.CharField(max_length=100, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["order"], condition=models.Q(status__in=["CREATING", "ACTIVE", "UNKNOWN", "VERIFYING"]),
            name="one_open_xendit_checkout_per_order",
        )]


class Payment(models.Model):

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.PROTECT,
        related_name="payments"
    )

    provider = models.CharField(max_length=50)

    transaction_id = models.CharField(
        max_length=200,
        unique=True
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True
    )

    raw_response = models.JSONField(
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.transaction_id


class Subscription(models.Model):

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="subscriptions"
    )

    order = models.OneToOneField(
        Order,
        on_delete=models.PROTECT,
        related_name="subscription"
    )

    start_date = models.DateTimeField()
    next_billing_date = models.DateTimeField()

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Subscription #{self.id}"


class VPSPowerOperation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        RUNNING = "running", "running"
        SUCCEEDED = "succeeded", "succeeded"
        FAILED = "failed", "failed"
        UNKNOWN = "unknown", "unknown"

    class Result(models.TextChoices):
        EXECUTED = "executed", "executed"
        NOOP = "noop", "noop"

    class ObservedState(models.TextChoices):
        RUNNING = "running", "running"
        STOPPED = "stopped", "stopped"
        PAUSED = "paused", "paused"
        SUSPENDED = "suspended", "suspended"
        UNKNOWN = "unknown", "unknown"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="power_operations")
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    remote_operation_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    action = models.CharField(max_length=8, choices=[
        ("start", "start"), ("shutdown", "shutdown"), ("reboot", "reboot"),
    ])
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.PENDING)
    result = models.CharField(max_length=8, choices=Result.choices, null=True, blank=True)
    observed_state = models.CharField(
        max_length=9, choices=ObservedState.choices, default=ObservedState.UNKNOWN
    )
    observed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    sync_attempts = models.PositiveSmallIntegerField(default=0)
    next_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["order", "status", "updated_at"], name="billing_power_work_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(status__in=["pending", "running", "unknown"]),
                name="one_unresolved_billing_power_per_order",
            ),
            models.CheckConstraint(
                condition=models.Q(action__in=["start", "shutdown", "reboot"]),
                name="billing_power_valid_action",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["pending", "running", "succeeded", "failed", "unknown"]),
                name="billing_power_valid_status",
            ),
        ]

    @property
    def unresolved(self):
        return self.status in {self.Status.PENDING, self.Status.RUNNING, self.Status.UNKNOWN}
