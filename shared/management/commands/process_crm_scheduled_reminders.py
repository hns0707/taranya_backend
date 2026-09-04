from django.core.management.base import BaseCommand

from shared.services.crm_reminder_service import process_due_scheduled_reminders


class Command(BaseCommand):
    help = "Process due CRM scheduled reminders (WhatsApp / SMS / Call queue)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Max reminders to process (default 100).",
        )

    def handle(self, *args, **options):
        result = process_due_scheduled_reminders(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Processed={result['processed']} sent={result['sent']} "
                f"failed={result['failed']} skipped={result['skipped']} "
                f"call_queued={result['call_queued']}"
            )
        )
