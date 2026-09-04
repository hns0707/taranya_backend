"""
CRM Insights APIs (Phase 3).

GET /master/crm/insights/wishlist-trends/       — most wished products
GET /master/crm/insights/customer-demographics/ — age groups + upcoming events
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from shared.models import CatalogueQuote, CatalogueQuoteLine, CrmCustomerVisit, Customer

READ_AUTH = (
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_VIEW',
    'CRM_CUSTOMER_LIST_VIEW',
    'CRM_CUSTOMER_LIST',
)

# Ticket bands: 0-10, 10-20, 20-25, 25-30, then 5-year steps, 60+
AGE_BANDS = [
    (0, 10, '0-10'),
    (10, 20, '10-20'),
    (20, 25, '20-25'),
    (25, 30, '25-30'),
    (30, 35, '30-35'),
    (35, 40, '35-40'),
    (40, 45, '40-45'),
    (45, 50, '45-50'),
    (50, 55, '50-55'),
    (55, 60, '55-60'),
    (60, 200, '60+'),
]


def _parse_int(val):
    if val is None or val == '':
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _age_years(dob: date, today: date) -> int:
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return max(0, years)


def _band_for_age(age: int) -> str:
    for lo, hi, label in AGE_BANDS:
        if lo <= age < hi:
            return label
    return '60+'


def _next_occurrence(event_date: date, today: date) -> date:
    """Next calendar occurrence of month/day on or after today."""
    try:
        candidate = event_date.replace(year=today.year)
    except ValueError:
        candidate = date(today.year, event_date.month, 28)
    if candidate < today:
        try:
            candidate = event_date.replace(year=today.year + 1)
        except ValueError:
            candidate = date(today.year + 1, event_date.month, 28)
    return candidate


def _scoped_customers(branch_id=None, customer_id=None):
    qs = Customer.objects.filter(is_active=True)
    if customer_id:
        return qs.filter(id=customer_id)
    if branch_id:
        ids = (
            CrmCustomerVisit.objects.filter(branch_id=branch_id)
            .values_list('customer_id', flat=True)
            .distinct()
        )
        qs = qs.filter(id__in=ids)
    return qs


def _build_age_groups(customers_qs, today: date):
    counts = {label: 0 for _, _, label in AGE_BANDS}
    with_dob = 0
    for dob in customers_qs.exclude(date_of_birth__isnull=True).values_list('date_of_birth', flat=True):
        if not dob:
            continue
        with_dob += 1
        counts[_band_for_age(_age_years(dob, today))] += 1
    return {
        'with_dob': with_dob,
        'bands': [{'label': label, 'count': counts[label]} for _, _, label in AGE_BANDS],
    }


def _build_upcoming_events(customers_qs, today: date, days: int, limit: int = 100):
    events = []
    end = today + timedelta(days=days)
    qs = customers_qs.only(
        'id', 'full_name', 'mobile', 'customer_code',
        'date_of_birth', 'anniversary_date', 'wedding_date',
    )
    for c in qs.iterator(chunk_size=500):
        for kind, field in (
            ('birthday', c.date_of_birth),
            ('anniversary', c.anniversary_date),
            ('wedding', c.wedding_date),
        ):
            if not field:
                continue
            nxt = _next_occurrence(field, today)
            if today <= nxt <= end:
                events.append({
                    'customer_id': c.id,
                    'customer_name': c.full_name,
                    'customer_mobile': c.mobile,
                    'customer_code': c.customer_code,
                    'event_type': kind,
                    'event_date': nxt.isoformat(),
                    'days_until': (nxt - today).days,
                    'original_date': field.isoformat(),
                })
    events.sort(key=lambda e: (e['days_until'], e['customer_name'] or ''))
    return events[:limit]


@api_view(['GET'])
@admin_auth(*READ_AUTH)
def crm_wishlist_trends(request):
    """
    Aggregate catalogue quote lines that count as wishlist intent:
    - lines on draft quotes, or
    - lines on quotes linked to a buy_next_time CRM visit
    """
    days = _parse_int(request.GET.get('days')) or 90
    days = max(7, min(days, 365))
    limit = _parse_int(request.GET.get('limit')) or 15
    limit = max(5, min(limit, 50))
    branch_id = _parse_int(request.GET.get('branch_id') or request.GET.get('store_id'))
    force_demo = (request.GET.get('demo') or '').strip().lower() in ('1', 'true', 'yes')
    force_real = (request.GET.get('demo') or '').strip().lower() in ('0', 'false', 'no')

    since = timezone.now() - timedelta(days=days)

    buy_next_quote_ids = list(
        CrmCustomerVisit.objects.filter(buy_next_time=True, quote_id__isnull=False)
        .values_list('quote_id', flat=True)
        .distinct()
    )

    line_filter = Q(is_removed=False) & Q(quote__system_created_at__gte=since) & (
        Q(quote__status=CatalogueQuote.STATUS_DRAFT) | Q(quote_id__in=buy_next_quote_ids)
    )
    if branch_id:
        branch_buy_next_quote_ids = list(
            CrmCustomerVisit.objects.filter(
                branch_id=branch_id,
                buy_next_time=True,
                quote_id__isnull=False,
            )
            .values_list('quote_id', flat=True)
            .distinct()
        )
        line_filter = Q(is_removed=False) & Q(quote__system_created_at__gte=since) & Q(
            quote_id__in=branch_buy_next_quote_ids
        )

    rows = list(
        CatalogueQuoteLine.objects.filter(line_filter)
        .values('product_id', 'product_name', 'design_code')
        .annotate(wish_count=Count('id'), customers=Count('quote__customer_id', distinct=True))
        .order_by('-wish_count', 'product_name')[:limit]
    )

    if force_demo or (not rows and not force_real):
        demo = [
            {'product_id': 'P-DEMO-01', 'product_name': 'Classic Gold Bangle', 'design_code': 'BG-101', 'wish_count': 12, 'customers': 8},
            {'product_id': 'P-DEMO-02', 'product_name': 'Diamond Stud Earrings', 'design_code': 'ER-220', 'wish_count': 9, 'customers': 7},
            {'product_id': 'P-DEMO-03', 'product_name': 'Temple Necklace Set', 'design_code': 'NK-088', 'wish_count': 7, 'customers': 5},
            {'product_id': 'P-DEMO-04', 'product_name': 'Rose Gold Chain', 'design_code': 'CH-055', 'wish_count': 6, 'customers': 5},
            {'product_id': 'P-DEMO-05', 'product_name': 'Antique Pendant', 'design_code': 'PD-033', 'wish_count': 4, 'customers': 3},
        ]
        return Response({
            'days': days,
            'branch_id': branch_id,
            'is_demo': True,
            'results': demo[:limit],
        })

    return Response({
        'days': days,
        'branch_id': branch_id,
        'is_demo': False,
        'results': [
            {
                'product_id': r.get('product_id'),
                'product_name': r.get('product_name') or '—',
                'design_code': r.get('design_code') or '',
                'wish_count': r.get('wish_count') or 0,
                'customers': r.get('customers') or 0,
            }
            for r in rows
        ],
    })


@api_view(['GET'])
@admin_auth(*READ_AUTH)
def crm_customer_demographics(request):
    """
    Age-group distribution (from DOB) + upcoming birthdays / anniversaries / weddings.
    Store-level by default; optional branch_id / customer_id scope.
    """
    branch_id = _parse_int(request.GET.get('branch_id') or request.GET.get('store_id'))
    customer_id = _parse_int(request.GET.get('customer_id'))
    event_days = _parse_int(request.GET.get('event_days')) or 30
    event_days = max(1, min(event_days, 90))
    event_limit = _parse_int(request.GET.get('event_limit')) or 80
    event_limit = max(10, min(event_limit, 200))

    today = timezone.localdate()
    customers = _scoped_customers(branch_id=branch_id, customer_id=customer_id)

    # If branch filter yields nobody (no visits yet), fall back to all active customers
    # so age/events still work after Excel import without visits.
    if branch_id and not customer_id and not customers.exists():
        customers = Customer.objects.filter(is_active=True)

    age = _build_age_groups(customers, today)
    events = _build_upcoming_events(customers, today, event_days, limit=event_limit)

    return Response({
        'branch_id': branch_id,
        'customer_id': customer_id,
        'as_of': today.isoformat(),
        'age_groups': age,
        'upcoming_events': {
            'days': event_days,
            'count': len(events),
            'results': events,
        },
    })
