from django.core.management.base import BaseCommand, CommandError
from billing.checkouts import reconcile_checkout
from billing.models import XenditCheckout
from billing.xendit import XenditError


class Command(BaseCommand):
    help = 'Reconcile an ambiguous Xendit checkout using its reference and provider session ID; never marks paid.'

    def add_arguments(self, parser):
        parser.add_argument('reference_id')
        parser.add_argument('payment_session_id')

    def handle(self, *args, **options):
        try:
            checkout = reconcile_checkout(options['reference_id'], options['payment_session_id'])
        except (XenditError, XenditCheckout.DoesNotExist):
            raise CommandError('CHECKOUT_RECONCILIATION_FAILED') from None
        self.stdout.write(f'Checkout {checkout.pk}: {checkout.status}. Completed payments require webhook verification.')
