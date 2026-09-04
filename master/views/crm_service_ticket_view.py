"""
CRM service tickets (repair / exchange / return).

GET  /master/crm/service-tickets/              — list (filters: type, status, customer_id)
POST /master/crm/service-tickets/              — create
PATCH /master/crm/service-tickets/<id>/        — update status/notes
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from shared.models import CrmServiceTicket, Customer

READ_AUTH = (
    'CRM_CUSTOMER_LIST',
    'CRM_CUSTOMER_LIST_VIEW',
    'CRM_CUSTOMER_VISIT_TRACKING',
)
WRITE_AUTH = (
    'CRM_CUSTOMER_LIST',
    'CRM_CUSTOMER_LIST_UPDATE',
    'CRM_CUSTOMER_VISIT_TRACKING',
    'CRM_CUSTOMER_VISIT_TRACKING_UPDATE',
)


def _serialize(t: CrmServiceTicket) -> dict:
    return {
        'id': t.id,
        'customer_id': t.customer_id,
        'customer_name': t.customer.full_name if t.customer_id else '',
        'customer_mobile': t.customer.mobile if t.customer_id else '',
        'ticket_type': t.ticket_type,
        'status': t.status,
        'title': t.title,
        'item_description': t.item_description,
        'notes': t.notes,
        'amount': str(t.amount or 0),
        'opened_at': t.opened_at.isoformat() if t.opened_at else None,
        'expected_ready_date': t.expected_ready_date.isoformat() if t.expected_ready_date else None,
        'closed_at': t.closed_at.isoformat() if t.closed_at else None,
        'branch_id': t.branch_id,
        'ref_invoice_id': t.ref_invoice_id,
    }


@api_view(['GET', 'POST'])
@admin_auth(*(READ_AUTH + WRITE_AUTH))
def service_ticket_list_create(request):
    if request.method == 'GET':
        qs = CrmServiceTicket.objects.select_related('customer').all()
        ticket_type = (request.GET.get('type') or request.GET.get('ticket_type') or '').upper()
        if ticket_type in dict(CrmServiceTicket.TYPE_CHOICES):
            qs = qs.filter(ticket_type=ticket_type)
        st = (request.GET.get('status') or '').upper()
        if st in dict(CrmServiceTicket.STATUS_CHOICES):
            qs = qs.filter(status=st)
        customer_id = request.GET.get('customer_id')
        if customer_id:
            qs = qs.filter(customer_id=customer_id)
        open_only = (request.GET.get('open_only') or '').lower() in ('1', 'true', 'yes')
        if open_only:
            qs = qs.filter(status__in=[
                CrmServiceTicket.STATUS_OPEN,
                CrmServiceTicket.STATUS_IN_PROGRESS,
                CrmServiceTicket.STATUS_READY,
            ])
        page = max(1, int(request.GET.get('page', 1)))
        page_size = min(100, max(1, int(request.GET.get('page_size', 50))))
        total = qs.count()
        start = (page - 1) * page_size
        rows = [_serialize(t) for t in qs[start: start + page_size]]
        return Response({'count': total, 'page': page, 'page_size': page_size, 'results': rows})

    # POST
    data = request.data
    customer_id = data.get('customer_id')
    ticket_type = (data.get('ticket_type') or data.get('type') or '').upper()
    title = (data.get('title') or '').strip()
    if not customer_id or ticket_type not in dict(CrmServiceTicket.TYPE_CHOICES) or not title:
        return Response(
            {'error': 'customer_id, ticket_type (REPAIR|EXCHANGE|RETURN), and title are required'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        customer = Customer.objects.get(id=customer_id)
    except Customer.DoesNotExist:
        return Response({'error': 'customer not found'}, status=status.HTTP_404_NOT_FOUND)

    opened_raw = data.get('opened_at')
    opened_at = parse_datetime(str(opened_raw)) if opened_raw else timezone.now()
    if opened_at and timezone.is_naive(opened_at):
        opened_at = timezone.make_aware(opened_at, timezone.get_current_timezone())

    expected = data.get('expected_ready_date')
    expected_date = parse_date(str(expected)) if expected else None

    try:
        amount = Decimal(str(data.get('amount') or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0')

    ticket = CrmServiceTicket.objects.create(
        customer=customer,
        ticket_type=ticket_type,
        status=(data.get('status') or CrmServiceTicket.STATUS_OPEN).upper(),
        title=title[:200],
        item_description=(data.get('item_description') or '').strip(),
        notes=(data.get('notes') or '').strip(),
        amount=amount,
        opened_at=opened_at or timezone.now(),
        expected_ready_date=expected_date,
        branch_id=data.get('branch_id') or None,
        ref_invoice_id=data.get('ref_invoice_id') or None,
        created_by=request.user if getattr(request.user, 'id', None) else None,
        updated_by=request.user if getattr(request.user, 'id', None) else None,
    )
    return Response(_serialize(ticket), status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
@admin_auth(*WRITE_AUTH)
def service_ticket_update(request, pk: int):
    try:
        ticket = CrmServiceTicket.objects.select_related('customer').get(pk=pk)
    except CrmServiceTicket.DoesNotExist:
        return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)

    data = request.data
    if 'status' in data:
        st = str(data.get('status') or '').upper()
        if st in dict(CrmServiceTicket.STATUS_CHOICES):
            ticket.status = st
            if st in (CrmServiceTicket.STATUS_CLOSED, CrmServiceTicket.STATUS_CANCELLED):
                ticket.closed_at = timezone.now()
    for field in ('title', 'item_description', 'notes'):
        if field in data and data[field] is not None:
            setattr(ticket, field, str(data[field]).strip())
    if 'amount' in data:
        try:
            ticket.amount = Decimal(str(data.get('amount') or 0))
        except (InvalidOperation, TypeError, ValueError):
            pass
    if 'expected_ready_date' in data:
        raw = data.get('expected_ready_date')
        ticket.expected_ready_date = parse_date(str(raw)) if raw else None
    ticket.updated_by = request.user if getattr(request.user, 'id', None) else None
    ticket.save()
    return Response(_serialize(ticket))
