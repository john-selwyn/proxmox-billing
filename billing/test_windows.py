"""Windows checkout, credential authorization, and backend contract regressions."""
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import BillingCycleForm
from .models import Invoice, Order, Payment, VPSPlan
from .rdp_endpoint import parse_rdp_endpoint
from .services import confirm_order
from .provisioning import _finish
from .provisioning_api import ProvisioningResult
from .vps_control import VPSControlError, _parse_runtime
from .windows_access import get_windows_credentials


ACCESS = {"host": "proxmoxportal.dyndns.org", "port": 22016, "username": "Admin"}
PASSWORD = "Example-Only-Password-123!"


@override_settings(WINDOWS_ORDERING_ENABLED=True, ALLOW_TEST_PAYMENT=False)
class WindowsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("windows-owner")
        self.other = User.objects.create_user("other-owner")
        self.plan = VPSPlan.objects.create(name="Windows", cpu=2, ram=4, storage=64,
                                          monthly_price="299.00", yearly_price="2990.00")
        self.client.force_login(self.user)

    def order(self):
        order = Order.objects.create(customer=self.user.customer_profile, plan=self.plan,
            billing_cycle="MONTHLY", amount="299.00", operating_system="Windows 11",
            status="ACTIVE", rdp_host=ACCESS["host"], rdp_port=ACCESS["port"])
        invoice = Invoice.objects.create(order=order, invoice_number=f"WIN-{order.pk}",
            amount=order.amount, status="PAID", paid_at=timezone.now(), due_date=timezone.now())
        Payment.objects.create(invoice=invoice, provider="XENDIT", transaction_id=f"PAY-{order.pk}",
            amount=order.amount, status="SUCCESS", paid_at=timezone.now())
        return order

    def test_windows_checkout_requires_no_ssh_key(self):
        response = self.client.post(reverse("select_plan", args=[self.plan.pk]),
            {"operating_system": "Windows 11", "billing_cycle": "MONTHLY"})
        self.assertEqual(response.status_code, 200)
        token = response.context["checkout_token"]
        response = self.client.post(reverse("order_confirm"), {"checkout_token": token})
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.operating_system, "Windows 11")
        self.assertEqual((order.ssh_username, order.ssh_public_key), ("", ""))

    def test_minimum_resources_are_checked_individually(self):
        for field, value in (("cpu", 1), ("ram", 3), ("storage", 63)):
            with self.subTest(field=field):
                original = getattr(self.plan, field)
                setattr(self.plan, field, value)
                form = BillingCycleForm({"operating_system": "Windows 11", "billing_cycle": "MONTHLY"}, plan=self.plan)
                self.assertFalse(form.is_valid())
                setattr(self.plan, field, original)

    def test_linux_still_requires_key(self):
        form = BillingCycleForm({"operating_system": "Debian 13", "billing_cycle": "MONTHLY"}, plan=self.plan)
        self.assertFalse(form.is_valid())
        self.assertIn("ssh_public_key", form.errors)

    @override_settings(WINDOWS_ORDERING_ENABLED=False)
    def test_disabled_windows_cannot_be_submitted_or_confirmed(self):
        form = BillingCycleForm({"operating_system": "Windows 11", "billing_cycle": "MONTHLY"}, plan=self.plan)
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError):
            confirm_order(customer=self.user.customer_profile, plan_id=self.plan.pk,
                cycle="MONTHLY", checkout_token=uuid.uuid4(), reviewed_amount="299.00",
                operating_system="Windows 11")
        self.assertFalse(Order.objects.exists())

    def test_owner_reveal_is_uncached_and_password_is_not_stored(self):
        order = self.order()
        with patch("billing.views.get_windows_credentials", return_value={**ACCESS, "password": PASSWORD}) as fetch:
            response = self.client.post(reverse("windows_credentials", args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rdp_access"]["password"], PASSWORD)
        self.assertIn("no-store", response["Cache-Control"])
        fetch.assert_called_once_with(order.pk)
        order.refresh_from_db()
        self.assertNotIn(PASSWORD, repr(order.__dict__))

    def test_other_customer_cannot_reveal(self):
        order = self.order()
        self.client.force_login(self.other)
        with patch("billing.views.get_windows_credentials") as fetch:
            response = self.client.post(reverse("windows_credentials", args=[order.pk]))
        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()

    def test_reveal_requires_payment_evidence(self):
        order = self.order()
        order.invoice.payments.all().delete()
        with patch("billing.views.get_windows_credentials") as fetch:
            response = self.client.post(reverse("windows_credentials", args=[order.pk]))
        self.assertEqual(response.status_code, 403)
        fetch.assert_not_called()

    def test_reveal_requires_post_login_and_csrf(self):
        order = self.order()
        url = reverse("windows_credentials", args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)

    def test_linux_pending_and_unpaid_orders_cannot_reveal(self):
        order = self.order()
        for updates in ({"operating_system": "Debian 13"}, {"status": "PROVISIONING"}):
            Order.objects.filter(pk=order.pk).update(**updates)
            with patch("billing.views.get_windows_credentials") as fetch:
                self.assertEqual(self.client.post(reverse("windows_credentials", args=[order.pk])).status_code, 404)
            fetch.assert_not_called()
            Order.objects.filter(pk=order.pk).update(operating_system="Windows 11", status="ACTIVE")
        order.invoice.status = "PENDING"
        order.invoice.save()
        self.assertEqual(self.client.post(reverse("windows_credentials", args=[order.pk])).status_code, 404)

    def test_backend_failure_does_not_expose_details(self):
        order = self.order()
        with patch("billing.views.get_windows_credentials", side_effect=VPSControlError("INTERNAL_SECRET")):
            response = self.client.post(reverse("windows_credentials", args=[order.pk]))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("INTERNAL_SECRET", response.content.decode())

    def test_credentials_contract_checks_order_and_endpoint(self):
        data = {"billing_order_id": 17, "rdp_access": {**ACCESS, "password": PASSWORD}}
        with patch("billing.windows_access._request", return_value=(200, data)):
            self.assertEqual(get_windows_credentials(17), data["rdp_access"])
            with self.assertRaises(VPSControlError):
                get_windows_credentials(18)
        for invalid in ({"password": "bad\nvalue"}, {"username": "root"}, {"port": True}, {"host": "https://bad/path"}):
            with self.subTest(invalid=invalid):
                altered = {**data, "rdp_access": {**data["rdp_access"], **invalid}}
                with patch("billing.windows_access._request", return_value=(200, altered)):
                    with self.assertRaises(VPSControlError):
                        get_windows_credentials(17)

    def test_status_contract_never_accepts_password(self):
        self.assertEqual(parse_rdp_endpoint({"rdp_access": ACCESS}), (ACCESS["host"], ACCESS["port"]))
        self.assertEqual(parse_rdp_endpoint({}), (None, None))
        self.assertEqual(parse_rdp_endpoint({"rdp_access": None}), ("", 3389))
        with self.assertRaises(ValueError):
            parse_rdp_endpoint({"rdp_access": {**ACCESS, "password": PASSWORD}})
        data = {"version": 1, "billing_order_id": 17, "state": "running",
                "observed_at": timezone.now().isoformat(), "ip_address": "10.60.0.202", "rdp_access": ACCESS}
        self.assertEqual(_parse_runtime(data, 17).rdp_port, ACCESS["port"])
        with self.assertRaises(VPSControlError):
            _parse_runtime({**data, "rdp_access": {**ACCESS, "password": PASSWORD}}, 17)

    def test_management_page_does_not_fetch_password(self):
        order = self.order()
        with patch("billing.views.get_windows_credentials") as fetch:
            response = self.client.get(reverse("vps_detail", args=[order.pk]))
        self.assertContains(response, "Windows 11")
        self.assertContains(response, "Show initial password")
        self.assertContains(response, f'{ACCESS["host"]}:{ACCESS["port"]}')
        self.assertNotContains(response, "SSH port:")
        self.assertNotContains(response, PASSWORD)
        fetch.assert_not_called()

    def test_provisioning_preserves_missing_endpoint_and_clears_explicit_null(self):
        order = self.order()
        for endpoint, expected in ((None, ACCESS["host"]), ("", ""), (ACCESS["host"], ACCESS["host"])):
            lease = uuid.uuid4()
            Order.objects.filter(pk=order.pk).update(provisioning_lease=lease)
            result = ProvisioningResult("ACTIVE", rdp_host=endpoint,
                                        rdp_port=ACCESS["port"] if endpoint else 3389)
            _finish(order.pk, lease, result)
            order.refresh_from_db()
            self.assertEqual(order.rdp_host, expected)
