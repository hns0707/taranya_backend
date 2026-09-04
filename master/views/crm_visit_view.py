"""
CRM Visit Dashboard APIs.

GET  /master/crm/visits/dashboard/  — KPIs + charts (store-level default; optional customer filter)
GET  /master/crm/visits/            — paginated visit list
PATCH /master/crm/visits/<id>/      — mark buy_next_time / notes

When there is no visit data yet, dashboard/list return demo payload (`is_demo: true`)
so UI can be reviewed. Pass `?demo=0` to force empty real data.
Pass `?demo=1` to force demo even if real visits exist.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from shared.models import CrmCustomerVisit
from shared.services.crm_visit_service import (
    IST,
    annotate_visit_outcome,
    filter_visits,
    local_day_bounds,
)

VISIT_READ_AUTH = (
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_VIEW',
    'CRM_CUSTOMER_LIST_VIEW',
    'CRM_CUSTOMER_LIST',
)

VISIT_WRITE_AUTH = (
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_CREATE',
    'CRM_CUSTOMER_VISIT_TRACKING_UPDATE',
    'CRM_CUSTOMER_LIST_UPDATE',
)

WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

DEMO_CUSTOMERS = [
    {'id': 9001, 'name': 'Anjana', 'mobile': '9876500001', 'code': 'CUS-DEMO-01'},
    {'id': 9002, 'name': 'Hari Sahu', 'mobile': '9876500002', 'code': 'CUS-DEMO-02'},
    {'id': 9003, 'name': 'Prasoon Shukla', 'mobile': '9876500003', 'code': 'CUS-DEMO-03'},
    {'id': 9004, 'name': 'Priya Sharma', 'mobile': '9876500004', 'code': 'CUS-DEMO-04'},
    {'id': 9005, 'name': 'Rahul Verma', 'mobile': '9876500005', 'code': 'CUS-DEMO-05'},
    {'id': 9006, 'name': 'Anita Patel', 'mobile': '9876500006', 'code': 'CUS-DEMO-06'},
    {'id': 9007, 'name': 'Sneha Gupta', 'mobile': '9876500007', 'code': 'CUS-DEMO-07'},
    {'id': 9008, 'name': 'Vikram Singh', 'mobile': '9876500008', 'code': 'CUS-DEMO-08'},
]


def _parse_int(val):
    if val is None or val == '':
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _scope_params(request):
    branch_id = _parse_int(request.GET.get('branch_id') or request.GET.get('store_id'))
    customer_id = _parse_int(request.GET.get('customer_id'))
    return branch_id, customer_id


def _demo_flag(request) -> str | None:
    """Return 'force' | 'off' | None (auto)."""
    raw = (request.GET.get('demo') or '').strip().lower()
    if raw in ('1', 'true', 'yes'):
        return 'force'
    if raw in ('0', 'false', 'no'):
        return 'off'
    return None


def _demo_visit_row(*, vid, customer, day, hour, minute, outcome, branch_id, branch_name):
    visited = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    converted = outcome == 'converted'
    return {
        'id': vid,
        'customer_id': customer['id'],
        'customer_name': customer['name'],
        'customer_mobile': customer['mobile'],
        'customer_code': customer['code'],
        'branch_id': branch_id,
        'branch_name': branch_name,
        'quote_id': None,
        'quote_number': f'DEMO-Q-{vid}',
        'visited_at': visited.isoformat(),
        'visit_date': day.isoformat(),
        'source': 'catalogue_enquiry' if vid % 2 else 'barcode_scan',
        'buy_next_time': (not converted) and (vid % 3 == 0),
        'outcome': outcome,
        'converted': converted,
        'lost': not converted,
        'handled_by': 'Demo Sales',
    }


def _build_demo_rows(today, week_start, month_start, month_end, branch_id, customer_id):
    branch_name = 'Demo Store' if branch_id else 'Main Branch'
    rows = []
    vid = 50000

    # Today — spread across shop hours
    today_slots = [
        (10, 15, 'converted'), (11, 5, 'lost'), (12, 40, 'converted'),
        (14, 10, 'lost'), (15, 30, 'converted'), (16, 45, 'lost'),
        (17, 20, 'converted'), (18, 5, 'lost'),
    ]
    for i, (h, m, outcome) in enumerate(today_slots):
        c = DEMO_CUSTOMERS[i % len(DEMO_CUSTOMERS)]
        if customer_id and c['id'] != customer_id:
            continue
        rows.append(_demo_visit_row(
            vid=vid + i, customer=c, day=today, hour=h, minute=m,
            outcome=outcome, branch_id=branch_id, branch_name=branch_name,
        ))

    # Rest of this week
    vid = 50100
    for day_offset in range(0, 7):
        day = week_start + timedelta(days=day_offset)
        if day == today:
            continue
        if day > today:
            break
        for j in range(3 + (day_offset % 3)):
            c = DEMO_CUSTOMERS[(day_offset + j) % len(DEMO_CUSTOMERS)]
            if customer_id and c['id'] != customer_id:
                continue
            outcome = 'converted' if (day_offset + j) % 2 == 0 else 'lost'
            rows.append(_demo_visit_row(
                vid=vid + day_offset * 10 + j, customer=c, day=day,
                hour=11 + j, minute=10 * j, outcome=outcome,
                branch_id=branch_id, branch_name=branch_name,
            ))

    # Earlier days in month
    vid = 50200
    day = month_start
    n = 0
    while day < week_start and day < month_end:
        for j in range(2 + (n % 2)):
            c = DEMO_CUSTOMERS[(n + j) % len(DEMO_CUSTOMERS)]
            if customer_id and c['id'] != customer_id:
                continue
            outcome = 'converted' if (n + j) % 3 else 'lost'
            rows.append(_demo_visit_row(
                vid=vid + n * 10 + j, customer=c, day=day,
                hour=12 + j, minute=5, outcome=outcome,
                branch_id=branch_id, branch_name=branch_name,
            ))
        day += timedelta(days=1)
        n += 1

    return rows


def _aggregate_dashboard(annotated, month_visits_by_id, today, week_start, week_end, month_start, month_end):
    def in_range(rows, start_d, end_d_exclusive):
        out = []
        for row in rows:
            d = datetime.fromisoformat(row['visit_date']).date() if isinstance(row['visit_date'], str) else row['visit_date']
            if start_d <= d < end_d_exclusive:
                out.append(row)
        return out

    today_rows = in_range(annotated, today, today + timedelta(days=1))
    week_rows = in_range(annotated, week_start, week_end)
    month_rows = annotated

    def unique_customers(rows):
        return len({r['customer_id'] for r in rows})

    def lost_rows(rows):
        return [r for r in rows if r['lost']]

    kpis = {
        'walkins_today': unique_customers(today_rows),
        'walkins_week': unique_customers(week_rows),
        'walkins_month': unique_customers(month_rows),
        'visits_today': len(today_rows),
        'visits_week': len(week_rows),
        'visits_month': len(month_rows),
        'lost_today': unique_customers(lost_rows(today_rows)),
        'lost_week': unique_customers(lost_rows(week_rows)),
        'lost_month': unique_customers(lost_rows(month_rows)),
        'converted_today': unique_customers([r for r in today_rows if r['converted']]),
        'converted_week': unique_customers([r for r in week_rows if r['converted']]),
        'converted_month': unique_customers([r for r in month_rows if r['converted']]),
    }

    hourly_map = {h: {'hour': h, 'label': f'{h:02d}:00', 'visits': 0, 'lost': 0, 'converted': 0} for h in range(24)}
    for row in today_rows:
        visit = month_visits_by_id.get(row['id'])
        if visit is not None:
            local_dt = visit.visited_at.astimezone(IST)
            h = local_dt.hour
        else:
            h = datetime.fromisoformat(row['visited_at']).astimezone(IST).hour
        hourly_map[h]['visits'] += 1
        if row['converted']:
            hourly_map[h]['converted'] += 1
        if row['lost']:
            hourly_map[h]['lost'] += 1
    daily_hourly = [hourly_map[h] for h in range(24)]

    weekly_map = {
        i: {'day': WEEKDAY_LABELS[i], 'day_index': i, 'visits': 0, 'lost': 0, 'converted': 0}
        for i in range(7)
    }
    for row in week_rows:
        d = datetime.fromisoformat(row['visit_date']).date()
        idx = d.weekday()
        weekly_map[idx]['visits'] += 1
        if row['converted']:
            weekly_map[idx]['converted'] += 1
        if row['lost']:
            weekly_map[idx]['lost'] += 1
    weekly = [weekly_map[i] for i in range(7)]

    days_in_month = (month_end - month_start).days
    monthly_map = {}
    for i in range(days_in_month):
        d = month_start + timedelta(days=i)
        key = d.isoformat()
        monthly_map[key] = {
            'date': key,
            'label': d.strftime('%d %b'),
            'visits': 0,
            'lost': 0,
            'converted': 0,
        }
    for row in month_rows:
        key = row['visit_date']
        if key not in monthly_map:
            continue
        monthly_map[key]['visits'] += 1
        if row['converted']:
            monthly_map[key]['converted'] += 1
        if row['lost']:
            monthly_map[key]['lost'] += 1
    monthly = [monthly_map[k] for k in sorted(monthly_map.keys())]

    lost_list = sorted(lost_rows(month_rows), key=lambda r: r['visited_at'], reverse=True)[:50]
    recent = sorted(month_rows, key=lambda r: r['visited_at'], reverse=True)[:25]

    return kpis, daily_hourly, weekly, monthly, lost_list, recent


def _has_real_visits(*, branch_id=None, customer_id=None) -> bool:
    qs = CrmCustomerVisit.objects.all()
    if branch_id:
        qs = qs.filter(branch_id=branch_id)
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    return qs.exists()


def _anchor_month_from_latest_visit(*, branch_id=None, customer_id=None):
    """
    If current calendar month has no visits, return the month of the latest
    real visit so imported invoice history still drives the dashboard.
    """
    qs = filter_visits(branch_id=branch_id, customer_id=customer_id).order_by('-visited_at')
    latest = qs.first()
    if not latest:
        return None
    return latest.visited_at.astimezone(IST).date().replace(day=1)


@api_view(['GET'])
@admin_auth(*VISIT_READ_AUTH)
def crm_visit_dashboard(request):
    """
    Store-level CRM visit analytics by default.
    Pass customer_id to scope charts/KPIs to one customer.
    """
    branch_id, customer_id = _scope_params(request)
    demo_mode = _demo_flag(request)
    now = timezone.now().astimezone(IST)
    today = now.date()

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    month_start = today.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    month_start_dt, _ = local_day_bounds(month_start)
    month_end_dt = datetime.combine(month_end, datetime.min.time(), tzinfo=IST)

    month_visits = list(
        filter_visits(
            branch_id=branch_id,
            customer_id=customer_id,
            start=month_start_dt,
            end=month_end_dt,
        )
    )

    anchored = False
    # Imported invoices are often older than "this month" — anchor to latest visit month
    # instead of falling back to demo sample customers.
    if (
        len(month_visits) == 0
        and demo_mode != 'force'
        and _has_real_visits(branch_id=branch_id, customer_id=customer_id)
    ):
        anchor_start = _anchor_month_from_latest_visit(branch_id=branch_id, customer_id=customer_id)
        if anchor_start:
            month_start = anchor_start
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)
            # Keep week relative to end of that month (or today if same month)
            anchor_today = min(today, month_end - timedelta(days=1))
            week_start = anchor_today - timedelta(days=anchor_today.weekday())
            week_end = week_start + timedelta(days=7)
            month_start_dt, _ = local_day_bounds(month_start)
            month_end_dt = datetime.combine(month_end, datetime.min.time(), tzinfo=IST)
            month_visits = list(
                filter_visits(
                    branch_id=branch_id,
                    customer_id=customer_id,
                    start=month_start_dt,
                    end=month_end_dt,
                )
            )
            today = anchor_today
            anchored = True

    has_real = _has_real_visits(branch_id=branch_id, customer_id=customer_id)
    use_demo = demo_mode == 'force' or (demo_mode is None and not has_real and len(month_visits) == 0)
    if demo_mode == 'off':
        use_demo = False

    if use_demo:
        annotated = _build_demo_rows(today, week_start, month_start, month_end, branch_id, customer_id)
        visits_by_id = {}
    else:
        annotated = [annotate_visit_outcome(v) for v in month_visits]
        visits_by_id = {v.id: v for v in month_visits}

    kpis, daily_hourly, weekly, monthly, lost_list, recent = _aggregate_dashboard(
        annotated, visits_by_id, today, week_start, week_end, month_start, month_end,
    )

    return Response({
        'branch_id': branch_id,
        'customer_id': customer_id,
        'timezone': 'Asia/Kolkata',
        'as_of': now.isoformat(),
        'is_demo': use_demo,
        'anchored_to_data_month': anchored,
        'ranges': {
            'today': today.isoformat(),
            'week_start': week_start.isoformat(),
            'week_end': (week_end - timedelta(days=1)).isoformat(),
            'month_start': month_start.isoformat(),
            'month_end': (month_end - timedelta(days=1)).isoformat(),
        },
        'kpis': kpis,
        'charts': {
            'daily_hourly': daily_hourly,
            'weekly': weekly,
            'monthly': monthly,
        },
        'lost_customers': lost_list,
        'recent_visits': recent,
    })


@api_view(['GET'])
@admin_auth(*VISIT_READ_AUTH)
def crm_visit_list(request):
    branch_id, customer_id = _scope_params(request)
    outcome_filter = (request.GET.get('outcome') or '').strip().lower()
    search = (request.GET.get('search') or '').strip()
    demo_mode = _demo_flag(request)

    try:
        page = max(1, int(request.GET.get('page') or 1))
    except ValueError:
        page = 1
    try:
        page_size = min(100, max(1, int(request.GET.get('page_size') or 20)))
    except ValueError:
        page_size = 20

    now = timezone.now().astimezone(IST)
    today = now.date()
    month_start = today.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    start_dt = datetime.combine(month_start, datetime.min.time(), tzinfo=IST)
    end_dt = datetime.combine(month_end, datetime.min.time(), tzinfo=IST)

    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    if date_from:
        try:
            start_dt = datetime.combine(
                datetime.strptime(date_from[:10], '%Y-%m-%d').date(),
                datetime.min.time(),
                tzinfo=IST,
            )
        except ValueError:
            pass
    if date_to:
        try:
            end_dt = datetime.combine(
                datetime.strptime(date_to[:10], '%Y-%m-%d').date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=IST,
            )
        except ValueError:
            pass

    qs = filter_visits(
        branch_id=branch_id,
        customer_id=customer_id,
        start=start_dt,
        end=end_dt,
    ).order_by('-visited_at')

    if search:
        qs = qs.filter(models_q_search(search))

    real_count = qs.count()
    has_any = _has_real_visits(branch_id=branch_id, customer_id=customer_id)

    # If default month window is empty but imported visits exist, widen to last 365 days
    if real_count == 0 and has_any and not date_from and not date_to and demo_mode != 'force':
        start_dt = datetime.combine(today - timedelta(days=365), datetime.min.time(), tzinfo=IST)
        end_dt = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=IST)
        qs = filter_visits(
            branch_id=branch_id,
            customer_id=customer_id,
            start=start_dt,
            end=end_dt,
        ).order_by('-visited_at')
        if search:
            qs = qs.filter(models_q_search(search))
        real_count = qs.count()

    use_demo = demo_mode == 'force' or (demo_mode is None and not has_any and real_count == 0)
    if demo_mode == 'off':
        use_demo = False

    if use_demo:
        week_start = today - timedelta(days=today.weekday())
        rows = _build_demo_rows(today, week_start, month_start, month_end, branch_id, customer_id)
        if search:
            q = search.lower()
            rows = [
                r for r in rows
                if q in (r['customer_name'] or '').lower()
                or q in (r['customer_mobile'] or '')
                or q in (r.get('customer_code') or '').lower()
            ]
    else:
        rows = [annotate_visit_outcome(v) for v in qs]

    if outcome_filter in ('lost', 'converted'):
        rows = [r for r in rows if r['outcome'] == outcome_filter]

    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    return Response({
        'count': total,
        'page': page,
        'page_size': page_size,
        'is_demo': use_demo,
        'results': page_rows,
    })


def models_q_search(search: str):
    from django.db.models import Q
    return (
        Q(customer__full_name__icontains=search)
        | Q(customer__mobile__icontains=search)
        | Q(customer__customer_code__icontains=search)
    )


@api_view(['PATCH'])
@admin_auth(*VISIT_WRITE_AUTH)
def crm_visit_update(request, pk: int):
    try:
        visit = CrmCustomerVisit.objects.select_related('customer', 'branch', 'quote').get(pk=pk)
    except CrmCustomerVisit.DoesNotExist:
        return Response({'error': 'Visit not found.'}, status=status.HTTP_404_NOT_FOUND)

    data = request.data or {}
    updates = []
    if 'buy_next_time' in data:
        visit.buy_next_time = bool(data.get('buy_next_time'))
        updates.append('buy_next_time')
    if 'notes' in data and data.get('notes') is not None:
        visit.notes = str(data.get('notes') or '')
        updates.append('notes')
    if updates:
        visit.updated_by = getattr(request, 'admin_user', None)
        updates.extend(['updated_by', 'system_updated_at'])
        visit.save(update_fields=updates)

    return Response(annotate_visit_outcome(visit))
