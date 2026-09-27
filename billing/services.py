"""Order creation; payment recording and provisioning live in separate services."""
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from .models import Invoice, Order, VPSPlan


def plan_amount(plan, cycle):
    if cycle == Order.BillingCycle.MONTHLY:
        amount = plan.monthly_price
    elif cycle == Order.BillingCycle.YEARLY:
        amount = plan.yearly_price
    else:
        raise ValidationError('Choose a valid billing cycle.')
    if amount < 0:
        raise ValidationError('This plan is not available for ordering.')
    return amount


@transaction.atomic
def confirm_order(*, customer, plan_id, cycle, checkout_token, reviewed_amount,
                  operating_system=Order.OperatingSystem.UBUNTU_26_04,
                  ssh_username="", ssh_public_key=""):
    if operating_system not in Order.OperatingSystem.values:
        raise ValidationError("Choose a supported operating system.")
    existing = Order.objects.filter(checkout_token=checkout_token, customer=customer).first()
    if existing:
        if existing.operating_system != operating_system:
            raise ValidationError("This checkout operating system does not match the existing order.")
        if ssh_username and (
            existing.ssh_username != ssh_username or existing.ssh_public_key != ssh_public_key
        ):
            raise ValidationError("This checkout SSH configuration does not match the existing order.")
        return existing
    # Serialize purchases on PostgreSQL; uniqueness also protects token retries.
    plan = VPSPlan.objects.select_for_update().get(pk=plan_id, is_active=True)
    amount = plan_amount(plan, cycle)
    if str(amount) != reviewed_amount:
        raise ValidationError('The plan price has changed. Please review your order again.')
    order, created = Order.objects.get_or_create(
        checkout_token=checkout_token,
        defaults={'customer': customer, 'plan': plan, 'billing_cycle': cycle,
                  'amount': amount, 'status': Order.Status.PENDING,
                  'operating_system': operating_system,
                  'ssh_username': ssh_username, 'ssh_public_key': ssh_public_key},
    )
    if order.customer_id != customer.pk:
        raise ValidationError('This checkout belongs to another account.')
    if created:
        Invoice.objects.create(
            order=order, invoice_number=f'INV-{order.pk:08d}', amount=amount,
            status=Invoice.Status.PENDING, due_date=order.created_at + timedelta(days=1),
        )
    return order
