from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from billing.models import Invoice, Order
from billing.payments import confirm_test_payment


class Command(BaseCommand):
    help = 'TEST ONLY: record simulated payment and dispatch provisioning. Requires ALLOW_TEST_PAYMENT=True.'

    def add_arguments(self, parser):
        parser.add_argument('order_id', type=int)
        parser.add_argument('--confirm', action='store_true', help='Acknowledge this can request a real VPS.')

    def handle(self, *args, **options):
        if not settings.ALLOW_TEST_PAYMENT:
            raise CommandError('Test payment confirmation is disabled when ALLOW_TEST_PAYMENT=False.')
        if not options['confirm']:
            raise CommandError('TEST ONLY. This may create a real VPS. Pass --confirm to execute.')
        try:
            confirm_test_payment(options['order_id'])
            order = Order.objects.get(pk=options['order_id'])
        except (Order.DoesNotExist, Invoice.DoesNotExist):
            raise CommandError('Order or invoice not found.') from None
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages)) from None
        self.stdout.write(f'TEST ONLY payment recorded. Order {order.pk}: {order.status}.')
        if order.provisioning_error:
            self.stdout.write(f'Provisioning result: {order.provisioning_error}. Use sync_provisioning to reconcile.')
