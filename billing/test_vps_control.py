from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Invoice, Order, Subscription, VPSPlan
from .vps_control import VPSControlError, VPSRuntime, get_vps_runtime, power_vps


@override_settings(
    DEBUG=True,
    PROVISIONING_API_URL="https://provisioner.invalid",
    BILLING_API_SECRET="unit-test-secret-only",
)
class VPSControlAdapterTests(TestCase):
    def setUp(self):
        patcher = patch("requests.Session.request")
        self.http = patcher.start()
        self.addCleanup(patcher.stop)

    def test_runtime_status_uses_internal_auth_and_validates_response(self):
        self.http.return_value = Mock(
            status_code=200,
            json=lambda: {
                "billing_order_id": 5,
                "vmid": 1006,
                "state": "running",
                "ip_address": "220.100.130.210",
            },
        )
        result = get_vps_runtime(5)
        self.assertEqual(result, VPSRuntime("running", "1006", "220.100.130.210"))
        args, kwargs = self.http.call_args
        self.assertEqual(args, ("GET", "https://provisioner.invalid/api/internal/vps/5/"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer unit-test-secret-only")
        self.assertFalse(kwargs["allow_redirects"])

    def test_power_action_posts_only_whitelisted_action(self):
        self.http.return_value = Mock(
            status_code=202,
            json=lambda: {
                "accepted": True,
                "action": "shutdown",
                "vmid": 1006,
                "state": "running",
                "changed": True,
            },
        )
        result = power_vps(5, "shutdown")
        self.assertTrue(result["accepted"])
        args, kwargs = self.http.call_args
        self.assertEqual(args, ("POST", "https://provisioner.invalid/api/internal/vps/5/power/"))
        self.assertEqual(kwargs["json"], {"action": "shutdown"})

        with self.assertRaises(VPSControlError) as context:
            power_vps(5, "destroy")
        self.assertEqual(context.exception.code, "INVALID_ACTION")

    def test_runtime_rejects_mismatched_order_and_bad_ip(self):
        for data in (
            {"billing_order_id": 6, "vmid": 1006, "state": "running", "ip_address": ""},
            {"billing_order_id": 5, "vmid": 1006, "state": "running", "ip_address": "not-an-ip"},
        ):
            with self.subTest(data=data):
                self.http.return_value = Mock(status_code=200, json=lambda data=data: data)
                with self.assertRaises(VPSControlError):
                    get_vps_runtime(5)


class VPSControlViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", password="Test-Password-9821")
        cls.other = User.objects.create_user("bob", password="Test-Password-9821")
        cls.plan = VPSPlan.objects.create(
            name="Compute Control",
            cpu=2,
            ram=4,
            storage=60,
            monthly_price=Decimal("299.00"),
            yearly_price=Decimal("2990.00"),
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.order = self._active_order(self.user, "1006", "220.100.130.210")

    def _active_order(self, user, vmid, ip):
        order = Order.objects.create(
            customer=user.customer_profile,
            plan=self.plan,
            billing_cycle=Order.BillingCycle.MONTHLY,
            amount=Decimal("299.00"),
            status=Order.Status.ACTIVE,
            provisioning_status="ACTIVE",
            provisioning_vps_id="10",
            provisioning_vmid=vmid,
            provisioning_progress=100,
            provisioning_step="VPS is ready.",
            provisioning_ip_address=ip,
            provisioning_payload={"os": "Ubuntu 26.04"},
        )
        Invoice.objects.create(
            order=order,
            invoice_number=f"CONTROL-{order.pk}",
            amount=order.amount,
            status=Invoice.Status.PAID,
            paid_at=timezone.now(),
            due_date=timezone.now() + timedelta(days=1),
        )
        Subscription.objects.create(
            customer=user.customer_profile,
            order=order,
            start_date=timezone.now(),
            next_billing_date=timezone.now() + timedelta(days=30),
            status=Subscription.Status.ACTIVE,
        )
        return order

    @patch("billing.views.get_vps_runtime")
    def test_runtime_endpoint_is_customer_scoped(self, runtime):
        runtime.return_value = VPSRuntime("running", "1006", "220.100.130.210")
        response = self.client.get(reverse("vps_runtime_status", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "running")
        self.assertTrue(response.json()["available"])

        other = self._active_order(self.other, "1007", "220.100.130.211")
        self.assertEqual(
            self.client.get(reverse("vps_runtime_status", args=[other.pk])).status_code,
            404,
        )

    @patch("billing.views.power_vps")
    def test_power_action_is_post_only_scoped_and_forwarded(self, power):
        power.return_value = {
            "accepted": True,
            "action": "shutdown",
            "vmid": 1006,
            "state": "running",
            "changed": True,
        }
        url = reverse("vps_power", args=[self.order.pk, "shutdown"])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)
        self.assertRedirects(response, reverse("vps_detail", args=[self.order.pk]))
        power.assert_called_once_with(self.order.pk, "shutdown")

        other = self._active_order(self.other, "1007", "220.100.130.211")
        self.assertEqual(
            self.client.post(reverse("vps_power", args=[other.pk, "start"])).status_code,
            404,
        )

    @patch("billing.views.power_vps")
    def test_invalid_action_never_reaches_vm100(self, power):
        response = self.client.post(reverse("vps_power", args=[self.order.pk, "destroy"]))
        self.assertEqual(response.status_code, 400)
        power.assert_not_called()

    def test_power_action_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post(reverse("vps_power", args=[self.order.pk, "shutdown"])).status_code,
            403,
        )

    def test_manage_page_shows_live_power_controls(self):
        response = self.client.get(reverse("vps_detail", args=[self.order.pk]))
        self.assertContains(response, "Power controls")
        self.assertContains(response, "Start")
        self.assertContains(response, "Shutdown")
        self.assertContains(response, "Reboot")
        self.assertContains(response, reverse("vps_runtime_status", args=[self.order.pk]))
