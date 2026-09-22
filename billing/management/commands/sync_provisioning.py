from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from billing.models import Order
from billing.provisioning import get_vps_provisioning_status, request_vps_provisioning


class Command(BaseCommand):
    help = 'Synchronize VM100 status, or explicitly dispatch/retry an already-paid order.'

    def add_arguments(self, parser):
        parser.add_argument('order_id', type=int)
        parser.add_argument('--retry', action='store_true', help='POST the same order ID and frozen payload again.')

    def handle(self, *args, **options):
        try:
            order = Order.objects.get(pk=options['order_id'])
            if options['retry']:
                order = request_vps_provisioning(order, retry=True)
            else:
                order = get_vps_provisioning_status(order)
        except Order.DoesNotExist:
            raise CommandError('Order not found.') from None
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages)) from None
        self.stdout.write(f'Order {order.pk}: {order.status}; provisioning: {order.provisioning_status or "not requested"}.')
        if order.provisioning_error:
            self.stdout.write(f'Provisioning result: {order.provisioning_error}.')
