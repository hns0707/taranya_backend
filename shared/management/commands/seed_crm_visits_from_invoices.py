"""
Create CRM visit rows from existing SaleInvoice records (Excel import, etc.).

Each invoice becomes one visit on the invoice date (converted — same-day sale).
Idempotent: skips invoices already seeded.

Usage (from Backend/ecom_backend, same venv as Daphne):
  python manage.py seed_crm_visits_from_invoices --dry-run
  python manage.py seed_crm_visits_from_invoices
"""
from __future__ import annotations

from datetime import datetime, time

from django.core.management.base import BaseCommand
from django.utils import timezone
from zoneinfo import ZoneInfo

from shared.models import CrmCustomerVisit, SaleInvoice

IST = ZoneInfo("Asia/Kolkata")
NOTE_PREFIX = "seeded_from_invoice:"


class Command(BaseCommand):
    help = "Seed CrmCustomerVisit rows from SaleInvoice (imported client data)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview only")
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max invoices to process (0 = all)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        qs = (
            SaleInvoice.objects.filter(is_deleted=False, customer_id__isnull=False)
            .exclude(customer_id=None)
            .select_related("customer")
            .order_by("invoice_date", "id")
        )
        if limit and limit > 0:
            qs = qs[:limit]

        created = 0
        skipped = 0
        no_customer = 0

        for inv in qs.iterator(chunk_size=200):
            if not inv.customer_id:
                no_customer += 1
                continue

            note_key = f"{NOTE_PREFIX}{inv.id}|"
            if CrmCustomerVisit.objects.filter(
                customer_id=inv.customer_id,
                notes__startswith=note_key,
            ).exists():
                skipped += 1
                continue

            inv_day = inv.invoice_date or timezone.localdate()
            visited_at = datetime.combine(inv_day, time(11, 0), tzinfo=IST)
            notes = f"{note_key}{inv.invoice_number or ''}"

            if dry_run:
                created += 1
                continue

            CrmCustomerVisit.objects.create(
                customer_id=inv.customer_id,
                branch_id=None,
                quote=None,
                catalogue_visit=None,
                visited_at=visited_at,
                source=CrmCustomerVisit.SOURCE_INVOICE,
                buy_next_time=False,
                notes=notes[:500],
            )
            created += 1

        mode = "DRY-RUN" if dry_run else "DONE"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] visits_created={created} skipped_existing={skipped} no_customer={no_customer}"
            )
        )
        if dry_run:
            self.stdout.write("Re-run without --dry-run to write visits.")
        else:
            self.stdout.write(
                "Restart Daphne, then open CRM Dashboard / Visit Tracking — Sample data should clear."
            )
