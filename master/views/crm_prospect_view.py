"""
CRM Prospect Campaign APIs (Phase 4).

GET  /master/crm/prospects/check/?mobile=  — suppression + existing-customer check
GET  /master/crm/prospects/               — paginated contact log
POST /master/crm/prospects/               — log a phone-diary / WhatsApp contact
GET  /master/crm/prospects/summary/       — simple campaign KPIs
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from shared.models import CrmProspectContact
from shared.services.crm_prospect_service import (
    create_prospect_contact,
    find_customer_by_mobile,
    normalize_mobile,
    prior_contacts_for_mobile,
    serialize_prospect,
)

READ_AUTH = (
    'CRM_CUSTOMER_CAMPAIGNS',
    'CRM_CUSTOMER_CAMPAIGNS_VIEW',
    'CRM_CUSTOMER_LIST',
    'CRM_CUSTOMER_LIST_VIEW',
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_VIEW',
    'CRM_COMMUNICATION_VIEW',
)

WRITE_AUTH = (
    'CRM_CUSTOMER_CAMPAIGNS',
    'CRM_CUSTOMER_CAMPAIGNS_CREATE',
    'CRM_CUSTOMER_CAMPAIGNS_UPDATE',
    'CRM_CUSTOMER_LIST',
    'CRM_CUSTOMER_LIST_CREATE',
    'CRM_CUSTOMER_LIST_UPDATE',
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_CREATE',
    'CRM_COMMUNICATION_CREATE',
)


def _parse_int(val):
    if val is None or val == '':
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


@api_view(['GET'])
@admin_auth(*READ_AUTH)
def crm_prospect_check(request):
    """Live suppression check while staff types a mobile number."""
    normalized = normalize_mobile(request.GET.get('mobile'))
    if len(normalized) < 10:
        return Response({
            'mobile_normalized': normalized,
            'valid': False,
            'already_contacted': False,
            'is_existing_customer': False,
            'prior_contacts': [],
            'matched_customer': None,
        })

    matched = find_customer_by_mobile(normalized)
    prior = list(prior_contacts_for_mobile(normalized, limit=8))
    return Response({
        'mobile_normalized': normalized,
        'valid': True,
        'already_contacted': len(prior) > 0,
        'is_existing_customer': bool(matched),
        'matched_customer': (
            {
                'id': matched.id,
                'full_name': matched.full_name,
                'customer_code': matched.customer_code,
                'mobile': matched.mobile,
            }
            if matched
            else None
        ),
        'prior_contacts': [serialize_prospect(p) for p in prior],
        'last_contacted_at': prior[0].contacted_at.isoformat() if prior else None,
    })


@api_view(['GET', 'POST'])
@admin_auth(*READ_AUTH)
def crm_prospect_list_create(request):
    if request.method == 'GET':
        page_number = max(1, int(request.GET.get('page', 1)))
        page_size = min(50, max(1, int(request.GET.get('page_size', 10))))
        search = (request.GET.get('search') or '').strip()
        branch_id = _parse_int(request.GET.get('branch_id') or request.GET.get('store_id'))
        campaign = (request.GET.get('campaign_name') or '').strip()
        channel = (request.GET.get('channel') or '').strip().upper()
        outcome = (request.GET.get('outcome') or '').strip()

        qs = CrmProspectContact.objects.select_related(
            'branch', 'created_by', 'matched_customer'
        ).all()
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if campaign:
            qs = qs.filter(campaign_name__icontains=campaign)
        if channel:
            qs = qs.filter(channel=channel)
        if outcome:
            qs = qs.filter(outcome=outcome)
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(mobile__icontains=search)
                | Q(mobile_normalized__icontains=search)
                | Q(campaign_name__icontains=search)
                | Q(notes__icontains=search)
            )

        force_demo = (request.GET.get('demo') or '').strip().lower() in ('1', 'true', 'yes')
        force_real = (request.GET.get('demo') or '').strip().lower() in ('0', 'false', 'no')
        if force_demo or (not qs.exists() and not force_real):
            today = timezone.now()
            demo = [
                {
                    'id': 70001,
                    'name': 'Ravi Diary',
                    'mobile': '9876511101',
                    'mobile_normalized': '9876511101',
                    'branch_id': branch_id,
                    'branch_name': 'Demo Store',
                    'campaign_name': 'March Phone Diary',
                    'channel': 'CALL',
                    'outcome': 'interested',
                    'notes': 'Asked about gold bangles.',
                    'contacted_at': (today - timedelta(hours=2)).isoformat(),
                    'matched_customer_id': None,
                    'matched_customer_name': None,
                    'handled_by': 'Demo Staff',
                },
                {
                    'id': 70002,
                    'name': 'Meena Gupta',
                    'mobile': '9876511102',
                    'mobile_normalized': '9876511102',
                    'branch_id': branch_id,
                    'branch_name': 'Demo Store',
                    'campaign_name': 'March Phone Diary',
                    'channel': 'WHATSAPP',
                    'outcome': 'callback',
                    'notes': 'Call again next Saturday.',
                    'contacted_at': (today - timedelta(days=1)).isoformat(),
                    'matched_customer_id': None,
                    'matched_customer_name': None,
                    'handled_by': 'Demo Staff',
                },
                {
                    'id': 70003,
                    'name': 'Suresh Nair',
                    'mobile': '9876511103',
                    'mobile_normalized': '9876511103',
                    'branch_id': branch_id,
                    'branch_name': 'Demo Store',
                    'campaign_name': 'Wedding Season',
                    'channel': 'CALL',
                    'outcome': 'not_interested',
                    'notes': 'Not looking now.',
                    'contacted_at': (today - timedelta(days=3)).isoformat(),
                    'matched_customer_id': None,
                    'matched_customer_name': None,
                    'handled_by': 'Demo Staff',
                },
            ]
            if search:
                q = search.lower()
                demo = [r for r in demo if q in r['name'].lower() or q in r['mobile']]
            paginator = Paginator(demo, page_size)
            page = paginator.get_page(page_number)
            return Response({
                'count': paginator.count,
                'total_pages': paginator.num_pages,
                'current_page': page.number,
                'is_demo': True,
                'results': list(page.object_list),
            })

        paginator = Paginator(qs, page_size)
        page = paginator.get_page(page_number)
        return Response({
            'count': paginator.count,
            'total_pages': paginator.num_pages,
            'current_page': page.number,
            'is_demo': False,
            'results': [serialize_prospect(r) for r in page.object_list],
        })

    # POST
    from master.permissions.permission_checker import ensure_admin_permission

    denied = ensure_admin_permission(request, *WRITE_AUTH)
    if denied:
        return denied

    data = request.data if isinstance(request.data, dict) else {}
    contacted_raw = data.get('contacted_at') or data.get('contactedAt')
    contacted_at = None
    if contacted_raw:
        contacted_at = parse_datetime(str(contacted_raw))
        if contacted_at is None:
            try:
                contacted_at = datetime.fromisoformat(str(contacted_raw).replace('Z', '+00:00'))
            except ValueError:
                contacted_at = None

    row, meta = create_prospect_contact(
        name=str(data.get('name') or ''),
        mobile=str(data.get('mobile') or ''),
        channel=str(data.get('channel') or CrmProspectContact.CHANNEL_CALL),
        outcome=str(data.get('outcome') or CrmProspectContact.OUTCOME_OTHER),
        notes=str(data.get('notes') or ''),
        campaign_name=str(data.get('campaign_name') or data.get('campaignName') or ''),
        branch_id=_parse_int(data.get('branch_id') or data.get('branchId')),
        contacted_at=contacted_at,
        admin_user=getattr(request, 'admin_user', None) or getattr(request, 'user', None),
        allow_existing_customer=bool(data.get('allow_existing_customer') or data.get('allowExistingCustomer')),
    )
    if row is None:
        return Response(meta, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {**serialize_prospect(row), **{k: v for k, v in meta.items() if k != 'error'}},
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
@admin_auth(*READ_AUTH)
def crm_prospect_summary(request):
    branch_id = _parse_int(request.GET.get('branch_id') or request.GET.get('store_id'))
    days = _parse_int(request.GET.get('days')) or 30
    days = max(1, min(days, 365))
    since = timezone.now() - timedelta(days=days)

    qs = CrmProspectContact.objects.filter(contacted_at__gte=since)
    if branch_id:
        qs = qs.filter(branch_id=branch_id)

    force_demo = (request.GET.get('demo') or '').strip().lower() in ('1', 'true', 'yes')
    force_real = (request.GET.get('demo') or '').strip().lower() in ('0', 'false', 'no')
    if force_demo or (not qs.exists() and not force_real):
        return Response({
            'days': days,
            'branch_id': branch_id,
            'is_demo': True,
            'kpis': {
                'contacts': 42,
                'unique_numbers': 38,
                'callbacks': 9,
                'interested': 11,
                'suppressed_repeats': 4,
            },
            'by_channel': [
                {'channel': 'CALL', 'count': 30},
                {'channel': 'WHATSAPP', 'count': 12},
            ],
            'by_campaign': [
                {'campaign_name': 'March Phone Diary', 'count': 28},
                {'campaign_name': 'Wedding Season', 'count': 14},
            ],
        })

    total = qs.count()
    unique = qs.values('mobile_normalized').distinct().count()
    callbacks = qs.filter(outcome=CrmProspectContact.OUTCOME_CALLBACK).count()
    interested = qs.filter(outcome=CrmProspectContact.OUTCOME_INTERESTED).count()
    # Repeats = contacts where that mobile appears more than once in window
    repeats = (
        qs.values('mobile_normalized')
        .annotate(c=Count('id'))
        .filter(c__gt=1)
        .count()
    )
    by_channel = list(qs.values('channel').annotate(count=Count('id')).order_by('-count'))
    by_campaign = list(
        qs.exclude(campaign_name='')
        .values('campaign_name')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )
    return Response({
        'days': days,
        'branch_id': branch_id,
        'is_demo': False,
        'kpis': {
            'contacts': total,
            'unique_numbers': unique,
            'callbacks': callbacks,
            'interested': interested,
            'suppressed_repeats': repeats,
        },
        'by_channel': by_channel,
        'by_campaign': by_campaign,
    })
