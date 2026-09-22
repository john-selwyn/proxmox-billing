from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Customer, Invoice, Order, Payment, Subscription, VPSPlan


class PortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('alice', password='Test-Password-9821')
        cls.other = User.objects.create_user('bob', password='Test-Password-9821')
        cls.plan = VPSPlan.objects.create(name='Compute', description='A test plan', cpu=2,
            ram=4, storage=60, monthly_price=Decimal('299.00'), yearly_price=Decimal('2990.00'))

    def setUp(self):
        self.client.force_login(self.user)

    def review(self, cycle='MONTHLY', **extra):
        return self.client.post(reverse('select_plan', args=[self.plan.pk]),
                                {'billing_cycle': cycle, **extra})

    def checkout(self, cycle='MONTHLY', **extra):
        token = self.review(cycle).context['checkout_token']
        return self.client.post(reverse('order_confirm'), {'checkout_token': token, **extra})

    def other_order(self):
        order = Order.objects.create(customer=self.other.customer_profile, plan=self.plan,
            billing_cycle='MONTHLY', amount='299.00')
        Invoice.objects.create(order=order, invoice_number='OTHER-INVOICE', amount=order.amount,
                               due_date=timezone.now() + timedelta(days=1))
        return order

    def test_registration_creates_profile_and_logs_in(self):
        self.client.logout()
        response = self.client.post(reverse('register'), {'username': 'newcustomer',
            'password1': 'A-Strong-Password-9821', 'password2': 'A-Strong-Password-9821',
            'full_name': 'New Customer', 'email': 'new@example.com'})
        self.assertRedirects(response, reverse('dashboard'))
        user = User.objects.get(username='newcustomer')
        self.assertEqual(user.customer_profile.full_name, 'New Customer')
        self.assertEqual(user.email, 'new@example.com')
        self.assertEqual(Customer.objects.filter(user=user).count(), 1)

    def test_user_creation_also_creates_profile(self):
        self.assertEqual(self.user.customer_profile.full_name, 'alice')

    def test_legacy_user_gets_profile(self):
        self.user.customer_profile.delete()
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        self.assertTrue(Customer.objects.filter(user=self.user).exists())

    def test_anonymous_pages_are_protected(self):
        self.client.logout()
        for name, args in [('dashboard', []), ('plans', []), ('orders', []),
            ('invoices', []), ('account', []), ('select_plan', [self.plan.pk]),
            ('invoice', [1]), ('order_detail', [1]), ('order_confirm', [])]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302)
        self.assertEqual(self.client.post(reverse('order_confirm')).status_code, 302)

    def test_login_redirects_dashboard_and_logout_requires_post(self):
        self.client.logout()
        response = self.client.post(reverse('login'), {'username': 'alice', 'password': 'Test-Password-9821'})
        self.assertRedirects(response, reverse('dashboard'))
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)
        self.assertRedirects(self.client.post(reverse('logout')), reverse('home'))

    def test_monthly_order_and_invoice(self):
        response = self.checkout()
        order = Order.objects.get()
        self.assertRedirects(response, reverse('invoice', args=[order.pk]))
        self.assertEqual(order.amount, self.plan.monthly_price)
        self.assertEqual(order.invoice.amount, order.amount)
        self.assertEqual(order.status, 'PENDING')
        self.assertEqual(order.invoice.status, 'PENDING')
        self.assertEqual(order.invoice.invoice_number, f'INV-{order.pk:08d}')
        self.assertEqual(order.invoice.due_date, order.created_at + timedelta(days=1))
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Subscription.objects.exists())

    def test_yearly_order(self):
        self.checkout('YEARLY')
        self.assertEqual(Order.objects.get().amount, self.plan.yearly_price)
        self.assertEqual(Invoice.objects.get().amount, self.plan.yearly_price)

    def test_browser_cannot_set_amount_or_status_or_customer(self):
        review = self.review(amount='0.01', price='0.01')
        self.assertEqual(review.context['amount'], self.plan.monthly_price)
        self.client.post(reverse('order_confirm'), {'checkout_token': review.context['checkout_token'],
            'amount': '0.01', 'status': 'PAID', 'payment': 'success', 'customer': self.other.customer_profile.pk,
            'billing_cycle': 'YEARLY', 'plan_id': 999})
        order = Order.objects.get()
        self.assertEqual(order.customer, self.user.customer_profile)
        self.assertEqual(order.billing_cycle, 'MONTHLY')
        self.assertEqual(order.amount, self.plan.monthly_price)
        self.assertEqual(order.invoice.amount, order.amount)
        self.assertEqual(order.status, 'PENDING')
        self.assertEqual(order.invoice.status, 'PENDING')

    def test_invalid_cycles_rejected(self):
        for cycle in ['weekly', '', 'monthly', 'YEARLY<script>']:
            with self.subTest(cycle=cycle):
                self.assertEqual(self.review(cycle).status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_review_does_not_create_order(self):
        self.assertEqual(self.review().status_code, 200)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.client.get(reverse('order_confirm')).status_code, 405)

    def test_repeated_confirmation_creates_one_order_and_invoice(self):
        token = self.review().context['checkout_token']
        first = self.client.post(reverse('order_confirm'), {'checkout_token': token})
        second = self.client.post(reverse('order_confirm'), {'checkout_token': token})
        self.assertEqual(first.url, second.url)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_invoice_failure_rolls_back_order(self):
        token = self.review().context['checkout_token']
        with patch('billing.services.Invoice.objects.create', side_effect=IntegrityError('invoice failed')):
            with self.assertRaises(IntegrityError):
                self.client.post(reverse('order_confirm'), {'checkout_token': token})
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(self.client.post(reverse('order_confirm'), {'checkout_token': token}).status_code, 302)

    def test_modified_missing_and_expired_tokens_rejected(self):
        token = self.review().context['checkout_token']
        for invalid in ['', token + 'tampered']:
            self.assertEqual(self.client.post(reverse('order_confirm'), {'checkout_token': invalid}).status_code, 400)
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 3602):
            self.assertEqual(self.client.post(reverse('order_confirm'), {'checkout_token': token}).status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_review_token_bound_to_account(self):
        token = self.review().context['checkout_token']
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('order_confirm'), {'checkout_token': token}).status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_price_change_requires_review(self):
        token = self.review().context['checkout_token']
        VPSPlan.objects.filter(pk=self.plan.pk).update(monthly_price='399.00')
        response = self.client.post(reverse('order_confirm'), {'checkout_token': token})
        self.assertContains(response, 'price has changed', status_code=400)
        self.assertFalse(Order.objects.exists())

    def test_inactive_plan_cannot_be_purchased(self):
        token = self.review().context['checkout_token']
        VPSPlan.objects.filter(pk=self.plan.pk).update(is_active=False)
        self.assertNotContains(self.client.get(reverse('plans')), self.plan.name)
        self.assertEqual(self.client.get(reverse('select_plan', args=[self.plan.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('order_confirm'), {'checkout_token': token}).status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_other_customers_order_is_hidden(self):
        order = self.other_order()
        self.assertEqual(self.client.get(reverse('order_detail', args=[order.pk])).status_code, 404)

    def test_other_customers_invoice_is_hidden(self):
        order = self.other_order()
        self.assertEqual(self.client.get(reverse('invoice', args=[order.pk])).status_code, 404)

    def test_order_list_is_scoped(self):
        other = self.other_order()
        self.checkout()
        response = self.client.get(reverse('orders'))
        self.assertEqual(list(response.context['page_obj']), list(Order.objects.filter(customer__user=self.user)))
        self.assertNotContains(response, 'OTHER-INVOICE')
        self.assertNotContains(response, reverse('order_detail', args=[other.pk]))

    def test_invoice_list_is_scoped(self):
        self.other_order()
        self.checkout()
        response = self.client.get(reverse('invoices'))
        self.assertEqual(list(response.context['page_obj']), list(Invoice.objects.filter(order__customer__user=self.user)))
        self.assertNotContains(response, 'OTHER-INVOICE')

    def test_dashboard_scopes_orders_and_subscriptions(self):
        other = self.other_order()
        Subscription.objects.create(customer=self.other.customer_profile, order=other,
            start_date=timezone.now(), next_billing_date=timezone.now() + timedelta(days=30))
        response = self.client.get(reverse('dashboard'))
        for key in ['order_count', 'pending_count', 'active_count']:
            self.assertEqual(response.context[key], 0)
        self.checkout()
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['order_count'], 1)
        self.assertEqual(response.context['pending_count'], 1)
        self.assertNotContains(response, 'OTHER-INVOICE')

    def test_account_update_only_changes_own_profile(self):
        self.client.post(reverse('account'), {'full_name': 'Alice Updated', 'company': 'Example',
            'phone': '123', 'user': self.other.pk, 'id': self.other.customer_profile.pk})
        self.user.customer_profile.refresh_from_db()
        self.other.customer_profile.refresh_from_db()
        self.assertEqual(self.user.customer_profile.full_name, 'Alice Updated')
        self.assertEqual(self.other.customer_profile.full_name, 'bob')

    def test_csrf_required_for_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for name, args in [('order_confirm', []), ('select_plan', [self.plan.pk]),
                           ('account', []), ('logout', [])]:
            self.assertEqual(client.post(reverse(name, args=args), {}).status_code, 403)
        client.logout()
        self.assertEqual(client.post(reverse('register'), {}).status_code, 403)

    def test_pages_render_and_empty_states(self):
        for name in ['home', 'dashboard', 'plans', 'orders', 'invoices', 'account']:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertContains(self.client.get(reverse('orders')), 'No orders yet')
        self.assertContains(self.client.get(reverse('invoices')), 'No invoices yet')
        self.checkout()
        order = Order.objects.get()
        self.assertEqual(self.client.get(reverse('order_detail', args=[order.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse('invoice', args=[order.pk])), 'Online payments are currently unavailable.')

    def test_missing_invoice_returns_404(self):
        order = Order.objects.create(customer=self.user.customer_profile, plan=self.plan,
                                    billing_cycle='MONTHLY', amount='299.00')
        self.assertEqual(self.client.get(reverse('invoice', args=[order.pk])).status_code, 404)
        self.assertContains(self.client.get(reverse('orders')), 'Not issued')
