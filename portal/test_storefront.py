from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from .models import VPSPlan, Order, Invoice


class StorefrontTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plan = VPSPlan.objects.create(name='Public plan', cpu=2, ram=4, storage=60,
            monthly_price=Decimal('299.00'), yearly_price=Decimal('2990.00'))
        VPSPlan.objects.create(name='Retired plan', cpu=1, ram=1, storage=20,
            monthly_price=Decimal('99.00'), yearly_price=Decimal('990.00'), is_active=False)

    def test_public_pages_and_real_plan_prices(self):
        for page in ('home', 'plans', 'web_hosting'):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'ICT Ventures')
        response = self.client.get(reverse('plans'))
        self.assertContains(response, 'Public plan')
        self.assertContains(response, '299.00')
        self.assertNotContains(response, 'Retired plan')
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Invoice.objects.exists())

    def test_ordering_still_requires_login_and_preserves_destination(self):
        url = reverse('select_plan', args=[self.plan.pk])
        for response in (self.client.get(url), self.client.post(url, {'billing_cycle': 'MONTHLY'})):
            self.assertRedirects(response, reverse('login') + '?next=' + url)
        self.assertFalse(Order.objects.exists())

    def test_web_hosting_does_not_offer_checkout(self):
        response = self.client.get(reverse('web_hosting'))
        self.assertContains(response, 'Starter')
        self.assertContains(response, '₱149.00')
        self.assertNotContains(response, '<form')
        self.assertEqual(self.client.post(reverse('web_hosting')).status_code, 405)

    @patch('portal.hestia.requests.post')
    def test_web_hosting_uses_live_hestia_package_limits(self, post):
        post.return_value.json.return_value = {
            'starter': {'DISK_QUOTA': '4096', 'BANDWIDTH': '40960', 'WEB_DOMAINS': '2', 'DATABASES': '3', 'CRON_JOBS': '6', 'MAIL_ACCOUNTS': '1'},
        }
        post.return_value.raise_for_status.return_value = None
        with self.settings(HESTIA_API_URL='https://hestia.test/api/', HESTIA_API_USER='selwyn', HESTIA_API_PASSWORD='secret'):
            response = self.client.get(reverse('web_hosting'))
        self.assertContains(response, '4 GB')
        self.assertContains(response, '40 GB')
        self.assertContains(response, 'Live package limits from Hestia')
        post.assert_called_once()

    def test_empty_catalog(self):
        VPSPlan.objects.update(is_active=False)
        self.assertContains(self.client.get(reverse('plans')), 'No VPS plans are currently available')
