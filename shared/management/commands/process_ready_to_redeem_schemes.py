from django.core.management.base import BaseCommand

from shared.services.ready_to_redeem_scheduler_service import process_ready_to_redeem_schemes


class Command(BaseCommand):
    help = (
        'Set SCHEME_STATUS READY_TO_REDEEM when all payable instalments are paid '
        'and start date + full tenure (e.g. 10+1 = 11 months) has elapsed.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--scheme-id', type=int, default=None, help='Single customer_scheme id')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        result = process_ready_to_redeem_schemes(
            customer_scheme_id=options['scheme_id'],
            dry_run=options['dry_run'],
            limit=options['limit'],
        )
        self.stdout.write(self.style.SUCCESS(str(result)))
