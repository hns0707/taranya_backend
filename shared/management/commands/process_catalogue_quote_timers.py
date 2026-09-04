from django.core.management.base import BaseCommand

from shared.services.catalogue_quote_timer_service import process_all_due_quote_timers


class Command(BaseCommand):
    help = (
        'Expire negotiated catalogue pricing (3h) and release stock on draft quotes '
        'past valid_until (end of IST day / midnight) by marking them expired.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit',
            type=int,
            default=500,
            help='Max quotes to process per timer type (default 500).',
        )

    def handle(self, *args, **options):
        result = process_all_due_quote_timers(limit=options['limit'])
        self.stdout.write(
            self.style.SUCCESS(
                f"Pricing reverted: {result['pricing_reverted']}; "
                f"Stock released (expired): {result['stock_released']}"
            )
        )
