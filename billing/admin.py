from django.contrib import admin

from .models import (
    VPSPlan,
    Customer,
    Order,
    Invoice,
    Payment,
    Subscription,
)


@admin.register(VPSPlan)
class VPSPlanAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "cpu",
        "ram",
        "storage",
        "monthly_price",
        "yearly_price",
        "is_active",
    )
    list_filter = ("is_active",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "company",
        "phone",
        "user",
        "created_at",
    )


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "customer",
        "plan",
        "billing_cycle",
        "amount",
        "status",
        "created_at",
    )
    list_filter = ("status", "billing_cycle")
    readonly_fields = (
        "provisioning_status", "provisioning_vps_id", "provisioning_vmid",
        "provisioning_error", "provisioning_started_at", "provisioning_checked_at",
    )


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "order",
        "amount",
        "status",
        "due_date",
        "paid_at",
    )
    list_filter = ("status",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_id",
        "invoice",
        "provider",
        "amount",
        "status",
        "paid_at",
    )
    list_filter = ("provider", "status")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "customer",
        "order",
        "start_date",
        "next_billing_date",
        "status",
    )
    list_filter = ("status",)
