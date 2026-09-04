"""
CRM visit recording and lost-customer helpers.

Visit = catalogue / barcode product enquiry (opens with quotation visit).
Lost  = visit local date with no SaleInvoice for that customer the same day.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import QuerySet
from django.utils import timezone

from shared.models import Branch, CrmCustomerVisit, CatalogueQuote, CatalogueQuoteVisit, SaleInvoice

IST = ZoneInfo('Asia/Kolkata')


def local_day_bounds(day, tz=IST):
    """Return timezone-aware [start, end) for a calendar day in `tz`."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def customer_has_sale_on_day(customer_id: int, day, tz=IST) -> bool:
    start, end = local_day_bounds(day, tz)
    return SaleInvoice.objects.filter(
        customer_id=customer_id,
        is_deleted=False,
        invoice_date=day,
    ).exists() or SaleInvoice.objects.filter(
        customer_id=customer_id,
        is_deleted=False,
        system_created_at__gte=start,
        system_created_at__lt=end,
    ).exists()


def record_crm_visit_from_quote(
    quote: CatalogueQuote,
    *,
    catalogue_visit: CatalogueQuoteVisit | None = None,
    primary_user=None,
    branch_id: int | None = None,
    source: str = CrmCustomerVisit.SOURCE_CATALOGUE,
) -> CrmCustomerVisit:
    """Idempotent: one CRM visit per catalogue quote visit / quote."""
    existing = None
    if catalogue_visit is not None:
        existing = CrmCustomerVisit.objects.filter(catalogue_visit=catalogue_visit).first()
    if existing is None and quote is not None:
        existing = CrmCustomerVisit.objects.filter(quote_id=quote.id).first()
    if existing:
        updates = []
        if branch_id and existing.branch_id != branch_id:
            if Branch.objects.filter(id=branch_id, is_active=True).exists():
                existing.branch_id = branch_id
                updates.append('branch')
        if catalogue_visit and existing.catalogue_visit_id is None:
            existing.catalogue_visit = catalogue_visit
            updates.append('catalogue_visit')
        if updates:
            existing.updated_by = primary_user
            updates.extend(['updated_by', 'system_updated_at'])
            existing.save(update_fields=updates)
        return existing

    branch = None
    if branch_id:
        branch = Branch.objects.filter(id=branch_id, is_active=True).first()

    visited_at = timezone.now()
    if catalogue_visit and catalogue_visit.system_created_at:
        visited_at = catalogue_visit.system_created_at

    return CrmCustomerVisit.objects.create(
        customer_id=quote.customer_id,
        branch=branch,
        quote=quote,
        catalogue_visit=catalogue_visit,
        visited_at=visited_at,
        source=source or CrmCustomerVisit.SOURCE_CATALOGUE,
        created_by=primary_user,
        updated_by=primary_user,
    )


def filter_visits(
    *,
    branch_id: int | None = None,
    customer_id: int | None = None,
    start=None,
    end=None,
) -> QuerySet:
    qs = CrmCustomerVisit.objects.select_related(
        'customer', 'branch', 'quote', 'created_by',
    )
    if branch_id:
        qs = qs.filter(branch_id=branch_id)
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    if start is not None:
        qs = qs.filter(visited_at__gte=start)
    if end is not None:
        qs = qs.filter(visited_at__lt=end)
    return qs


def annotate_visit_outcome(visit: CrmCustomerVisit, tz=IST) -> dict:
    local_dt = (
        visit.visited_at.astimezone(tz)
        if timezone.is_aware(visit.visited_at)
        else visit.visited_at.replace(tzinfo=tz)
    )
    day = local_dt.date()
    converted = customer_has_sale_on_day(visit.customer_id, day, tz)
    # Lost = visit with no SaleInvoice on the same local day (incl. today until they buy).
    outcome = 'converted' if converted else 'lost'

    return {
        'id': visit.id,
        'customer_id': visit.customer_id,
        'customer_name': visit.customer.full_name if visit.customer else '',
        'customer_mobile': visit.customer.mobile if visit.customer else '',
        'customer_code': visit.customer.customer_code if visit.customer else '',
        'branch_id': visit.branch_id,
        'branch_name': visit.branch.name if visit.branch else None,
        'quote_id': visit.quote_id,
        'quote_number': visit.quote.quote_number if visit.quote else None,
        'visited_at': visit.visited_at.isoformat(),
        'visit_date': day.isoformat(),
        'source': visit.source,
        'buy_next_time': visit.buy_next_time,
        'outcome': outcome,
        'converted': converted,
        'lost': not converted,
        'handled_by': (
            visit.created_by.full_name if visit.created_by else None
        ),
    }


def is_lost_visit(visit: CrmCustomerVisit, tz=IST) -> bool:
    return annotate_visit_outcome(visit, tz)['lost']
