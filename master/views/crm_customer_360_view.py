"""
CRM Customer 360 summary.

GET /master/customers/<pk>/crm-360/
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from master.permissions.permission_checker import admin_auth
from master.permissions.section_auth import CUSTOMER_READ_AUTH
from shared.models import (
    CatalogueQuote,
    CatalogueQuoteDiscountApproval,
    CatalogueQuoteLine,
    CommunicationLog,
    CrmCustomerVisit,
    CrmServiceTicket,
    CrmStoreContact,
    Customer,
    CustomerScheme,
    SaleInvoice,
)
from shared.services.crm_visit_service import IST

AGE_BANDS = [
    (0, 17, 'Under 18'),
    (18, 24, '18–24'),
    (25, 34, '25–34'),
    (35, 44, '35–44'),
    (45, 54, '45–54'),
    (55, 64, '55–64'),
    (65, 200, '65+'),
]


def _age_group(dob) -> str | None:
    if not dob:
        return None
    today = timezone.localdate()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    for lo, hi, label in AGE_BANDS:
        if lo <= age <= hi:
            return label
    return None


def _days_ago(dt) -> int | None:
    """Return whole days since dt (date or datetime)."""
    if not dt:
        return None
    # datetime is a subclass of date — check datetime first
    if isinstance(dt, datetime):
        aware = timezone.make_aware(dt, IST) if timezone.is_naive(dt) else dt
        d = aware.astimezone(IST).date()
    elif isinstance(dt, date):
        d = dt
    else:
        return None
    return (timezone.localdate() - d).days


@api_view(['GET'])
@admin_auth(*CUSTOMER_READ_AUTH)
def customer_crm_360(request, pk: int):
    try:
        customer = Customer.objects.select_related('referred_by').get(id=pk)
    except Customer.DoesNotExist:
        return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)

    today = timezone.localdate()
    year_start = today.replace(month=1, day=1)

    invoices = SaleInvoice.objects.filter(customer_id=pk, is_deleted=False)
    inv_agg = invoices.aggregate(ltv=Sum('total_amount'), cnt=Count('id'))
    ltv = inv_agg['ltv'] or Decimal('0')
    inv_count = inv_agg['cnt'] or 0
    aov = (ltv / inv_count).quantize(Decimal('0.01')) if inv_count else Decimal('0')

    last_invoice = invoices.order_by('-invoice_date', '-system_created_at').first()
    last_sale_date = last_invoice.invoice_date if last_invoice else None
    last_sale_amount = last_invoice.total_amount if last_invoice else None

    last_visit = (
        CrmCustomerVisit.objects.filter(customer_id=pk)
        .order_by('-visited_at')
        .first()
    )
    last_visit_at = last_visit.visited_at if last_visit else None

    year_invoices = list(
        invoices.filter(invoice_date__gte=year_start)
        .order_by('-invoice_date')[:20]
        .values(
            'id', 'invoice_number', 'invoice_date', 'total_amount',
            'paid_amount', 'pending_amount', 'status',
        )
    )
    for row in year_invoices:
        for k in ('total_amount', 'paid_amount', 'pending_amount'):
            if row.get(k) is not None:
                row[k] = str(row[k])
        if row.get('invoice_date'):
            row['invoice_date'] = row['invoice_date'].isoformat()

    pending_quotes = list(
        CatalogueQuote.objects.filter(
            customer_id=pk,
            status__in=[CatalogueQuote.STATUS_ORDER, CatalogueQuote.STATUS_BOOKING],
        )
        .order_by('-system_created_at')[:20]
        .values(
            'id', 'quote_number', 'status', 'grand_total', 'paid_amount',
            'valid_until', 'expected_delivery_date', 'system_created_at',
        )
    )
    for q in pending_quotes:
        q['grand_total'] = str(q.get('grand_total') or 0)
        q['paid_amount'] = str(q.get('paid_amount') or 0)
        q['pending_amount'] = str(
            max(
                Decimal('0'),
                Decimal(q['grand_total']) - Decimal(q['paid_amount']),
            )
        )
        if q.get('valid_until'):
            q['valid_until'] = q['valid_until'].isoformat()
        if q.get('expected_delivery_date'):
            q['expected_delivery_date'] = q['expected_delivery_date'].isoformat()
        if q.get('system_created_at'):
            q['system_created_at'] = q['system_created_at'].isoformat()
        # Bookings with future valid_until count as stock hold / booking
        q['is_stock_hold'] = q.get('status') == CatalogueQuote.STATUS_BOOKING

    pending_orders = [q for q in pending_quotes if q.get('status') == CatalogueQuote.STATUS_ORDER]
    pending_bookings = [q for q in pending_quotes if q.get('status') == CatalogueQuote.STATUS_BOOKING]

    draft_estimate_qs = (
        CatalogueQuote.objects.filter(customer_id=pk, status=CatalogueQuote.STATUS_DRAFT)
        .order_by('-system_created_at')[:20]
    )
    draft_estimates = draft_estimate_qs.count()
    estimates_out = []
    for dq in draft_estimate_qs:
        pending_approval = CatalogueQuoteDiscountApproval.objects.filter(
            quote_id=dq.id,
            status=CatalogueQuoteDiscountApproval.STATUS_PENDING,
        ).exists()
        estimates_out.append({
            'id': dq.id,
            'quote_number': dq.quote_number,
            'status': dq.status,
            'grand_total': str(dq.grand_total or 0),
            'approval_pending': pending_approval,
            'valid_until': dq.valid_until.isoformat() if dq.valid_until else None,
            'system_created_at': dq.system_created_at.isoformat() if dq.system_created_at else None,
        })

    pending_approvals = list(
        CatalogueQuoteDiscountApproval.objects.filter(
            quote__customer_id=pk,
            status=CatalogueQuoteDiscountApproval.STATUS_PENDING,
        )
        .select_related('quote')
        .order_by('-system_created_at')[:20]
    )
    approvals_out = [
        {
            'id': a.id,
            'quote_id': a.quote_id,
            'quote_number': a.quote.quote_number if a.quote_id else None,
            'status': a.status,
            'threshold_percent': str(getattr(a, 'threshold_percent', '') or ''),
            'created_at': a.system_created_at.isoformat() if a.system_created_at else None,
        }
        for a in pending_approvals
    ]

    last_call = (
        CommunicationLog.objects.filter(
            customer_id=pk,
            channel=CommunicationLog.CHANNEL_CALL,
        )
        .order_by('-sent_at')
        .first()
    )
    last_call_at = last_call.sent_at if last_call else None

    store_contacts = list(
        CrmStoreContact.objects.filter(customer_id=pk)
        .select_related('branch', 'created_by')
        .order_by('-contacted_at')[:30]
    )
    last_store_contact = store_contacts[0] if store_contacts else None
    store_contacts_out = [
        {
            'id': c.id,
            'channel': c.channel,
            'channel_label': dict(CrmStoreContact.CHANNEL_CHOICES).get(c.channel, c.channel),
            'contact_reason': c.contact_reason,
            'contact_reason_label': dict(CrmStoreContact.REASON_CHOICES).get(
                c.contact_reason, c.contact_reason
            ),
            'remarks': c.remarks,
            'contacted_at': c.contacted_at.isoformat() if c.contacted_at else None,
            'branch_name': c.branch.name if c.branch_id and c.branch else None,
            'logged_by': (
                getattr(c.created_by, 'full_name', None)
                or getattr(c.created_by, 'username', None)
                if c.created_by_id
                else None
            ),
        }
        for c in store_contacts
    ]

    interaction_candidates = []
    if last_visit_at:
        interaction_candidates.append(('in_store', last_visit_at))
    if last_store_contact and last_store_contact.contacted_at:
        kind = (
            'in_store'
            if last_store_contact.channel == CrmStoreContact.CHANNEL_IN_STORE
            else 'call'
            if last_store_contact.channel == CrmStoreContact.CHANNEL_CALL
            else 'whatsapp'
        )
        interaction_candidates.append((kind, last_store_contact.contacted_at))
    if last_call_at:
        interaction_candidates.append(('call', last_call_at))
    if last_sale_date:
        sale_dt = datetime.combine(last_sale_date, datetime.min.time())
        if timezone.is_naive(sale_dt):
            sale_dt = timezone.make_aware(sale_dt, IST)
        interaction_candidates.append(('sale', sale_dt))
    interaction_candidates.sort(
        key=lambda x: x[1] if not timezone.is_naive(x[1]) else timezone.make_aware(x[1], IST),
        reverse=True,
    )
    last_interaction = interaction_candidates[0][0] if interaction_candidates else None
    last_interaction_at = interaction_candidates[0][1] if interaction_candidates else None

    service_tickets = list(
        CrmServiceTicket.objects.filter(customer_id=pk)
        .exclude(status=CrmServiceTicket.STATUS_CANCELLED)
        .order_by('-opened_at')[:30]
    )
    tickets_out = [
        {
            'id': t.id,
            'ticket_type': t.ticket_type,
            'status': t.status,
            'title': t.title,
            'item_description': t.item_description,
            'amount': str(t.amount or 0),
            'opened_at': t.opened_at.isoformat() if t.opened_at else None,
            'expected_ready_date': t.expected_ready_date.isoformat() if t.expected_ready_date else None,
            'closed_at': t.closed_at.isoformat() if t.closed_at else None,
        }
        for t in service_tickets
    ]
    repairs_out = [t for t in tickets_out if t['ticket_type'] == CrmServiceTicket.TYPE_REPAIR]
    exchanges_out = [t for t in tickets_out if t['ticket_type'] == CrmServiceTicket.TYPE_EXCHANGE]
    returns_out = [t for t in tickets_out if t['ticket_type'] == CrmServiceTicket.TYPE_RETURN]

    wishlist_visits = list(
        CrmCustomerVisit.objects.filter(customer_id=pk, buy_next_time=True)
        .select_related('quote')
        .order_by('-visited_at')[:10]
    )
    wishlist = []
    for v in wishlist_visits:
        products = []
        if v.quote_id:
            products = list(
                CatalogueQuoteLine.objects.filter(quote_id=v.quote_id, is_removed=False)
                .values('product_name', 'design_code', 'quantity', 'line_total')[:8]
            )
            for p in products:
                p['line_total'] = str(p.get('line_total') or 0)
        wishlist.append({
            'visit_id': v.id,
            'visited_at': v.visited_at.isoformat(),
            'quote_number': v.quote.quote_number if v.quote else None,
            'notes': v.notes,
            'products': products,
        })

    # Also unconverted drafts as wishlist-style intent
    draft_quotes = (
        CatalogueQuote.objects.filter(customer_id=pk, status=CatalogueQuote.STATUS_DRAFT)
        .order_by('-system_created_at')[:5]
    )
    for dq in draft_quotes:
        products = list(
            CatalogueQuoteLine.objects.filter(quote_id=dq.id, is_removed=False)
            .values('product_name', 'design_code', 'quantity', 'line_total')[:8]
        )
        for p in products:
            p['line_total'] = str(p.get('line_total') or 0)
        wishlist.append({
            'visit_id': None,
            'visited_at': dq.system_created_at.isoformat() if dq.system_created_at else None,
            'quote_number': dq.quote_number,
            'notes': 'Unconverted quotation',
            'products': products,
        })

    active_schemes = list(
        CustomerScheme.objects.filter(
            customer_id=pk,
            scheme_status__code__in=['ACTIVE', 'PENDING'],
        )
        .select_related('scheme', 'scheme_status')
        .order_by('-system_updated_at')[:20]
    )
    schemes_out = []
    for cs in active_schemes:
        schemes_out.append({
            'id': cs.id,
            'scheme_name': cs.scheme.scheme_name if cs.scheme else None,
            'scheme_code': cs.scheme.scheme_code if cs.scheme else None,
            'status': cs.scheme_status.code if cs.scheme_status else None,
            'monthly_amount': str(cs.monthly_amount or 0),
        })

    redeem_ready = list(
        CustomerScheme.objects.filter(
            customer_id=pk,
            scheme_status__code__in=['COMPLETED', 'MATURED', 'READY_TO_REDEEM'],
        )
        .select_related('scheme', 'scheme_status')[:10]
    )
    redeem_out = [
        {
            'id': cs.id,
            'scheme_name': cs.scheme.scheme_name if cs.scheme else None,
            'status': cs.scheme_status.code if cs.scheme_status else None,
        }
        for cs in redeem_ready
    ]

    demo_flag = (request.GET.get('demo') or '').strip().lower()
    force_demo = demo_flag in ('1', 'true', 'yes')
    force_real = demo_flag in ('0', 'false', 'no')
    sparse = inv_count == 0 and last_visit_at is None and not pending_quotes and not wishlist
    use_demo = force_demo or (sparse and not force_real)

    if use_demo:
        ltv = Decimal('230000.00')
        aov = Decimal('8214.29')
        inv_count = 28
        last_sale_date = today - timedelta(days=12)
        last_sale_amount = Decimal('18500.00')
        last_visit_at = timezone.now().astimezone(IST) - timedelta(days=3)
        year_invoices = [
            {
                'id': 90001,
                'invoice_number': 'DEMO-INV-2401',
                'invoice_date': (today - timedelta(days=12)).isoformat(),
                'total_amount': '18500.00',
                'paid_amount': '18500.00',
                'pending_amount': '0.00',
                'status': 'PAID',
            },
            {
                'id': 90002,
                'invoice_number': 'DEMO-INV-2398',
                'invoice_date': (today - timedelta(days=45)).isoformat(),
                'total_amount': '42000.00',
                'paid_amount': '42000.00',
                'pending_amount': '0.00',
                'status': 'PAID',
            },
            {
                'id': 90003,
                'invoice_number': 'DEMO-INV-2380',
                'invoice_date': (today - timedelta(days=90)).isoformat(),
                'total_amount': '27500.00',
                'paid_amount': '20000.00',
                'pending_amount': '7500.00',
                'status': 'PARTIAL',
            },
        ]
        pending_quotes = [
            {
                'id': 91001,
                'quote_number': 'DEMO-BK-101',
                'status': 'booking',
                'grand_total': '65000.00',
                'paid_amount': '15000.00',
                'pending_amount': '50000.00',
                'valid_until': (timezone.now() + timedelta(days=5)).isoformat(),
                'expected_delivery_date': (today + timedelta(days=7)).isoformat(),
                'system_created_at': (timezone.now() - timedelta(days=2)).isoformat(),
                'is_stock_hold': True,
            },
            {
                'id': 91002,
                'quote_number': 'DEMO-OR-088',
                'status': 'order',
                'grand_total': '22000.00',
                'paid_amount': '0.00',
                'pending_amount': '22000.00',
                'valid_until': (timezone.now() + timedelta(days=10)).isoformat(),
                'expected_delivery_date': (today + timedelta(days=14)).isoformat(),
                'system_created_at': (timezone.now() - timedelta(days=1)).isoformat(),
                'is_stock_hold': False,
            },
        ]
        pending_orders = [q for q in pending_quotes if q['status'] == 'order']
        pending_bookings = [q for q in pending_quotes if q['status'] == 'booking']
        estimates_out = [
            {
                'id': 91101,
                'quote_number': 'DEMO-EST-01',
                'status': 'draft',
                'grand_total': '18000.00',
                'approval_pending': True,
                'valid_until': (timezone.now() + timedelta(days=1)).isoformat(),
                'system_created_at': (timezone.now() - timedelta(hours=6)).isoformat(),
            }
        ]
        approvals_out = [
            {
                'id': 91201,
                'quote_id': 91101,
                'quote_number': 'DEMO-EST-01',
                'status': 'pending',
                'threshold_percent': '10.00',
                'created_at': (timezone.now() - timedelta(hours=5)).isoformat(),
            }
        ]
        last_interaction = 'in_store'
        last_interaction_at = last_visit_at
        last_call_at = timezone.now().astimezone(IST) - timedelta(days=9)
        repairs_out = [
            {
                'id': 94001,
                'ticket_type': 'REPAIR',
                'status': 'IN_PROGRESS',
                'title': 'Chain clasp repair',
                'item_description': '22K gold chain',
                'amount': '850.00',
                'opened_at': (timezone.now() - timedelta(days=4)).isoformat(),
                'expected_ready_date': (today + timedelta(days=3)).isoformat(),
                'closed_at': None,
            }
        ]
        exchanges_out = [
            {
                'id': 94002,
                'ticket_type': 'EXCHANGE',
                'status': 'OPEN',
                'title': 'Old gold exchange',
                'item_description': 'Bangle pair',
                'amount': '12000.00',
                'opened_at': (timezone.now() - timedelta(days=1)).isoformat(),
                'expected_ready_date': None,
                'closed_at': None,
            }
        ]
        returns_out = []
        if not store_contacts_out:
            store_contacts_out = [
                {
                    'id': 95001,
                    'channel': 'IN_STORE',
                    'channel_label': 'In store',
                    'contact_reason': 'PRODUCT_ENQUIRY',
                    'contact_reason_label': 'Product enquiry',
                    'remarks': 'Discussed 22K necklace set; will decide after festival.',
                    'contacted_at': (timezone.now() - timedelta(days=2)).isoformat(),
                    'branch_name': 'Main Branch',
                    'logged_by': 'Demo Staff',
                }
            ]
        wishlist = [
            {
                'visit_id': 92001,
                'visited_at': (timezone.now() - timedelta(days=4)).isoformat(),
                'quote_number': 'DEMO-Q-551',
                'notes': 'Will buy next time',
                'products': [
                    {'product_name': '22K Gold Necklace', 'design_code': 'NK-2201', 'quantity': 1, 'line_total': '85000.00'},
                    {'product_name': 'Diamond Studs', 'design_code': 'ER-441', 'quantity': 1, 'line_total': '32000.00'},
                ],
            },
            {
                'visit_id': None,
                'visited_at': (timezone.now() - timedelta(days=8)).isoformat(),
                'quote_number': 'DEMO-Q-540',
                'notes': 'Unconverted quotation',
                'products': [
                    {'product_name': 'Silver Anklet Pair', 'design_code': 'SL-119', 'quantity': 2, 'line_total': '4500.00'},
                ],
            },
        ]
        draft_estimates = 2
        if not schemes_out:
            schemes_out = [
                {
                    'id': 93001,
                    'scheme_name': 'Demo Gold Savings',
                    'scheme_code': 'DEMO-GS',
                    'status': 'ACTIVE',
                    'monthly_amount': '5000.00',
                }
            ]
        if not redeem_out:
            redeem_out = [
                {'id': 93002, 'scheme_name': 'Demo Matured Plan', 'status': 'COMPLETED'}
            ]
        age = _age_group(customer.date_of_birth) or '35–44'
        family = getattr(customer, 'family_group', None) or 'Demo Family'
    else:
        age = _age_group(customer.date_of_birth)
        family = getattr(customer, 'family_group', None) or None

    referral_count = Customer.objects.filter(referred_by_id=customer.id).count()
    referred_by = getattr(customer, 'referred_by', None)

    return Response({
        'customer_id': customer.id,
        'full_name': customer.full_name,
        'mobile': customer.mobile,
        'email': customer.email,
        'date_of_birth': customer.date_of_birth.isoformat() if customer.date_of_birth else None,
        'anniversary_date': customer.anniversary_date.isoformat() if getattr(customer, 'anniversary_date', None) else None,
        'wedding_date': customer.wedding_date.isoformat() if getattr(customer, 'wedding_date', None) else None,
        'family_group': family,
        'referred_by_id': customer.referred_by_id,
        'referred_by_name': referred_by.full_name if referred_by else None,
        'referral_code': getattr(customer, 'referral_code', None),
        'referral_count': referral_count,
        'age_group': age,
        'lifetime_value': str(ltv),
        'average_order_value': str(aov),
        'invoice_count': inv_count,
        'last_transaction_date': last_sale_date.isoformat() if last_sale_date else None,
        'last_transaction_days_ago': _days_ago(last_sale_date),
        'last_transaction_amount': str(last_sale_amount) if last_sale_amount is not None else None,
        'last_visit_at': last_visit_at.isoformat() if last_visit_at else None,
        'last_visit_days_ago': _days_ago(last_visit_at),
        'last_interaction': last_interaction,
        'last_interaction_at': last_interaction_at.isoformat() if last_interaction_at else None,
        'last_call_at': last_call_at.isoformat() if last_call_at else None,
        'pending_orders': pending_quotes,
        'pending_orders_count': len(pending_quotes),
        'pending_catalogue_orders': pending_orders,
        'pending_bookings': pending_bookings,
        'stock_holds': pending_bookings,
        'draft_estimates_count': draft_estimates,
        'estimates': estimates_out,
        'pending_approvals': approvals_out,
        'invoices_current_year': year_invoices,
        'wishlist': wishlist[:15],
        'active_schemes': schemes_out,
        'schemes_ready_to_redeem': redeem_out,
        'repairs': repairs_out,
        'exchanges': exchanges_out,
        'returns': returns_out,
        'store_contacts': store_contacts_out,
        'is_demo': use_demo,
    })
