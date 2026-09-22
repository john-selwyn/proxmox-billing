"""Checkout bookkeeping. Network calls stay outside database transactions."""
import uuid
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import xendit
from .models import Invoice, Order, XenditCheckout
from .payments import record_verified_payment

OPEN_STATES = ('CREATING', 'ACTIVE', 'UNKNOWN', 'VERIFYING')


def _payable(order, invoice, checkout=None):
    if (order.status != Order.Status.PENDING or invoice.status not in ('PENDING', 'OVERDUE') or
            order.amount != invoice.amount or order.amount <= 0 or
            (checkout and checkout.amount != order.amount)):
        raise xendit.XenditError('ORDER_NOT_PAYABLE')


def _session_state(checkout_id, data, *, session_id):
    """Persist only validated checkout identifiers, URL, and lifecycle state."""
    snapshot = XenditCheckout.objects.get(pk=checkout_id)
    xendit.verify_session(data, snapshot, session_id=session_id)
    link = xendit.checkout_url(data.get('payment_link_url')) if data['status'] == 'ACTIVE' else ''
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=snapshot.order_id)
        invoice = Invoice.objects.get(order=order)
        checkout = XenditCheckout.objects.select_for_update().get(pk=checkout_id)
        if checkout.status == 'COMPLETED':
            return checkout
        # Do not revive an expired checkout from a stale ACTIVE response.
        if checkout.status in ('EXPIRED', 'CANCELED') and data['status'] == 'ACTIVE':
            return checkout
        if data['status'] == 'ACTIVE':
            _payable(order, invoice, checkout)
        # Never replace an already-associated remote ID.
        if checkout.payment_session_id and checkout.payment_session_id != session_id:
            raise xendit.XenditError('SESSION_MISMATCH')
        if checkout.amount != order.amount or checkout.amount != invoice.amount:
            raise xendit.XenditError('AMOUNT_MISMATCH')
        checkout.payment_session_id = session_id
        checkout.payment_link_url = link
        checkout.status = 'VERIFYING' if data['status'] == 'COMPLETED' else data['status']
        checkout.error_code = ''
        checkout.save(update_fields=['payment_session_id', 'payment_link_url', 'status', 'error_code', 'updated_at'])
    return checkout


def start_checkout(order_id):
    if not xendit.configured():
        raise xendit.XenditError('NOT_CONFIGURED', retryable=True)
    # A conditional unique constraint is the second line of duplicate protection.
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        invoice = Invoice.objects.get(order=order)
        _payable(order, invoice)
        checkout = order.checkouts.filter(status__in=OPEN_STATES).first()
        created = checkout is None
        if created:
            checkout = XenditCheckout.objects.create(order=order, amount=invoice.amount,
                reference_id=f'order-{order.pk}-{uuid.uuid4().hex}')
        else:
            _payable(order, invoice, checkout)
    if not created:
        if checkout.status != 'ACTIVE':
            raise xendit.XenditError('CHECKOUT_PENDING', retryable=True)
        data = xendit.retrieve_session(checkout.payment_session_id)
        checkout = _session_state(checkout.pk, data, session_id=checkout.payment_session_id)
        if checkout.status == 'ACTIVE':
            return checkout
        if checkout.status in ('EXPIRED', 'CANCELED'):
            return start_checkout(order_id)
        raise xendit.XenditError('CHECKOUT_PENDING', retryable=True)
    try:
        data = xendit.create_session(checkout)
        session_id = xendit.identifier(data.get('payment_session_id'))
        return _session_state(checkout.pk, data, session_id=session_id)
    except xendit.XenditError as exc:
        # A failed response can still mean a remote checkout exists. Never
        # automatically POST again: an operator must reconcile this reference.
        XenditCheckout.objects.filter(pk=checkout.pk, status='CREATING').update(
            status='UNKNOWN', error_code=exc.code, updated_at=timezone.now())
        raise


def reconcile_checkout(reference_id, payment_session_id):
    """Operator-only recovery for an ambiguous creation response; never marks paid."""
    checkout = XenditCheckout.objects.get(reference_id=reference_id)
    session_id = xendit.identifier(payment_session_id)
    data = xendit.retrieve_session(session_id)
    return _session_state(checkout.pk, data, session_id=session_id)


def process_event(event, data):
    if not isinstance(data, dict):
        raise xendit.XenditError('INVALID_EVENT')
    session_id = xendit.identifier(data.get('payment_session_id'))
    reference = xendit.identifier(data.get('reference_id'))
    checkout = XenditCheckout.objects.filter(reference_id=reference).first()
    if checkout is None:
        raise xendit.XenditError('UNKNOWN_CHECKOUT')
    if checkout.payment_session_id is None:
        # Creation may still be completing, or have timed out. Retry/reconcile.
        raise xendit.XenditError('CHECKOUT_PENDING', retryable=True)
    if checkout.payment_session_id != session_id:
        raise xendit.XenditError('SESSION_MISMATCH')
    if checkout.status == 'COMPLETED':
        return
    if event == 'payment_session.expired':
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=checkout.order_id)
            XenditCheckout.objects.filter(pk=checkout.pk).exclude(status='COMPLETED').update(
                status='EXPIRED', payment_link_url='', updated_at=timezone.now())
        return
    session = xendit.retrieve_session(session_id)
    xendit.verify_session(session, checkout, session_id=session_id, completed=True)
    payment_id = xendit.identifier(session.get('payment_id'))
    payment = xendit.retrieve_payment(payment_id)
    verified_amount = xendit.verify_payment(payment, checkout, payment_id)
    try:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=checkout.order_id)
            invoice = Invoice.objects.select_for_update().get(order=order)
            locked = XenditCheckout.objects.select_for_update().get(pk=checkout.pk)
            if locked.status == 'COMPLETED':
                return
            if locked.payment_session_id != session_id or locked.amount != verified_amount:
                raise xendit.XenditError('SESSION_MISMATCH')
            _payable(order, invoice, locked)
            record_verified_payment(order_id=order.pk, provider='XENDIT',
                transaction_id=payment_id, amount=verified_amount)
            locked.status = 'COMPLETED'
            locked.verified_payment_id = payment_id
            locked.payment_link_url = ''
            locked.error_code = ''
            locked.save(update_fields=['status', 'verified_payment_id', 'payment_link_url', 'error_code', 'updated_at'])
    except (ValidationError, IntegrityError):
        raise xendit.XenditError('PAYMENT_CONFLICT') from None
