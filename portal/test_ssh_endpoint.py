from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Invoice, Order, VPSPlan
from .ssh_endpoint import parse_ssh_endpoint
from .vps_control import VPSControlError, VPSRuntime, _parse_runtime


class SSHEndpointTests(TestCase):
    def test_optional_contract_and_invalid_endpoints(self):
        self.assertEqual(parse_ssh_endpoint({}), (None, None))
        self.assertEqual(parse_ssh_endpoint({'ssh_access': None}), ('', 22))
        self.assertEqual(parse_ssh_endpoint({'ssh_access': {'host': 'proxmoxportal.dyndns.org', 'port': 22015}}),
                         ('proxmoxportal.dyndns.org', 22015))
        for endpoint in ([], {}, {'host': 'host', 'port': True}, {'host': 'host', 'port': '22015'},
                         {'host': 'host;id', 'port': 22015}, {'host': '-oProxyCommand=x', 'port': 22015},
                         {'host': 'host', 'port': 65536}, {'host': 'host', 'port': 0},
                         {'host': 'host', 'port': 22, 'vmid': 1016}):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                parse_ssh_endpoint({'ssh_access': endpoint})

    def test_runtime_preserves_v1_validation_with_optional_endpoint(self):
        data = {'version': 1, 'billing_order_id': 17, 'state': 'running',
                'observed_at': timezone.now().isoformat(), 'ip_address': '10.60.0.15'}
        self.assertIsNone(_parse_runtime(data, 17).ssh_host)
        runtime = _parse_runtime({**data, 'ssh_access': {'host': 'proxmoxportal.dyndns.org', 'port': 22015}}, 17)
        self.assertEqual((runtime.ssh_host, runtime.ssh_port), ('proxmoxportal.dyndns.org', 22015))
        with self.assertRaises(VPSControlError):
            _parse_runtime({**data, 'ssh_access': {'host': 'host', 'port': True}}, 17)
        with self.assertRaises(VPSControlError):
            _parse_runtime({**data, 'ssh_access': None, 'vmid': 1016}, 17)

    def make_order(self):
        user = User.objects.create_user('ssh-owner')
        plan = VPSPlan.objects.create(name='SSH VPS', cpu=2, ram=2, storage=32,
            monthly_price=Decimal('299.00'), yearly_price=Decimal('2990.00'))
        order = Order.objects.create(customer=user.customer_profile, plan=plan, status='ACTIVE',
            billing_cycle='MONTHLY', amount='299.00', ssh_username='vpsuser',
            provisioning_ip_address='10.60.0.15', provisioning_vmid='1016')
        Invoice.objects.create(order=order, invoice_number='SSH-TEST', amount=order.amount,
            status='PAID', paid_at=timezone.now(), due_date=timezone.now())
        return user, order

    def test_customer_template_uses_public_host_and_port_with_key_guidance(self):
        user, order = self.make_order()
        order.ssh_host, order.ssh_port = 'proxmoxportal.dyndns.org', 22015
        html = render_to_string('portal/vps_detail.html', {'order': order})
        self.assertIn('ssh -p 22015 vpsuser@proxmoxportal.dyndns.org', html)
        self.assertNotIn('ssh vpsuser@10.60.0.15', html)
        self.assertIn('Private IP', html)
        self.assertIn('-i', html)
        order.ssh_host = ''
        html = render_to_string('portal/vps_detail.html', {'order': order})
        self.assertIn('Preparing remote access', html)

    @override_settings(BILLING_POWER_CONTROLS_ENABLED=True)
    @patch('portal.views.get_vps_runtime')
    def test_runtime_sync_is_customer_scoped_and_retains_legacy_endpoint(self, runtime):
        user, order = self.make_order()
        runtime.return_value = VPSRuntime('running', timezone.now(), '10.60.0.15',
            ssh_host='proxmoxportal.dyndns.org', ssh_port=22015)
        self.client.force_login(user)
        url = reverse('vps_runtime_status', args=[order.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual((order.ssh_host, order.ssh_port), ('proxmoxportal.dyndns.org', 22015))
        runtime.return_value = VPSRuntime('running', timezone.now(), '10.60.0.15')
        self.client.get(url)
        order.refresh_from_db()
        self.assertEqual(order.ssh_port, 22015)
        stranger = User.objects.create_user('other-ssh-customer')
        self.client.force_login(stranger)
        runtime.reset_mock()
        self.assertEqual(self.client.get(url).status_code, 404)
        runtime.assert_not_called()
