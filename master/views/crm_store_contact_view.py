"""
CRM store contact log (client requirement).

Always record when a customer is contacted from the store, with:
  - channel (IN_STORE / CALL / WHATSAPP)
  - contact_reason
  - conversation remarks

GET  /master/crm/store-contacts/           — list (filters: customer_id, channel, reason)
POST /master/crm/store-contacts/           — create (reason + remarks required)
GET  /master/crm/store-contacts/reasons/   — reason + channel option lists
"""
from __future__ import annotations

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from shared.models import CrmStoreContact, Customer

READ_AUTH = (
    'CRM_CUSTOMER_LIST',
    'CRM_CUSTOMER_LIST_VIEW',
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_VIEW',
)
WRITE_AUTH = (
    'CRM_CUSTOMER_LIST',
    'CRM_CUSTOMER_LIST_UPDATE',
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_UPDATE',
)


def _serialize(c: CrmStoreContact) -> dict:
    return {
        'id': c.id,
        'customer_id': c.customer_id,
        'customer_name': c.customer.full_name if c.customer_id else '',
        'customer_mobile': c.customer.mobile if c.customer_id else '',
        'branch_id': c.branch_id,
        'branch_name': c.branch.name if c.branch_id and c.branch else None,
        'channel': c.channel,
        'channel_label': dict(CrmStoreContact.CHANNEL_CHOICES).get(c.channel, c.channel),
        'contact_reason': c.contact_reason,
        'contact_reason_label': dict(CrmStoreContact.REASON_CHOICES).get(
            c.contact_reason, c.contact_reason
        ),
        'remarks': c.remarks,
        'contacted_at': c.contacted_at.isoformat() if c.contacted_at else None,
        'logged_by': (
            getattr(c.created_by, 'full_name', None)
            or getattr(c.created_by, 'username', None)
            if c.created_by_id
            else None
        ),
    }


@api_view(['GET'])
@admin_auth(*READ_AUTH)
def store_contact_options(request):
    return Response({
        'channels': [
            {'value': v, 'label': l} for v, l in CrmStoreContact.CHANNEL_CHOICES
        ],
        'reasons': [
            {'value': v, 'label': l} for v, l in CrmStoreContact.REASON_CHOICES
        ],
    })


@api_view(['GET', 'POST'])
@admin_auth(*(READ_AUTH + WRITE_AUTH))
def store_contact_list_create(request):
    if request.method == 'GET':
        qs = CrmStoreContact.objects.select_related('customer', 'branch', 'created_by').all()
        customer_id = request.GET.get('customer_id')
        if customer_id:
            qs = qs.filter(customer_id=customer_id)
        channel = (request.GET.get('channel') or '').upper()
        if channel in dict(CrmStoreContact.CHANNEL_CHOICES):
            qs = qs.filter(channel=channel)
        reason = (request.GET.get('reason') or request.GET.get('contact_reason') or '').upper()
        if reason in dict(CrmStoreContact.REASON_CHOICES):
            qs = qs.filter(contact_reason=reason)
        page = max(1, int(request.GET.get('page', 1)))
        page_size = min(100, max(1, int(request.GET.get('page_size', 50))))
        total = qs.count()
        start = (page - 1) * page_size
        rows = [_serialize(c) for c in qs[start: start + page_size]]
        return Response({'count': total, 'page': page, 'page_size': page_size, 'results': rows})

    data = request.data
    customer_id = data.get('customer_id')
    contact_reason = (data.get('contact_reason') or data.get('reason') or '').upper()
    remarks = (data.get('remarks') or data.get('notes') or '').strip()
    channel = (data.get('channel') or CrmStoreContact.CHANNEL_IN_STORE).upper()

    if not customer_id:
        return Response({'error': 'customer_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    if contact_reason not in dict(CrmStoreContact.REASON_CHOICES):
        return Response(
            {'error': 'contact_reason is required', 'allowed': [v for v, _ in CrmStoreContact.REASON_CHOICES]},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not remarks:
        return Response(
            {'error': 'remarks are required (conversation notes)'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if channel not in dict(CrmStoreContact.CHANNEL_CHOICES):
        channel = CrmStoreContact.CHANNEL_IN_STORE

    try:
        customer = Customer.objects.get(id=customer_id)
    except Customer.DoesNotExist:
        return Response({'error': 'customer not found'}, status=status.HTTP_404_NOT_FOUND)

    raw_at = data.get('contacted_at')
    contacted_at = parse_datetime(str(raw_at)) if raw_at else timezone.now()
    if contacted_at and timezone.is_naive(contacted_at):
        contacted_at = timezone.make_aware(contacted_at, timezone.get_current_timezone())

    contact = CrmStoreContact.objects.create(
        customer=customer,
        branch_id=data.get('branch_id') or None,
        channel=channel,
        contact_reason=contact_reason,
        remarks=remarks,
        contacted_at=contacted_at or timezone.now(),
        created_by=request.user if getattr(request.user, 'id', None) else None,
        updated_by=request.user if getattr(request.user, 'id', None) else None,
    )
    return Response(_serialize(contact), status=status.HTTP_201_CREATED)
