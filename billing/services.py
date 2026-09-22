"""Local billing operations; payment and provisioning integrations are deferred."""
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
def confirm_order(*, customer, plan_id, cycle, checkout_token, reviewed_amount):
    existing = Order.objects.filter(checkout_token=checkout_token, customer=customer).first()
    if existing:
        return existing
    # Serialize purchases on PostgreSQL; uniqueness also protects token retries.
    plan = VPSPlan.objects.select_for_update().get(pk=plan_id, is_active=True)
    amount = plan_amount(plan, cycle)
    if str(amount) != reviewed_amount:
        raise ValidationError('The plan price has changed. Please review your order again.')
    order, created = Order.objects.get_or_create(
        checkout_token=checkout_token,
        defaults={'customer': customer, 'plan': plan, 'billing_cycle': cycle,
                  'amount': amount, 'status': Order.Status.PENDING},
    )
    if order.customer_id != customer.pk:
        raise ValidationError('This checkout belongs to another account.')
    if created:
        Invoice.objects.create(
            order=order, invoice_number=f'INV-{order.pk:08d}', amount=amount,
            status=Invoice.Status.PENDING, due_date=order.created_at + timedelta(days=1),
        )
    return order
