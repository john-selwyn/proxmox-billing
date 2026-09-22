"""Integration tests: every HTTP boundary is mocked; no external hosts are contacted."""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import Mock, patch
import uuid

import requests
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Invoice, Order, Payment, Subscription, VPSPlan
from .payments import confirm_test_payment, record_verified_payment
from .provisioning import (
    _finish, _next_billing_date, get_vps_provisioning_status, request_vps_provisioning,
)
from .provisioning_api import ProvisioningResult
from .services import confirm_order


@override_settings(DEBUG=True, PROVISIONING_API_URL='https://provisioner.invalid',
                   BILLING_API_SECRET='unit-test-secret-only', PROVISIONING_DEFAULT_OS='Ubuntu 26.04')
class ProvisioningTests(TransactionTestCase):
    def setUp(self):
        # Guard the actual socket path as well as mocking the intended boundary.
        guard = patch('requests.adapters.HTTPAdapter.send', side_effect=AssertionError('Real HTTP is forbidden in tests'))
        guard.start()
        self.addCleanup(guard.stop)
        mocker = patch('requests.Session.request')
        self.http = mocker.start()
        self.addCleanup(mocker.stop)
        self.respond()
        self.user = User.objects.create_user('buyer')
        self.plan = VPSPlan.objects.create(name='VPS Starter', cpu=2, ram=4, storage=50,
                                          monthly_price='299.00', yearly_price='2990.00')
        self.order = confirm_order(customer=self.user.customer_profile, plan_id=self.plan.pk,
            cycle='MONTHLY', checkout_token=uuid.uuid4(), reviewed_amount='299.00')

    def respond(self, status='Provisioning', code=200, **extra):
        data = {'success': True, 'status': status, 'vps_id': 15, 'vmid': 115, **extra}
        self.http.return_value = Mock(status_code=code)
        self.http.return_value.json.return_value = data
        self.http.side_effect = None

    def pay(self, **overrides):
        args = dict(order_id=self.order.pk, provider='verified-fixture',
                    transaction_id=f'verified-{self.order.pk}', amount=self.order.amount)
        args.update(overrides)
        return record_verified_payment(**args)

    def sync(self):
        Order.objects.filter(pk=self.order.pk).update(provisioning_checked_at=None)
        return get_vps_provisioning_status(self.order)

    def test_unpaid_order_cannot_trigger_or_sync(self):
        for operation in (request_vps_provisioning, get_vps_provisioning_status):
            with self.assertRaises(ValidationError):
                operation(self.order)
        self.http.assert_not_called()

    def test_order_paid_status_alone_is_not_payment_evidence(self):
        Order.objects.filter(pk=self.order.pk).update(status='PAID')
        with self.assertRaises(ValidationError):
            request_vps_provisioning(self.order)
        self.http.assert_not_called()

    def test_paid_order_sends_trusted_resources_and_auth(self):
        self.order.plan.cpu = 99  # Unsaved/browser-like changes cannot affect payload.
        self.pay()
        args, kwargs = self.http.call_args
        self.assertEqual(args, ('POST', 'https://provisioner.invalid/api/internal/provision/'))
        self.assertEqual(kwargs['json'], {
            'order_id': self.order.pk, 'name': f'customer-{self.user.customer_profile.pk}-vps-{self.order.pk}',
            'cpu': 2, 'ram': 4, 'storage': 50, 'os': 'Ubuntu 26.04',
            'billing_cycle': 'MONTHLY', 'plan': 'VPS Starter',
        })
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer unit-test-secret-only')
        self.assertEqual(kwargs['headers']['Content-Type'], 'application/json')
        self.assertEqual(kwargs['timeout'], (5, 20))
        self.assertFalse(kwargs['allow_redirects'])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'PROVISIONING')
        self.assertEqual(self.order.provisioning_vps_id, '15')
        self.assertEqual(self.order.provisioning_vmid, '115')

    def test_http_occurs_only_after_commit(self):
        def response(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Invoice.objects.get(order=self.order).status, 'PAID')
            self.assertEqual(Payment.objects.get().status, 'SUCCESS')
            return Mock(status_code=200, json=lambda: {'status': 'Provisioning'})
        self.http.side_effect = response
        with transaction.atomic():
            self.pay()
            self.http.assert_not_called()
            self.assertEqual(Order.objects.get(pk=self.order.pk).status, 'PAID')
        self.http.assert_called_once()

    def test_rollback_does_not_dispatch_or_keep_payment(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.pay()
                raise ValueError('rollback')
        self.http.assert_not_called()
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(Invoice.objects.get().status, 'PENDING')

    def test_direct_dispatch_in_transaction_rejected(self):
        with transaction.atomic(), self.assertRaises(RuntimeError):
            request_vps_provisioning(self.order)
        self.http.assert_not_called()

    def test_amount_mismatch_and_nonfinite_rejected(self):
        for amount in ['0.01', 'NaN', 'Infinity', 'bad']:
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                self.pay(amount=amount)
        self.assertFalse(Payment.objects.exists())
        self.http.assert_not_called()

    def test_duplicate_payment_and_request_do_not_duplicate_records(self):
        self.pay()
        self.pay()
        request_vps_provisioning(self.order)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_different_successful_reference_is_rejected(self):
        self.pay()
        with self.assertRaises(ValidationError):
            self.pay(transaction_id='another-reference')
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.http.call_count, 1)

    def test_reference_cannot_be_reused_for_another_order(self):
        self.pay()
        other = confirm_order(customer=self.user.customer_profile, plan_id=self.plan.pk,
            cycle='MONTHLY', checkout_token=uuid.uuid4(), reviewed_amount='299.00')
        with self.assertRaises(ValidationError):
            self.pay(order_id=other.pk)
        self.assertEqual(Invoice.objects.get(order=other).status, 'PENDING')

    def test_timeout_and_connection_errors_are_safe_and_retriable(self):
        for exception, code in [(requests.Timeout, 'TIMEOUT'), (requests.ConnectionError, 'CONNECTION_FAILED')]:
            with self.subTest(code=code):
                self.http.side_effect = exception('Bearer unit-test-secret-only internal host details')
                if not Payment.objects.exists():
                    self.pay()
                else:
                    request_vps_provisioning(self.order, retry=True)
                self.order.refresh_from_db()
                self.assertEqual(self.order.provisioning_error, code)
                self.assertEqual(self.order.status, 'PROVISIONING')
                self.assertIsNone(self.order.provisioning_lease)
        self.respond()
        request_vps_provisioning(self.order, retry=True)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_non_2xx_and_redirect_errors_do_not_leak_response(self):
        self.respond(code=503, error_message='unit-test-secret-only stack trace')
        self.pay()
        self.order.refresh_from_db()
        self.assertEqual(self.order.provisioning_error, 'HTTP_503')
        self.respond(code=302)
        request_vps_provisioning(self.order, retry=True)
        self.order.refresh_from_db()
        self.assertEqual(self.order.provisioning_error, 'HTTP_302')

    def test_invalid_json_and_invalid_shape(self):
        self.http.return_value.json.side_effect = ValueError('unit-test-secret-only')
        self.pay()
        self.order.refresh_from_db()
        self.assertEqual(self.order.provisioning_error, 'INVALID_JSON')
        for data, code in [([], 'INVALID_RESPONSE'), ({'status': 'unknown'}, 'UNKNOWN_STATUS'),
                           ({'success': False}, 'REMOTE_REJECTED'),
                           ({'status': 'Running', 'billing_order_id': -1}, 'ORDER_MISMATCH'),
                           ({'status': 'Running', 'vmid': 'unit-test-secret-only'}, 'INVALID_RESPONSE')]:
            self.respond()
            self.http.return_value.json.return_value = data
            request_vps_provisioning(self.order, retry=True)
            self.order.refresh_from_db()
            self.assertEqual(self.order.provisioning_error, code)
            self.assertFalse(Subscription.objects.exists())

    def test_running_creates_subscription_once_and_preserves_payment(self):
        self.pay()
        paid_at = Invoice.objects.get().paid_at
        self.respond('rUnNiNg', billing_order_id=self.order.pk, progress=100,
                     current_step='Finished', ip_address='0.0.0.0', error_message='')
        order = self.sync()
        self.assertEqual(self.http.call_args.args[0], 'GET')
        self.assertEqual(self.http.call_args.args[1], f'https://provisioner.invalid/api/internal/provision/{self.order.pk}/status/')
        self.assertEqual(order.status, 'ACTIVE')
        subscription = Subscription.objects.get()
        self.assertEqual(subscription.customer, self.user.customer_profile)
        self.assertEqual(subscription.status, 'ACTIVE')
        self.assertGreater(subscription.next_billing_date, subscription.start_date)
        self.sync()
        request_vps_provisioning(self.order, retry=True)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(Subscription.objects.count(), 1)
        self.assertEqual(Invoice.objects.get().paid_at, paid_at)
        self.assertEqual(Invoice.objects.get().status, 'PAID')
        self.assertEqual(Payment.objects.count(), 1)

    def test_failed_state_and_safe_customer_feedback(self):
        self.pay()
        self.respond('Failed', error_message='unit-test-secret-only at 10.0.0.1 traceback')
        order = self.sync()
        self.assertEqual(order.status, 'FAILED')
        self.assertEqual(order.provisioning_error, 'REMOTE_FAILED')
        self.assertEqual(Invoice.objects.get().status, 'PAID')
        self.assertFalse(Subscription.objects.exists())
        self.client.force_login(self.user)
        for name in ('order_detail', 'invoice'):
            response = self.client.get(reverse(name, args=[self.order.pk]))
            self.assertContains(response, 'VPS provisioning failed')
            self.assertNotContains(response, 'unit-test-secret-only')
            self.assertNotContains(response, '10.0.0.1')
            self.assertNotContains(response, 'vmid')

    def test_retry_uses_frozen_payload_and_same_order_id(self):
        self.respond('Failed')
        self.pay()
        first_payload = self.http.call_args.kwargs['json']
        VPSPlan.objects.filter(pk=self.plan.pk).update(cpu=99, ram=99, storage=99)
        self.respond()
        request_vps_provisioning(self.order, retry=True)
        self.assertEqual(self.http.call_args.kwargs['json'], first_payload)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_fresh_lease_avoids_concurrent_dispatch_and_stale_lease_recovers(self):
        with patch('billing.payments.request_vps_provisioning'):
            self.pay()
        Order.objects.filter(pk=self.order.pk).update(provisioning_lease=uuid.uuid4(), provisioning_started_at=timezone.now())
        request_vps_provisioning(self.order, retry=True)
        self.http.assert_not_called()
        Order.objects.filter(pk=self.order.pk).update(provisioning_started_at=timezone.now() - timedelta(minutes=6))
        request_vps_provisioning(self.order, retry=True)
        self.http.assert_called_once()

    def test_late_result_does_not_overwrite_new_worker(self):
        self.pay()
        order = _finish(self.order.pk, uuid.uuid4(), result=ProvisioningResult('FAILED'))
        self.assertEqual(order.status, 'PROVISIONING')

    def test_sync_cooldown_avoids_repeated_calls(self):
        self.pay()
        get_vps_provisioning_status(self.order)
        self.assertEqual(self.http.call_count, 1)

    def test_test_payment_only_command_and_idempotence(self):
        out = StringIO()
        call_command('confirm_test_payment', self.order.pk, confirm=True, stdout=out)
        call_command('confirm_test_payment', self.order.pk, confirm=True, stdout=out)
        self.assertIn('TEST ONLY', out.getvalue())
        self.assertEqual(Payment.objects.get().provider, 'TEST_ONLY')
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.client.post(f'/orders/{self.order.pk}/test-payment/').status_code, 404)

    @override_settings(DEBUG=False)
    def test_test_payment_unavailable_in_production(self):
        with self.assertRaises(CommandError):
            call_command('confirm_test_payment', self.order.pk, confirm=True)
        with self.assertRaises(ValidationError):
            confirm_test_payment(self.order.pk)
        with self.assertRaises(ValidationError):
            self.pay(provider='TEST_ONLY')
        self.assertFalse(Payment.objects.exists())
        self.http.assert_not_called()

    def test_old_test_payment_cannot_provision_in_production(self):
        with patch('billing.payments.request_vps_provisioning'):
            confirm_test_payment(self.order.pk)
        with override_settings(DEBUG=False), self.assertRaises(ValidationError):
            request_vps_provisioning(self.order)
        self.http.assert_not_called()

    def test_test_command_requires_explicit_confirmation(self):
        with self.assertRaises(CommandError):
            call_command('confirm_test_payment', self.order.pk)
        self.assertFalse(Payment.objects.exists())

    def test_sync_command_and_active_customer_feedback(self):
        self.pay()
        self.respond('Running')
        Order.objects.filter(pk=self.order.pk).update(provisioning_checked_at=None)
        out = StringIO()
        call_command('sync_provisioning', self.order.pk, stdout=out)
        self.assertIn('ACTIVE', out.getvalue())
        self.client.force_login(self.user)
        response = self.client.get(reverse('order_detail', args=[self.order.pk]))
        self.assertContains(response, 'Payment confirmed')
        self.assertContains(response, 'Your VPS is active')

    @override_settings(PROVISIONING_API_URL='', BILLING_API_SECRET='')
    def test_missing_configuration_fails_without_network(self):
        self.pay()
        self.order.refresh_from_db()
        self.assertEqual(self.order.provisioning_error, 'NOT_CONFIGURED')
        self.http.assert_not_called()

    @override_settings(DEBUG=False, PROVISIONING_API_URL='http://provisioner.invalid',
                       PROVISIONING_ALLOW_HTTP=False)
    def test_production_requires_https(self):
        self.pay()
        self.order.refresh_from_db()
        self.assertEqual(self.order.provisioning_error, 'HTTPS_REQUIRED')
        self.http.assert_not_called()

    @override_settings(DEBUG=False, PROVISIONING_API_URL='http://provisioner.invalid',
                       PROVISIONING_ALLOW_HTTP=True)
    def test_explicit_http_opt_in_without_debug(self):
        self.pay()
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args,
                         ('POST', 'http://provisioner.invalid/api/internal/provision/'))
        self.assertEqual(self.http.call_args.kwargs['headers']['Authorization'],
                         'Bearer unit-test-secret-only')
        self.assertFalse(self.http.call_args.kwargs['allow_redirects'])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'PROVISIONING')
        self.assertEqual(self.order.provisioning_error, '')

    @override_settings(DEBUG=False, PROVISIONING_ALLOW_HTTP=False)
    def test_https_without_debug_or_http_opt_in(self):
        self.pay()
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args,
                         ('POST', 'https://provisioner.invalid/api/internal/provision/'))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'PROVISIONING')
        self.assertEqual(self.order.provisioning_error, '')

    @override_settings(DEBUG=True, PROVISIONING_API_URL='http://provisioner.invalid',
                       PROVISIONING_ALLOW_HTTP=False)
    def test_debug_still_allows_http(self):
        self.pay()
        self.http.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.provisioning_error, '')

    def test_calendar_billing_cycles(self):
        start = datetime(2028, 1, 31, tzinfo=dt_timezone.utc)
        self.assertEqual(_next_billing_date(start, 'MONTHLY').date().isoformat(), '2028-02-29')
        leap = datetime(2028, 2, 29, tzinfo=dt_timezone.utc)
        self.assertEqual(_next_billing_date(leap, 'YEARLY').date().isoformat(), '2029-02-28')

    def test_timeout_reconciles_with_get_without_second_post(self):
        self.http.side_effect = requests.Timeout('private diagnostic')
        self.pay()
        self.respond('Running', billing_order_id=self.order.pk)
        order = self.sync()
        self.assertEqual(order.status, 'ACTIVE')
        self.assertEqual([call.args[0] for call in self.http.call_args_list], ['POST', 'GET'])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Subscription.objects.count(), 1)

    def test_inconsistent_invoice_or_refunded_payment_blocks_retry(self):
        self.pay()
        Invoice.objects.filter(order=self.order).update(amount='0.01')
        with self.assertRaises(ValidationError):
            request_vps_provisioning(self.order, retry=True)
        Invoice.objects.filter(order=self.order).update(amount=self.order.amount)
        Payment.objects.update(status='REFUNDED')
        with self.assertRaises(ValidationError):
            request_vps_provisioning(self.order, retry=True)
        self.assertEqual(self.http.call_count, 1)

    def test_cancelled_order_cannot_be_paid(self):
        Order.objects.filter(pk=self.order.pk).update(status='CANCELLED')
        with self.assertRaises(ValidationError):
            self.pay()
        self.assertFalse(Payment.objects.exists())
        self.http.assert_not_called()

    def test_existing_subscription_reused_without_resetting_billing_dates(self):
        self.pay()
        start = timezone.now() - timedelta(days=1)
        due = start + timedelta(days=30)
        sub = Subscription.objects.create(order=self.order, customer=self.user.customer_profile,
            status='EXPIRED', start_date=start, next_billing_date=due)
        self.respond('Running')
        self.sync()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'ACTIVE')
        self.assertEqual(sub.next_billing_date, due)
        self.assertEqual(Subscription.objects.count(), 1)

    def test_changed_remote_ids_are_rejected(self):
        self.pay()
        self.respond('Running', vps_id=999)
        order = self.sync()
        self.assertEqual(order.provisioning_error, 'REMOTE_ID_MISMATCH')
        self.assertEqual(order.provisioning_vps_id, '15')
        self.assertEqual(order.status, 'PROVISIONING')
        self.assertFalse(Subscription.objects.exists())

    def test_customer_cannot_trigger_payment_or_provisioning(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('order_detail', args=[self.order.pk]),
            {'payment': 'success', 'status': 'PAID', 'cpu': 99, 'ram': 99})
        self.assertEqual(response.status_code, 405)
        self.client.get(reverse('invoice', args=[self.order.pk]) + '?payment=success')
        self.http.assert_not_called()
        self.assertFalse(Payment.objects.exists())
