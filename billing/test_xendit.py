"""Xendit tests use only synthetic credentials and block the real HTTP transport."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock, patch
import json
import uuid

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.checks import run_checks
from django.core.management import call_command
from django.db import connection, IntegrityError, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from . import xendit
from .checkouts import reconcile_checkout
from .models import Invoice, Order, Payment, Subscription, VPSPlan, XenditCheckout
from .payments import record_verified_payment
from .services import confirm_order


@override_settings(DEBUG=False, ALLOW_TEST_PAYMENT=False,
    XENDIT_SECRET_API_KEY='synthetic-api-key', XENDIT_WEBHOOK_TOKEN='synthetic-callback-token',
    XENDIT_BUSINESS_ID='business-test', XENDIT_PUBLIC_BASE_URL='https://billing.example',
    PROVISIONING_API_URL='https://provisioner.invalid', BILLING_API_SECRET='synthetic-provisioning-key',
    PROVISIONING_DEFAULT_OS='Ubuntu 26.04')
class XenditTests(TransactionTestCase):
    def setUp(self):
        guard = patch('requests.adapters.HTTPAdapter.send', side_effect=AssertionError('Real network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        mocker = patch('requests.Session.request', autospec=True)
        self.http = mocker.start()
        self.addCleanup(mocker.stop)
        self.http.side_effect = self.fake_api
        self.remote_sessions = {}
        self.remote_payments = {}
        self.user = User.objects.create_user('payer', email='payer@example.com')
        self.other = User.objects.create_user('other')
        self.plan = VPSPlan.objects.create(name='Plan', cpu=2, ram=4, storage=50,
                                          monthly_price='299.95', yearly_price='2990.50')
        self.order = self.new_order()
        self.client.force_login(self.user)
        self.webhook_client = Client(enforce_csrf_checks=True)

    def new_order(self, customer=None):
        return confirm_order(customer=customer or self.user.customer_profile, plan_id=self.plan.pk,
            cycle='MONTHLY', checkout_token=uuid.uuid4(), reviewed_amount='299.95')

    def fake_api(self, client, method, url, **kwargs):
        self.assertFalse(connection.in_atomic_block, 'HTTP must not run inside a transaction')
        self.assertFalse(client.trust_env)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (5, 20))
        if url.startswith('https://provisioner.invalid'):
            return Mock(status_code=200, json=Mock(return_value={'status': 'Provisioning', 'vps_id': 1, 'vmid': 101}))
        self.assertTrue(kwargs['verify'])
        self.assertEqual(kwargs['auth'], ('synthetic-api-key', ''))
        if method == 'POST' and url == 'https://api.xendit.co/sessions':
            sid = 'ps-' + uuid.uuid4().hex
            data = dict(kwargs['json'], payment_session_id=sid, business_id='business-test',
                        status='ACTIVE', payment_link_url='https://checkout.xendit.co/sessions/' + sid)
            self.remote_sessions[sid] = data
        elif method == 'GET' and '/sessions/' in url:
            data = self.remote_sessions[url.rsplit('/', 1)[1]]
        elif method == 'GET' and '/v3/payments/' in url:
            self.assertEqual(kwargs['headers']['api-version'], '2024-11-11')
            data = self.remote_payments[url.rsplit('/', 1)[1]]
        else:
            raise AssertionError('Unexpected HTTP request')
        return Mock(status_code=200, json=Mock(return_value=deepcopy(data)))

    def checkout(self, order=None, **browser_fields):
        response = self.client.post(reverse('pay_now', args=[(order or self.order).pk]), browser_fields)
        self.assertEqual(response.status_code, 302)
        return XenditCheckout.objects.filter(order=order or self.order).latest('pk')

    def complete(self, checkout):
        sid = checkout.payment_session_id
        pid = 'py-' + uuid.uuid4().hex
        self.remote_sessions[sid].update(status='COMPLETED', payment_id=pid)
        self.remote_payments[pid] = {'payment_id': pid, 'status': 'SUCCEEDED', 'currency': 'PHP',
            'request_amount': str(checkout.amount), 'reference_id': checkout.reference_id, 'business_id': 'business-test'}
        return pid

    def event(self, checkout, event='payment_session.completed', **data):
        return {'event': event, 'business_id': 'business-test', 'data': {
            'payment_session_id': checkout.payment_session_id, 'reference_id': checkout.reference_id, **data}}

    def post_event(self, payload, token='synthetic-callback-token'):
        return self.webhook_client.post(reverse('xendit_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_CALLBACK_TOKEN=token)

    def assert_unpaid(self):
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Subscription.objects.exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'PENDING')
        self.assertEqual(self.order.invoice.status, 'PENDING')
        self.assertFalse(any('provisioner.invalid' in call.args[2] for call in self.http.call_args_list))

    def test_pay_now_uses_database_amount_and_required_session_options(self):
        checkout = self.checkout(amount='0.01', status='PAID', payment_id='forged', owner=self.other.pk)
        payload = self.http.call_args.kwargs['json']
        self.assertEqual(Decimal(str(payload['amount'])), self.order.amount)
        for key, value in {'currency': 'PHP', 'country': 'PH', 'session_type': 'PAY',
                           'mode': 'PAYMENT_LINK', 'capture_method': 'AUTOMATIC',
                           'allow_save_payment_method': 'DISABLED'}.items():
            self.assertEqual(payload[key], value)
        self.assertTrue(payload['success_return_url'].startswith('https://billing.example/'))
        self.assertTrue(payload['cancel_return_url'].startswith('https://billing.example/'))
        self.assertEqual(checkout.amount, self.order.amount)
        self.assertEqual(checkout.status, 'ACTIVE')
        self.assertEqual(checkout.payment_link_url, self.remote_sessions[checkout.payment_session_id]['payment_link_url'])
        self.assert_unpaid()

    def test_active_checkout_reused_and_url_returned(self):
        checkout = self.checkout()
        response = self.client.post(reverse('pay_now', args=[self.order.pk]))
        self.assertEqual(response.url, checkout.payment_link_url)
        self.assertEqual(XenditCheckout.objects.count(), 1)
        self.assertEqual([call.args[1] for call in self.http.call_args_list], ['POST', 'GET'])
        self.assert_unpaid()

    def test_expired_remote_session_gets_new_attempt(self):
        checkout = self.checkout()
        self.remote_sessions[checkout.payment_session_id]['status'] = 'EXPIRED'
        new = self.checkout()
        self.assertNotEqual(checkout.reference_id, new.reference_id)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'EXPIRED')
        self.assert_unpaid()

    def test_customer_cannot_pay_or_view_returns_for_another_order(self):
        order = self.new_order(self.other.customer_profile)
        for route in ('pay_now', 'xendit_return', 'xendit_cancel'):
            operation = self.client.post if route == 'pay_now' else self.client.get
            self.assertEqual(operation(reverse(route, args=[order.pk])).status_code, 404)
        self.http.assert_not_called()

    def test_login_post_and_csrf_required_for_checkout(self):
        self.assertEqual(self.client.get(reverse('pay_now', args=[self.order.pk])).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse('pay_now', args=[self.order.pk])).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(reverse('pay_now', args=[self.order.pk])).status_code, 302)
        self.http.assert_not_called()

    def test_callback_token_checked_before_json_or_provider_requests(self):
        for token in ('', 'wrong'):
            response = self.webhook_client.post(reverse('xendit_webhook'), data='{invalid',
                content_type='application/json', HTTP_X_CALLBACK_TOKEN=token)
            self.assertEqual(response.status_code, 401)
        self.http.assert_not_called()
        self.assert_unpaid()

    def test_webhook_post_only_and_bad_json_rejected(self):
        self.assertEqual(self.webhook_client.get(reverse('xendit_webhook')).status_code, 405)
        for body in ('{', '[]', 'null'):
            response = self.webhook_client.post(reverse('xendit_webhook'), data=body,
                content_type='application/json', HTTP_X_CALLBACK_TOKEN='synthetic-callback-token')
            self.assertEqual(response.status_code, 400)
        self.http.assert_not_called()

    def test_forged_completed_body_does_not_override_live_session(self):
        checkout = self.checkout()
        response = self.post_event(self.event(checkout, status='COMPLETED', amount=299.95,
                                              payment_id='py-forged'))
        self.assertEqual(response.status_code, 400)
        self.assert_unpaid()

    def test_session_verification_rejects_every_required_mismatch(self):
        checkout = self.checkout()
        self.complete(checkout)
        original = deepcopy(self.remote_sessions[checkout.payment_session_id])
        for key, value in [('status', 'ACTIVE'), ('session_type', 'SAVE'), ('currency', 'USD'),
            ('country', 'ID'), ('reference_id', 'other-reference'), ('business_id', 'other-business'),
            ('payment_session_id', 'ps-other'), ('amount', '0.01'), ('amount', 'NaN'), ('payment_id', None)]:
            with self.subTest(key=key, value=value):
                self.remote_sessions[checkout.payment_session_id] = dict(original, **{key: value})
                with patch('billing.checkouts.record_verified_payment') as recorder:
                    self.assertEqual(self.post_event(self.event(checkout)).status_code, 400)
                    recorder.assert_not_called()
                self.assert_unpaid()

    def test_payment_verification_rejects_every_required_mismatch(self):
        checkout = self.checkout()
        pid = self.complete(checkout)
        original = deepcopy(self.remote_payments[pid])
        for key, value in [('status', 'PENDING'), ('status', 'AUTHORIZED'), ('currency', 'USD'),
            ('request_amount', '0.01'), ('request_amount', 'Infinity'), ('reference_id', 'other'),
            ('business_id', 'other'), ('payment_id', 'py-other')]:
            with self.subTest(key=key, value=value):
                self.remote_payments[pid] = dict(original, **{key: value})
                with patch('billing.checkouts.record_verified_payment') as recorder:
                    self.assertEqual(self.post_event(self.event(checkout)).status_code, 400)
                    recorder.assert_not_called()
                self.assert_unpaid()

    def test_webhook_reference_session_and_business_mismatch(self):
        checkout = self.checkout()
        self.complete(checkout)
        self.http.reset_mock()
        for data in ({'reference_id': 'wrong'}, {'payment_session_id': 'ps-wrong'}):
            self.assertEqual(self.post_event(self.event(checkout, **data)).status_code, 400)
        payload = self.event(checkout)
        payload['business_id'] = 'wrong'
        self.assertEqual(self.post_event(payload).status_code, 400)
        self.http.assert_not_called()
        self.assert_unpaid()

    def test_changed_local_order_or_invoice_amount_rejected(self):
        checkout = self.checkout()
        self.complete(checkout)
        for model in (Order, Invoice):
            with self.subTest(model=model):
                model.objects.filter(pk=self.order.pk if model == Order else self.order.invoice.pk).update(amount='1.00')
                self.assertEqual(self.post_event(self.event(checkout)).status_code, 400)
                model.objects.filter(pk=self.order.pk if model == Order else self.order.invoice.pk).update(amount='299.95')
                self.assert_unpaid()

    def test_session_api_timeout_is_retryable_not_paid(self):
        checkout = self.checkout()
        self.complete(checkout)
        self.http.side_effect = requests.Timeout('synthetic-api-key synthetic-callback-token')
        response = self.post_event(self.event(checkout))
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, 'synthetic-api-key', status_code=503)
        self.assert_unpaid()

    def test_payment_api_failure_is_retryable_not_paid(self):
        checkout = self.checkout()
        self.complete(checkout)
        def failing(client, method, url, **kwargs):
            if '/v3/payments/' in url:
                return Mock(status_code=503, text='synthetic-api-key')
            return self.fake_api(client, method, url, **kwargs)
        self.http.side_effect = failing
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 503)
        self.assert_unpaid()

    def test_valid_completed_event_records_once_and_provisions_after_commit(self):
        checkout = self.checkout()
        pid = self.complete(checkout)
        with patch('billing.checkouts.record_verified_payment', wraps=record_verified_payment) as recorder:
            response = self.post_event(self.event(checkout))
            self.assertEqual(response.status_code, 200)
            recorder.assert_called_once_with(order_id=self.order.pk, provider='XENDIT',
                                            transaction_id=pid, amount=Decimal('299.95'))
        payment = Payment.objects.get()
        self.assertEqual(payment.status, 'SUCCESS')
        self.assertEqual(payment.transaction_id, pid)
        self.assertEqual(payment.amount, self.order.amount)
        self.assertIsNone(payment.raw_response)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'PROVISIONING')
        self.assertEqual(self.order.invoice.status, 'PAID')
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'COMPLETED')
        self.assertEqual(checkout.verified_payment_id, pid)
        calls = self.http.call_count
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 200)
        self.assertEqual(self.http.call_count, calls)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(self.post_event(self.event(checkout, 'payment_session.expired')).status_code, 200)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'COMPLETED')

    def test_expired_event_updates_only_checkout(self):
        checkout = self.checkout()
        self.http.reset_mock()
        self.assertEqual(self.post_event(self.event(checkout, 'payment_session.expired')).status_code, 200)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'EXPIRED')
        self.http.assert_not_called()
        self.assert_unpaid()

    def test_completed_event_after_expired_is_verified(self):
        checkout = self.checkout()
        self.post_event(self.event(checkout, 'payment_session.expired'))
        self.complete(checkout)
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 200)
        self.assertEqual(Payment.objects.count(), 1)

    def test_payment_id_cannot_be_reused_for_other_order(self):
        first = self.checkout()
        pid = self.complete(first)
        self.assertEqual(self.post_event(self.event(first)).status_code, 200)
        other_order = self.new_order()
        second = self.checkout(other_order)
        self.complete(second)
        self.remote_sessions[second.payment_session_id]['payment_id'] = pid
        self.remote_payments[pid]['reference_id'] = second.reference_id
        self.assertEqual(self.post_event(self.event(second)).status_code, 400)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Invoice.objects.get(order=other_order).status, 'PENDING')
        second.refresh_from_db()
        self.assertNotEqual(second.status, 'COMPLETED')

    def test_success_and_cancel_returns_never_mark_paid(self):
        for route in ('xendit_return', 'xendit_cancel'):
            response = self.client.get(reverse(route, args=[self.order.pk]) + '?payment=success&status=PAID')
            self.assertContains(response, 'payment is being verified')
        self.http.assert_not_called()
        self.assert_unpaid()

    def test_paid_return_and_invoice_ui(self):
        checkout = self.checkout()
        self.assertContains(self.client.get(reverse('invoice', args=[self.order.pk])), 'Continue Payment')
        self.complete(checkout)
        self.post_event(self.event(checkout))
        self.assertContains(self.client.get(reverse('xendit_return', args=[self.order.pk])), 'Payment confirmed')
        self.assertNotContains(self.client.get(reverse('invoice', args=[self.order.pk])), 'Pay Now')

    def test_pending_invoice_and_order_show_pay_now(self):
        for route in ('invoice', 'order_detail'):
            self.assertContains(self.client.get(reverse(route, args=[self.order.pk])), 'Pay Now')
        self.http.assert_not_called()

    def test_create_timeout_blocks_blind_retry_and_can_be_reconciled(self):
        def ambiguous(client, method, url, **kwargs):
            self.fake_api(client, method, url, **kwargs)
            raise requests.Timeout('synthetic-api-key')
        self.http.side_effect = ambiguous
        response = self.client.post(reverse('pay_now', args=[self.order.pk]))
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, 'synthetic-api-key', status_code=503)
        checkout = XenditCheckout.objects.get()
        self.assertEqual(checkout.status, 'UNKNOWN')
        self.assertEqual(checkout.error_code, 'TIMEOUT')
        self.client.post(reverse('pay_now', args=[self.order.pk]))
        self.assertEqual(self.http.call_count, 1)
        self.http.side_effect = self.fake_api
        sid = next(iter(self.remote_sessions))
        reconcile_checkout(checkout.reference_id, sid)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'ACTIVE')
        self.assert_unpaid()

    def test_early_webhook_retries_until_session_id_is_persisted(self):
        checkout = self.checkout()
        XenditCheckout.objects.filter(pk=checkout.pk).update(payment_session_id=None, status='CREATING')
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 503)
        self.assert_unpaid()

    def test_unknown_checkout_and_path_injection_rejected(self):
        checkout = self.checkout()
        for field in ('reference_id', 'payment_session_id'):
            self.assertEqual(self.post_event(self.event(checkout, **{field: '../secret'})).status_code, 400)
        self.assert_unpaid()

    def test_redirect_and_invalid_json_fail_safely(self):
        for response in (Mock(status_code=302), Mock(status_code=200, json=Mock(side_effect=ValueError('secret')))):
            self.http.return_value = response
            self.http.side_effect = None
            with self.assertRaises(xendit.XenditError) as caught:
                xendit.retrieve_session('ps-test')
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn('secret', str(caught.exception))
        self.assert_unpaid()

    def test_gateway_checkout_url_allowlist(self):
        for url in ('http://checkout.xendit.co/pay', 'https://evil.example/pay',
                    'https://checkout.xendit.co.evil.example/pay', 'https://user:pass@xen.to/pay'):
            with self.subTest(url=url), self.assertRaises(xendit.XenditError):
                xendit.checkout_url(url)
        self.assertEqual(xendit.checkout_url('https://dev.xen.to/test'), 'https://dev.xen.to/test')

    @override_settings(XENDIT_PUBLIC_BASE_URL='http://billing.example')
    def test_https_public_origin_required_even_with_debug(self):
        with override_settings(DEBUG=True):
            self.assertFalse(xendit.configured())
            self.assertTrue(any(error.id == 'billing.E001' for error in run_checks()))
            self.assertEqual(self.client.post(reverse('pay_now', args=[self.order.pk])).status_code, 503)
        self.http.assert_not_called()

    @override_settings(XENDIT_SECRET_API_KEY='', XENDIT_WEBHOOK_TOKEN='',
                       XENDIT_BUSINESS_ID='', XENDIT_PUBLIC_BASE_URL='')
    def test_empty_configuration_disables_gateway(self):
        self.assertFalse(xendit.configured())
        self.assertFalse(any(error.id == 'billing.E001' for error in run_checks()))
        self.assertNotContains(self.client.get(reverse('invoice', args=[self.order.pk])), 'Pay Now')
        self.assertEqual(self.post_event({'event': 'payment_session.completed'}).status_code, 401)
        self.http.assert_not_called()

    def test_no_public_test_payment_endpoint(self):
        self.assertFalse(settings.ALLOW_TEST_PAYMENT)
        self.assertEqual(self.client.post(f'/orders/{self.order.pk}/test-payment/').status_code, 404)
        self.assert_unpaid()

    def test_only_one_open_checkout_per_order(self):
        checkout = self.checkout()
        with self.assertRaises(IntegrityError), transaction.atomic():
            XenditCheckout.objects.create(order=self.order, amount=self.order.amount,
                                         reference_id='another-reference')
        self.assertEqual(XenditCheckout.objects.count(), 1)

    def test_paid_during_checkout_refresh_cannot_redirect_to_payment(self):
        checkout = self.checkout()
        original = self.fake_api
        def race(client, method, url, **kwargs):
            if method == 'GET':
                Order.objects.filter(pk=self.order.pk).update(status='PAID')
                Invoice.objects.filter(order=self.order).update(status='PAID')
            return original(client, method, url, **kwargs)
        self.http.side_effect = race
        response = self.client.post(reverse('pay_now', args=[self.order.pk]))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Payment.objects.exists())

    def test_stale_active_response_cannot_revive_expired_checkout(self):
        from .checkouts import _session_state
        checkout = self.checkout()
        self.post_event(self.event(checkout, 'payment_session.expired'))
        result = _session_state(checkout.pk, self.remote_sessions[checkout.payment_session_id],
                                session_id=checkout.payment_session_id)
        self.assertEqual(result.status, 'EXPIRED')
        self.assert_unpaid()

    def test_missing_invoice_returns_not_found_without_network(self):
        legacy = Order.objects.create(customer=self.user.customer_profile, plan=self.plan,
                                      billing_cycle='MONTHLY', amount='299.95')
        self.assertEqual(self.client.post(reverse('pay_now', args=[legacy.pk])).status_code, 404)
        self.http.assert_not_called()

    def test_verified_amount_uses_decimal_not_float_rounding(self):
        checkout = self.checkout()
        pid = self.complete(checkout)
        self.remote_payments[pid]['request_amount'] = '299.95000000000000001'
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 400)
        self.assert_unpaid()

    def test_credentials_and_raw_provider_data_are_not_persisted(self):
        checkout = self.checkout()
        pid = self.complete(checkout)
        self.remote_sessions[checkout.payment_session_id]['private'] = 'synthetic-api-key'
        self.remote_payments[pid]['payment_details'] = {'card': 'private-card-data'}
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 200)
        saved = str(list(XenditCheckout.objects.values())) + str(list(Payment.objects.values()))
        for secret in ('synthetic-api-key', 'synthetic-callback-token', 'private-card-data'):
            self.assertNotIn(secret, saved)

    def test_session_network_connection_failure_is_retryable(self):
        checkout = self.checkout()
        self.complete(checkout)
        self.http.side_effect = requests.ConnectionError('private credential details')
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 503)
        self.assert_unpaid()

    def test_get_detected_completed_session_waits_for_webhook(self):
        checkout = self.checkout()
        self.complete(checkout)
        response = self.client.post(reverse('pay_now', args=[self.order.pk]))
        self.assertEqual(response.status_code, 503)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'VERIFYING')
        self.assert_unpaid()
        self.assertEqual(self.post_event(self.event(checkout)).status_code, 200)
        self.assertEqual(Payment.objects.count(), 1)
