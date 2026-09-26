from datetime import timedelta
from decimal import Decimal
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Invoice, Order, Subscription, VPSPlan, VPSPowerOperation
from .vps_control import (
    VPSControlError,
    VPSRuntime,
    get_vps_runtime,
    submit_power_operation,
    sync_power_operation,
)


TEST_SECRET = "unit-test-internal-api-secret-00000000000000000000"


@override_settings(
    DEBUG=True,
    PROVISIONING_API_URL="https://provisioner.invalid",
    BILLING_API_SECRET=TEST_SECRET,
)
class VPSControlAdapterTests(TestCase):
    def setUp(self):
        patcher = patch("requests.Session.request")
        self.http = patcher.start()
        self.addCleanup(patcher.stop)

    def test_runtime_status_uses_v1_schema_without_vmid(self):
        self.http.return_value = Mock(
            status_code=200,
            json=lambda: {
                "version": 1,
                "billing_order_id": 5,
                "state": "running",
                "observed_at": "2026-09-26T08:00:00+00:00",
                "ip_address": "220.100.130.210",
            },
        )
        result = get_vps_runtime(5)
        self.assertEqual(result.state, "running")
        self.assertEqual(result.ip_address, "220.100.130.210")
        args, kwargs = self.http.call_args
        self.assertEqual(args, ("GET", "https://provisioner.invalid/api/internal/v1/vps/5/"))
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {TEST_SECRET}")
        self.assertFalse(kwargs["allow_redirects"])

    def test_power_submission_uses_persisted_idempotency_key_and_v1_path(self):
        key = uuid.uuid4()
        operation_id = uuid.uuid4()
        self.http.return_value = Mock(
            status_code=202,
            json=lambda: {
                "version": 1,
                "billing_order_id": 5,
                "operation_id": str(operation_id),
                "action": "shutdown",
                "status": "pending",
                "result": None,
                "observed_state": "running",
                "observed_at": "2026-09-26T08:00:00+00:00",
                "error": None,
            },
        )
        result = submit_power_operation(5, "shutdown", key)
        self.assertEqual(result.operation_id, operation_id)
        args, kwargs = self.http.call_args
        self.assertEqual(args, ("POST", "https://provisioner.invalid/api/internal/v1/vps/5/power/"))
        self.assertEqual(kwargs["json"], {"action": "shutdown"})
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], str(key))

    def test_runtime_rejects_old_or_mismatched_schema(self):
        for data in (
            {"billing_order_id": 5, "vmid": 1006, "state": "running", "ip_address": ""},
            {
                "version": 1, "billing_order_id": 6, "state": "running",
                "observed_at": "2026-09-26T08:00:00+00:00", "ip_address": "",
            },
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
        runtime.return_value = VPSRuntime(
            "running", timezone.now(), "220.100.130.210"
        )
        response = self.client.get(reverse("vps_runtime_status", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "running")
        self.assertTrue(response.json()["available"])
        self.assertEqual(response.json()["vmid"], "1006")

        other = self._active_order(self.other, "1007", "220.100.130.211")
        self.assertEqual(
            self.client.get(reverse("vps_runtime_status", args=[other.pk])).status_code,
            404,
        )

    @patch("billing.views.sync_power_operation")
    def test_power_action_persists_idempotency_before_forwarding(self, sync):
        def mark_running(operation, force=False):
            self.assertTrue(VPSPowerOperation.objects.filter(pk=operation.pk).exists())
            self.assertIsInstance(operation.idempotency_key, uuid.UUID)
            operation.status = VPSPowerOperation.Status.RUNNING
            operation.remote_operation_id = uuid.uuid4()
            operation.save(update_fields=["status", "remote_operation_id", "updated_at"])
            return operation

        sync.side_effect = mark_running
        url = reverse("vps_power", args=[self.order.pk, "shutdown"])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)
        self.assertRedirects(response, reverse("vps_detail", args=[self.order.pk]))
        operation = VPSPowerOperation.objects.get(order=self.order)
        self.assertEqual(operation.action, "shutdown")
        sync.assert_called_once()
        self.assertTrue(sync.call_args.kwargs["force"])

    @patch("billing.views.sync_power_operation")
    def test_unresolved_operation_blocks_second_command(self, sync):
        VPSPowerOperation.objects.create(order=self.order, action="shutdown")
        response = self.client.post(reverse("vps_power", args=[self.order.pk, "reboot"]))
        self.assertRedirects(response, reverse("vps_detail", args=[self.order.pk]))
        self.assertEqual(VPSPowerOperation.objects.filter(order=self.order).count(), 1)
        sync.assert_not_called()

    @patch("billing.views.get_vps_runtime")
    @patch("billing.views.sync_power_operation")
    def test_runtime_poll_reconciles_existing_operation(self, sync, runtime):
        operation = VPSPowerOperation.objects.create(order=self.order, action="start")
        runtime.return_value = VPSRuntime("stopped", timezone.now(), "220.100.130.210")
        response = self.client.get(reverse("vps_runtime_status", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        sync.assert_called_once_with(operation)
        self.assertEqual(response.json()["operation"]["status"], "pending")

    @patch("billing.vps_control.submit_power_operation")
    def test_timeout_preserves_same_local_idempotency_key_for_retry(self, submit):
        operation = VPSPowerOperation.objects.create(order=self.order, action="start")
        original_key = operation.idempotency_key
        submit.side_effect = VPSControlError("TIMEOUT")
        with self.assertRaises(VPSControlError):
            sync_power_operation(operation, force=True)
        operation.refresh_from_db()
        self.assertEqual(operation.idempotency_key, original_key)
        self.assertEqual(operation.status, VPSPowerOperation.Status.PENDING)
        self.assertGreater(operation.sync_attempts, 0)

    @patch("billing.views.sync_power_operation")
    def test_invalid_action_never_reaches_vm100(self, sync):
        response = self.client.post(reverse("vps_power", args=[self.order.pk, "destroy"]))
        self.assertEqual(response.status_code, 400)
        sync.assert_not_called()

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
