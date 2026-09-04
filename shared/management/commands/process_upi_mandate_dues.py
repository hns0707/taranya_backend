from django.core.management.base import BaseCommand

from shared.services.upi_mandate_scheduler_service import process_upi_mandate_dues


class Command(BaseCommand):
    help = 'Send ICICI MandateNotification (T-1) and ExecuteMandate on debit_date for approved UPI mandates.'

    def add_arguments(self, parser):
        parser.add_argument('--mandate-id', type=int, default=None, help='Process a single mandate id')
        parser.add_argument('--dry-run', action='store_true', help='Log actions without calling ICICI')
        parser.add_argument('--notify-only', action='store_true', help='Only send notifications')
        parser.add_argument('--execute-only', action='store_true', help='Only execute debits')
        parser.add_argument('--limit', type=int, default=200)

    def handle(self, *args, **options):
        result = process_upi_mandate_dues(
            mandate_id=options['mandate_id'],
            dry_run=options['dry_run'],
            notify_only=options['notify_only'],
            execute_only=options['execute_only'],
            limit=options['limit'],
        )
        self.stdout.write(self.style.SUCCESS(str(result)))
