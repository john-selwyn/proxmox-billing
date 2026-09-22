"""Server-side payment recording. This module does NOT verify provider webhooks.

Provider adapters must authenticate their webhooks, verify settlement,
amount/currency and order identity BEFORE calling record_verified_payment.
There is intentionally no browser-accessible payment confirmation route.
"""
from decimal import Decimal, InvalidOperation
from functools import partial

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Invoice, Order, Payment
from .provisioning import TEST_PROVIDER, request_vps_provisioning


@transaction.atomic
def record_verified_payment(*, order_id, provider, transaction_id, amount):
    if provider == TEST_PROVIDER and not settings.ALLOW_TEST_PAYMENT:
        raise ValidationError('Test payment confirmation is disabled.')
    if not provider or len(provider) > 50 or not transaction_id or len(transaction_id) > 200:
        raise ValidationError('Valid provider and transaction identifiers are required.')
    order = Order.objects.select_for_update().get(pk=order_id)
    invoice = Invoice.objects.select_for_update().get(order=order)
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValidationError('Payment amount does not match the invoice.') from None
    if not amount.is_finite() or amount != order.amount or amount != invoice.amount:
        raise ValidationError('Payment amount does not match the invoice.')
    if order.status == Order.Status.CANCELLED or invoice.status == Invoice.Status.VOID:
        raise ValidationError('This order cannot accept payment.')
    paid = invoice.payments.filter(status=Payment.Status.SUCCESS).first()
    if paid and (paid.transaction_id != transaction_id or paid.provider != provider):
        raise ValidationError('This invoice already has a successful payment.')
    now = timezone.now()
    payment, created = Payment.objects.get_or_create(transaction_id=transaction_id, defaults={
        'invoice': invoice, 'provider': provider, 'amount': amount,
        'status': Payment.Status.SUCCESS, 'paid_at': now,
    })
    if not created and (payment.invoice_id != invoice.pk or payment.provider != provider or
                        payment.amount != amount or payment.status != Payment.Status.SUCCESS or not payment.paid_at):
        raise ValidationError('The payment reference cannot be reused.')
    invoice.status = Invoice.Status.PAID
    invoice.paid_at = invoice.paid_at or payment.paid_at
    invoice.save(update_fields=['status', 'paid_at'])
    if order.status == Order.Status.PENDING:
        order.status = Order.Status.PAID
        order.save(update_fields=['status', 'updated_at'])
    # The order remains durably PAID if the process exits before this callback.
    # The operator retry command can recover it; no HTTP occurs inside atomic().
    transaction.on_commit(partial(request_vps_provisioning, order))
    return payment


def confirm_test_payment(order_id):
    """TEST ONLY. Operator command; guard is checked even for direct Python calls."""
    if not settings.ALLOW_TEST_PAYMENT:
        raise ValidationError('Test payment confirmation is disabled.')
    order = Order.objects.get(pk=order_id)
    return record_verified_payment(order_id=order.pk, provider=TEST_PROVIDER,
        transaction_id=f'test-order-{order.pk}', amount=order.amount)
