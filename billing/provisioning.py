"""Paid-order dispatch, recoverable request leases, and VM100 status synchronization."""
import calendar
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Invoice, Order, Payment, Subscription
from .provisioning_api import ProvisioningAPIError, api_request

LEASE_TIME = timedelta(minutes=5)
SYNC_INTERVAL = timedelta(seconds=10)
TEST_PROVIDER = 'TEST_ONLY'


def require_paid(order):
    """Statuses alone are insufficient: require consistent server-side payment evidence."""
    invoice = Invoice.objects.filter(order=order, status=Invoice.Status.PAID,
                                     amount=order.amount, paid_at__isnull=False).first()
    payments = Payment.objects.filter(invoice=invoice, status=Payment.Status.SUCCESS,
                                      amount=order.amount, paid_at__isnull=False)
    if not settings.DEBUG:
        payments = payments.exclude(provider=TEST_PROVIDER)
    if not invoice or not payments.exists() or order.status in (Order.Status.PENDING, Order.Status.CANCELLED):
        raise ValidationError('A verified server-side payment is required.')
    return invoice


def _next_billing_date(start, cycle):
    months = 12 if cycle == Order.BillingCycle.YEARLY else 1
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    return start.replace(year=year, month=month,
                         day=min(start.day, calendar.monthrange(year, month)[1]))


def _claim(order_id, *, sync, retry):
    # Refuse misuse by callers inside atomic blocks: HTTP must run after commit.
    if connection.in_atomic_block:
        raise RuntimeError('Provisioning must be called outside a database transaction.')
    now = timezone.now()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        require_paid(order)
        if order.status == Order.Status.ACTIVE:
            return None
        if order.provisioning_lease and order.provisioning_started_at and order.provisioning_started_at > now - LEASE_TIME:
            return None
        if sync:
            if not order.provisioning_payload:
                return None
            if order.provisioning_checked_at and order.provisioning_checked_at > now - SYNC_INTERVAL:
                return None
        elif not retry and order.provisioning_status in ('ACCEPTED', 'ERROR', 'FAILED'):
            return None
        payload = order.provisioning_payload
        if not sync and not payload:
            plan = order.plan
            if not settings.PROVISIONING_DEFAULT_OS.strip():
                raise ValidationError('A server-side default OS must be configured.')
            payload = {'order_id': order.pk, 'name': f'customer-{order.customer_id}-vps-{order.pk}',
                       'cpu': plan.cpu, 'ram': plan.ram, 'storage': plan.storage,
                       'os': settings.PROVISIONING_DEFAULT_OS, 'billing_cycle': order.billing_cycle,
                       'plan': plan.name}
        lease = uuid.uuid4()
        # Conditional acquisition also avoids parallel winners on databases where
        # SELECT FOR UPDATE is unavailable. Late results require the same lease.
        available = Q(provisioning_lease__isnull=True) | Q(provisioning_started_at__lte=now - LEASE_TIME)
        updates = {'provisioning_lease': lease, 'provisioning_started_at': now,
                   'provisioning_payload': payload, 'updated_at': now}
        if not sync:
            updates.update(status=Order.Status.PROVISIONING, provisioning_status='SENDING', provisioning_error='')
        if not Order.objects.filter(pk=order.pk).filter(available).update(**updates):
            return None
    return lease, payload


def _finish(order_id, lease, result=None, error=''):
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.provisioning_lease != lease:
            return order  # A newer worker owns the result now.
        order.provisioning_lease = None
        order.provisioning_checked_at = timezone.now()
        if result and (
            (order.provisioning_vps_id and result.vps_id and order.provisioning_vps_id != result.vps_id) or
            (order.provisioning_vmid and result.vmid and order.provisioning_vmid != result.vmid)
        ):
            error = 'REMOTE_ID_MISMATCH'
        if error:
            # A timeout is an unknown remote outcome, not proof the VPS failed.
            order.provisioning_error = error
            order.provisioning_status = 'ERROR'
        else:
            require_paid(order)
            order.status = result.status
            order.provisioning_status = 'ACCEPTED' if result.status == 'PROVISIONING' else result.status
            order.provisioning_error = result.error
            order.provisioning_vps_id = result.vps_id or order.provisioning_vps_id
            order.provisioning_vmid = result.vmid or order.provisioning_vmid
            if result.status == Order.Status.ACTIVE:
                now = timezone.now()
                subscription, created = Subscription.objects.get_or_create(order=order, defaults={
                    'customer': order.customer, 'start_date': now,
                    'next_billing_date': _next_billing_date(now, order.billing_cycle),
                    'status': Subscription.Status.ACTIVE,
                })
                if subscription.customer_id != order.customer_id:
                    raise ValidationError('Subscription ownership does not match the order.')
                if not created and subscription.status != Subscription.Status.ACTIVE:
                    subscription.status = Subscription.Status.ACTIVE
                    subscription.save(update_fields=['status'])
        order.save(update_fields=['status', 'provisioning_status', 'provisioning_error',
            'provisioning_vps_id', 'provisioning_vmid', 'provisioning_lease',
            'provisioning_checked_at', 'updated_at'])
    return order


def _run(order, *, sync=False, retry=False):
    order_id = order.pk
    claim = _claim(order_id, sync=sync, retry=retry)
    if claim is None:
        return Order.objects.get(pk=order_id)
    lease, payload = claim
    try:
        result = api_request('GET' if sync else 'POST', order_id,
                             payload=None if sync else payload)
    except ProvisioningAPIError as exc:
        return _finish(order_id, lease, error=exc.code)
    return _finish(order_id, lease, result=result)


def request_vps_provisioning(order, *, retry=False):
    """Dispatch a paid order; explicit retry reuses the durable payload/order ID."""
    return _run(order, retry=retry)


def get_vps_provisioning_status(order):
    """Fetch and apply VM100 status without making any provisioning POST."""
    return _run(order, sync=True)
