"""
Catalogue quotation APIs — draft / order / booking for store assisted selling.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from shared.models import (
    CatalogueQuote,
    CatalogueQuoteChangeLog,
    CatalogueQuoteDiscountApproval,
    CatalogueQuoteLine,
    CatalogueQuoteLineRemovalRequest,
    CatalogueQuotePayment,
    Customer,
    CustomerAddress,
)
from shared.services.catalogue_quote_visit_service import (
    active_visit_payload,
    approve_line_removal,
    close_visit_for_quote,
    compute_line_sales_credit,
    DISCOUNT_APPROVAL_THRESHOLD_PCT,
    ensure_primary_contributor,
    effective_discount_limit,
    get_active_visit_for_customer,
    is_quote_contributor,
    join_quote_as_assistant,
    log_quote_change,
    maybe_create_discount_approval,
    merge_quote_lines,
    open_visit_for_quote,
    reject_line_removal,
    serialize_change_log_entry,
    serialize_discount_approval,
    serialize_removal_request,
    snapshot_sales_credit,
    sync_contributors_from_line_sales,
    update_contributor_shares,
    _contributors_payload,
    _actor_label,
    _discount_percent,
    _resolve_added_by_user,
)
from shared.services.customer_service import normalize_mobile
from shared.services.catalogue_quote_ledger_service import sync_catalogue_quote_to_customer_ledger
from shared.services.customer_store_account_service import (
    balance_snapshot_for_storage,
    get_customer_store_balance,
)
from shared.services.catalogue_quote_timer_service import (
    ensure_pricing_expires_at,
    payload_has_negotiated_pricing,
    pricing_expires_payload,
    process_quote_timers,
    quote_has_negotiated_pricing,
)
from shared.services.customer_scheme_redeem_service import (
    parse_scheme_settlements_payload,
    total_scheme_settlement_amount,
    validate_scheme_settlements,
)

# Draft quotes hold stock until end of the calendar day in IST (midnight), not rolling 24h.
QUOTE_VALIDITY_TZ = ZoneInfo('Asia/Kolkata')

TWOPLACES = Decimal('0.01')


def _d(value) -> Decimal:
    if value is None:
        return Decimal('0')
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _quote_validity_window():
    """
    valid_from = now; valid_until = next midnight IST (12:00 AM).

    Example: quote created 2:00 PM IST → valid until 12:00 AM IST that night
    (start of next calendar day), so reserved stock releases after the store day ends.
    """
    now = timezone.now()
    now_ist = timezone.localtime(now, QUOTE_VALIDITY_TZ)
    next_day = now_ist.date() + timedelta(days=1)
    valid_until = datetime.combine(next_day, time.min, tzinfo=QUOTE_VALIDITY_TZ)
    return now, valid_until


def _generate_quote_number() -> str:
    year = timezone.now().year
    prefix = f'QUO-{year}-'
    last = (
        CatalogueQuote.objects.filter(quote_number__startswith=prefix)
        .order_by('-id')
        .values_list('quote_number', flat=True)
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(str(last).split('-')[-1]) + 1
        except (ValueError, IndexError):
            seq = CatalogueQuote.objects.filter(quote_number__startswith=prefix).count() + 1
    return f'{prefix}{seq:04d}'


def _address_snapshot_from_model(addr: CustomerAddress | None) -> dict:
    if not addr:
        return {}
    return {
        'id': addr.id,
        'addressLine1': addr.address_line1 or '',
        'addressLine2': addr.address_line2 or '',
        'city': addr.city or '',
        'state': addr.state or '',
        'pincode': addr.pincode or '',
        'country': addr.country or 'India',
    }


def _address_snapshot_from_payload(raw: dict | None) -> dict:
    if not raw or not isinstance(raw, dict):
        return {}
    return {
        'id': raw.get('id'),
        'addressLine1': (raw.get('addressLine1') or raw.get('address_line1') or '').strip(),
        'addressLine2': (raw.get('addressLine2') or raw.get('address_line2') or '').strip(),
        'city': (raw.get('city') or '').strip(),
        'state': (raw.get('state') or '').strip(),
        'pincode': (raw.get('pincode') or raw.get('postal_code') or '').strip(),
        'country': (raw.get('country') or 'India').strip(),
    }


def _ensure_not_expired(quote: CatalogueQuote):
    """Apply pricing revert (3h) and stock release / status=expired (end of IST day)."""
    process_quote_timers(quote)

def _validate_status_amounts(
    quote_status: str,
    grand_total: Decimal,
    paid_amount: Decimal,
    settle_from_jama: Decimal | None = None,
    settle_from_scheme: Decimal | None = None,
):
    settle_from_jama = _d(settle_from_jama or 0)
    settle_from_scheme = _d(settle_from_scheme or 0)
    total_settled = paid_amount + settle_from_jama + settle_from_scheme

    if quote_status == CatalogueQuote.STATUS_DRAFT:
        if paid_amount != 0 or settle_from_jama != 0 or settle_from_scheme != 0:
            return (
                'Draft quotations must have paid_amount = 0 and no store/scheme settlement.'
            )
    elif quote_status == CatalogueQuote.STATUS_ORDER:
        if total_settled != grand_total:
            return (
                'Order requires cash + JAMA + kitty redeem to equal grand_total '
                f'(got {total_settled}, need {grand_total}).'
            )
    elif quote_status == CatalogueQuote.STATUS_BOOKING:
        if total_settled <= 0 or total_settled >= grand_total:
            return (
                'Booking requires partial settlement '
                '(0 < cash + JAMA + kitty < grand_total).'
            )
    else:
        return f'Invalid status: {quote_status}'
    return None


def _validate_jama_settlement(customer_id: int, settle_from_jama: Decimal) -> str | None:
    if settle_from_jama <= 0:
        return None
    bal = get_customer_store_balance(customer_id)
    available = _d(bal['jama_available'])
    if settle_from_jama > available:
        return (
            f'JAMA adjustment ({settle_from_jama}) exceeds available advance ({available}).'
        )
    return None


def _line_models_from_payload(
    quote: CatalogueQuote,
    lines_payload: list,
    admin_user=None,
) -> list[CatalogueQuoteLine]:
    rows = []
    for idx, raw in enumerate(lines_payload or [], start=1):
        qty = int(raw.get('quantity') or 1)
        if qty < 1:
            qty = 1
        unit_price = _d(raw.get('unitPrice') or raw.get('unit_price') or 0)
        line_total = raw.get('lineTotal') or raw.get('line_total')
        line_total_d = _d(line_total if line_total is not None else unit_price * qty)
        pricing_meta = raw.get('pricingMeta') or raw.get('pricing_meta') or {}
        if raw.get('adjustmentLedger'):
            pricing_meta = {**pricing_meta, 'adjustmentLedger': raw['adjustmentLedger']}
        if raw.get('baselineBreakdown'):
            pricing_meta = {**pricing_meta, 'baselineBreakdown': raw['baselineBreakdown']}
        rows.append(
            CatalogueQuoteLine(
                quote=quote,
                line_no=idx,
                product_id=str(raw.get('productId') or raw.get('product_id') or ''),
                product_name=str(raw.get('productName') or raw.get('product_name') or ''),
                design_code=str(raw.get('designCode') or raw.get('design_code') or ''),
                image=str(raw.get('image') or ''),
                variant_label=str(raw.get('variantLabel') or raw.get('variant_label') or ''),
                variant_key=str(raw.get('variantKey') or raw.get('variant_key') or ''),
                quantity=qty,
                unit_price=unit_price,
                line_total=line_total_d,
                breakdown=raw.get('breakdown') or {},
                pricing_meta=pricing_meta,
                added_by=_resolve_added_by_user(raw, admin_user),
                created_by=admin_user,
                updated_by=admin_user,
            )
        )
    return rows


def _payment_models_from_payload(quote: CatalogueQuote, payments_payload: list | None) -> list[CatalogueQuotePayment]:
    rows = []
    for raw in payments_payload or []:
        amount = _d(raw.get('amount') or 0)
        if amount <= 0:
            continue
        rows.append(
            CatalogueQuotePayment(
                quote=quote,
                mode_code=str(raw.get('mode') or raw.get('mode_code') or ''),
                mode_name=str(raw.get('modeName') or raw.get('mode_name') or ''),
                amount=amount,
                reference_no=str(raw.get('referenceNo') or raw.get('reference_no') or ''),
                notes=str(raw.get('notes') or ''),
            )
        )
    return rows


def _quote_list_item(quote: CatalogueQuote) -> dict:
    _ensure_not_expired(quote)
    item_count = quote.lines.filter(is_removed=False).count()
    created_by = quote.created_by
    sales_credit = compute_line_sales_credit(quote)
    return {
        'id': quote.quote_number,
        'quoteId': quote.id,
        'status': quote.status,
        'createdAt': quote.system_created_at.isoformat() if quote.system_created_at else None,
        'validUntil': quote.valid_until.isoformat() if quote.valid_until else None,
        'isExpired': quote.status == CatalogueQuote.STATUS_EXPIRED
        or (quote.status == CatalogueQuote.STATUS_DRAFT and timezone.now() > quote.valid_until),
        'salesperson': {
            'adminUserId': quote.created_by_id,
            'name': _actor_label(created_by),
            'username': getattr(created_by, 'username', None) if created_by else None,
        },
        'createdBy': {
            'adminUserId': quote.created_by_id,
            'name': _actor_label(created_by),
            'username': getattr(created_by, 'username', None) if created_by else None,
        },
        'salesCredit': sales_credit,
        'customer': {
            'customerId': quote.customer_id,
            'name': quote.customer_name_snapshot,
            'phone': quote.contact_mobile,
            'customerCode': quote.customer.customer_code if quote.customer_id else None,
        },
        'grandTotal': float(quote.grand_total),
        'paidAmount': float(quote.paid_amount),
        'settleFromJama': float(getattr(quote, 'settle_from_jama', 0) or 0),
        'settleFromScheme': float(getattr(quote, 'settle_from_scheme', 0) or 0),
        'pendingAmount': float(quote.pending_amount),
        'itemCount': item_count,
    }


def _line_detail_payload(ln: CatalogueQuoteLine) -> dict:
    meta = ln.pricing_meta or {}
    owner_id = ln.added_by_id
    owner_user = ln.added_by
    if not owner_id and ln.quote_id:
        quote = getattr(ln, 'quote', None)
        if quote and quote.created_by_id:
            owner_id = quote.created_by_id
            owner_user = quote.created_by
    payload = {
        'id': f'line_{ln.id}',
        'serverLineId': ln.id,
        'productId': ln.product_id,
        'productName': ln.product_name,
        'designCode': ln.design_code,
        'image': ln.image,
        'variantLabel': ln.variant_label,
        'variantKey': ln.variant_key,
        'quantity': ln.quantity,
        'unitPrice': float(ln.unit_price),
        'lineTotal': float(ln.line_total),
        'breakdown': ln.breakdown,
        'addedBy': {
            'adminUserId': owner_id,
            'name': _actor_label(owner_user),
        } if owner_id else None,
    }
    if meta:
        payload['pricingMeta'] = meta
        if meta.get('adjustmentLedger'):
            payload['adjustmentLedger'] = meta['adjustmentLedger']
        if meta.get('baselineBreakdown'):
            payload['baselineBreakdown'] = meta['baselineBreakdown']
    return payload


def _quote_detail_payload(quote: CatalogueQuote) -> dict:
    _ensure_not_expired(quote)
    lines = list(quote.lines.filter(is_removed=False).order_by('line_no', 'id'))
    payments = list(quote.payments.order_by('id'))
    snap = quote.delivery_address_snapshot or {}
    customer_form = {
        'customerId': quote.customer_id,
        'customerCode': quote.customer.customer_code if quote.customer_id else None,
        'name': quote.customer_name_snapshot,
        'phone': quote.contact_mobile,
        'email': quote.customer_email_snapshot or '',
        'notes': quote.notes or '',
        'deliveryAddress': snap,
    }
    created_by = quote.created_by
    return {
        'id': quote.quote_number,
        'quoteId': quote.id,
        'status': quote.status,
        'version': quote.version or 1,
        'saleType': quote.status if quote.status in (
            CatalogueQuote.STATUS_DRAFT,
            CatalogueQuote.STATUS_ORDER,
            CatalogueQuote.STATUS_BOOKING,
        ) else 'draft',
        'createdAt': quote.system_created_at.isoformat() if quote.system_created_at else None,
        'validFrom': quote.valid_from.isoformat() if quote.valid_from else None,
        'validUntil': quote.valid_until.isoformat() if quote.valid_until else None,
        'isExpired': quote.status == CatalogueQuote.STATUS_EXPIRED
        or (quote.status == CatalogueQuote.STATUS_DRAFT and timezone.now() > quote.valid_until),
        **pricing_expires_payload(quote),
        'createdBy': {
            'adminUserId': quote.created_by_id,
            'name': _actor_label(created_by),
            'username': getattr(created_by, 'username', None) if created_by else None,
        },
        'contributors': _contributors_payload(quote),
        'salesCredit': compute_line_sales_credit(quote),
        'salesCreditSnapshot': quote.sales_credit_snapshot or [],
        'pendingRemovalRequests': [
            serialize_removal_request(r)
            for r in quote.line_removal_requests.filter(
                status=CatalogueQuoteLineRemovalRequest.STATUS_PENDING
            ).select_related('line', 'requested_by', 'owner_sales_user', 'quote')
        ],
        'cartPricingMeta': quote.cart_pricing_meta or {},
        'customer': customer_form,
        'lines': [_line_detail_payload(ln) for ln in lines],
        'subtotal': float(quote.subtotal),
        'gstTotal': float(quote.gst_total),
        'grandTotal': float(quote.grand_total),
        'paidAmount': float(quote.paid_amount),
        'settleFromJama': float(getattr(quote, 'settle_from_jama', 0) or 0),
        'settleFromScheme': float(getattr(quote, 'settle_from_scheme', 0) or 0),
        'schemeSettlements': quote.scheme_settlements or [],
        'pendingAmount': float(quote.pending_amount),
        'accountBalanceSnapshot': quote.account_balance_snapshot or {},
        'payments': [
            {
                'mode': p.mode_code,
                'modeName': p.mode_name,
                'amount': float(p.amount),
                'referenceNo': p.reference_no,
                'notes': p.notes,
            }
            for p in payments
        ],
        'deliveryAddressId': quote.delivery_address_id,
        'deliveryAddress': snap,
        'expectedDeliveryDate': (
            quote.expected_delivery_date.isoformat()
            if getattr(quote, 'expected_delivery_date', None)
            else None
        ),
        **_sale_invoice_meta(quote),
    }


def _sale_invoice_meta(quote: CatalogueQuote) -> dict:
    inv = getattr(quote, 'sale_invoice', None)
    if quote.sale_invoice_id and inv:
        return {
            'saleInvoiceId': inv.id,
            'invoiceNumber': inv.invoice_number,
        }
    return {'saleInvoiceId': None, 'invoiceNumber': None}


def _resolve_customer_and_address(data: dict):
    customer_id = data.get('customerId') or data.get('customer_id')
    if not customer_id:
        raise ValueError('customerId is required')

    try:
        customer = Customer.objects.get(id=int(customer_id))
    except (Customer.DoesNotExist, TypeError, ValueError):
        raise ValueError('Customer not found')

    cust_payload = data.get('customer') or {}
    contact_mobile = normalize_mobile(
        data.get('contactMobile')
        or data.get('contact_mobile')
        or cust_payload.get('phone')
        or customer.mobile
    )

    delivery_address_id = (
        data.get('deliveryAddressId')
        or data.get('delivery_address_id')
        or cust_payload.get('deliveryAddressId')
    )
    addr_obj = None
    snap = _address_snapshot_from_payload(
        data.get('deliveryAddress') or data.get('delivery_address') or cust_payload.get('deliveryAddress')
    )

    if delivery_address_id:
        try:
            addr_obj = CustomerAddress.objects.get(
                id=int(delivery_address_id),
                customer_id=customer.id,
                is_active=True,
            )
            snap = _address_snapshot_from_model(addr_obj)
        except (CustomerAddress.DoesNotExist, TypeError, ValueError):
            pass
    elif snap.get('addressLine1') and snap.get('city'):
        addr_obj = CustomerAddress.objects.create(
            customer=customer,
            address_line1=snap['addressLine1'],
            address_line2=snap.get('addressLine2') or '',
            city=snap['city'],
            state=snap.get('state') or '',
            pincode=snap.get('pincode') or '',
            country=snap.get('country') or 'India',
            is_default=not CustomerAddress.objects.filter(customer=customer, is_active=True).exists(),
            is_active=True,
        )
        snap = _address_snapshot_from_model(addr_obj)

    if not snap.get('addressLine1'):
        default_addr = (
            CustomerAddress.objects.filter(customer=customer, is_active=True)
            .order_by('-is_default', 'id')
            .first()
        )
        if default_addr:
            addr_obj = default_addr
            snap = _address_snapshot_from_model(default_addr)

    name = (cust_payload.get('name') or customer.full_name or '').strip()
    email = cust_payload.get('email') or customer.email
    if email is not None and str(email).strip() == '':
        email = None
    notes = (cust_payload.get('notes') or data.get('notes') or '').strip()

    return customer, addr_obj, snap, contact_mobile, name, email, notes


def _get_quote_by_identifier(quote_id: str) -> CatalogueQuote | None:
    if not quote_id:
        return None
    qs = CatalogueQuote.objects.select_related(
        'customer', 'sale_invoice', 'created_by',
    ).prefetch_related('lines', 'payments', 'contributors__admin_user', 'line_removal_requests')
    if str(quote_id).isdigit():
        return qs.filter(Q(id=int(quote_id)) | Q(quote_number=quote_id)).first()
    return qs.filter(quote_number=quote_id).first()


def _check_quote_version(quote: CatalogueQuote, data: dict) -> Response | None:
    client_version = data.get('version')
    if client_version is None:
        return None
    try:
        client_v = int(client_version)
    except (TypeError, ValueError):
        return Response({'error': 'Invalid version.'}, status=status.HTTP_400_BAD_REQUEST)
    if client_v != (quote.version or 1):
        return Response(
            {
                'error': 'Quotation was updated by another user. Refresh and try again.',
                'code': 'version_conflict',
                'version': quote.version,
            },
            status=status.HTTP_409_CONFLICT,
        )
    return None


def _parse_removed_line_ids(data: dict) -> list[int]:
    raw = data.get('removedLineIds') or data.get('removed_line_ids') or []
    if not isinstance(raw, list):
        return []
    ids = []
    for item in raw:
        if str(item).isdigit():
            ids.append(int(item))
    return ids


@api_view(['GET', 'POST'])
@admin_auth()
def catalogue_quote_list_create(request):
    """
    GET  /master/catalogue/quotes/?status=&search=&from=&to=&page=&page_size=
    POST /master/catalogue/quotes/
    """
    if request.method == 'GET':
        qs = CatalogueQuote.objects.select_related('customer', 'created_by').prefetch_related(
            'lines', 'contributors__admin_user',
        )

        customer_id = request.GET.get('customerId') or request.GET.get('customer_id')
        if customer_id and str(customer_id).isdigit():
            qs = qs.filter(customer_id=int(customer_id))

        salesperson_id = request.GET.get('salespersonId') or request.GET.get('salesperson_id')
        if salesperson_id and str(salesperson_id).isdigit():
            qs = qs.filter(created_by_id=int(salesperson_id))

        active_only = (request.GET.get('activeOnly') or request.GET.get('active_only') or '').strip().lower()
        if active_only in ('1', 'true', 'yes'):
            qs = qs.filter(
                status=CatalogueQuote.STATUS_DRAFT,
                valid_until__gte=timezone.now(),
            )

        status_filter = (request.GET.get('status') or '').strip().lower()
        if status_filter in (
            CatalogueQuote.STATUS_DRAFT,
            CatalogueQuote.STATUS_ORDER,
            CatalogueQuote.STATUS_BOOKING,
            CatalogueQuote.STATUS_CANCELLED,
            CatalogueQuote.STATUS_EXPIRED,
        ):
            qs = qs.filter(status=status_filter)

        search = (request.GET.get('search') or '').strip()
        if search:
            qs = qs.filter(
                Q(quote_number__icontains=search)
                | Q(customer_name_snapshot__icontains=search)
                | Q(contact_mobile__icontains=search)
                | Q(customer__customer_code__icontains=search)
            )

        date_from = request.GET.get('from')
        date_to = request.GET.get('to')
        if date_from:
            qs = qs.filter(system_created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(system_created_at__date__lte=date_to)

        qs = qs.order_by('-system_created_at')

        try:
            page = max(1, int(request.GET.get('page') or 1))
        except ValueError:
            page = 1
        try:
            page_size = min(100, max(1, int(request.GET.get('page_size') or 20)))
        except ValueError:
            page_size = 20

        total = qs.count()
        start = (page - 1) * page_size
        rows = [_quote_list_item(q) for q in qs[start : start + page_size]]

        return Response({'count': total, 'results': rows, 'page': page, 'page_size': page_size})

    # POST — create quote
    data = request.data or {}
    quote_status = (data.get('status') or data.get('saleType') or CatalogueQuote.STATUS_DRAFT).strip().lower()
    if quote_status not in (
        CatalogueQuote.STATUS_DRAFT,
        CatalogueQuote.STATUS_ORDER,
        CatalogueQuote.STATUS_BOOKING,
    ):
        return Response({'error': f'Invalid status: {quote_status}'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        customer, addr_obj, snap, contact_mobile, name, email, notes = _resolve_customer_and_address(data)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if quote_status == CatalogueQuote.STATUS_DRAFT:
        existing_visit = get_active_visit_for_customer(customer.id)
        if existing_visit:
            return Response(
                {
                    'error': 'An open quotation already exists for this customer. Join as assistant instead.',
                    'code': 'active_visit_exists',
                    **active_visit_payload(existing_visit),
                },
                status=status.HTTP_409_CONFLICT,
            )

    lines_payload = data.get('lines') or []
    if not lines_payload:
        return Response({'error': 'At least one line item is required.'}, status=status.HTTP_400_BAD_REQUEST)

    subtotal = _d(data.get('subtotal'))
    gst_total = _d(data.get('gstTotal') or data.get('gst_total'))
    grand_total = _d(data.get('grandTotal') or data.get('grand_total'))
    paid_amount = _d(data.get('paidAmount') or data.get('paid_amount'))
    settle_from_jama = _d(data.get('settleFromJama') or data.get('settle_from_jama'))
    scheme_settlements = parse_scheme_settlements_payload(
        data.get('schemeSettlements') or data.get('scheme_settlements')
    )
    settle_from_scheme = total_scheme_settlement_amount(scheme_settlements)
    explicit_scheme = data.get('settleFromScheme') or data.get('settle_from_scheme')
    if explicit_scheme is not None:
        settle_from_scheme = _d(explicit_scheme)
        if scheme_settlements and settle_from_scheme != total_scheme_settlement_amount(scheme_settlements):
            return Response(
                {'error': 'settleFromScheme must match sum of schemeSettlements.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    payments_payload = data.get('payments')
    if payments_payload and paid_amount == 0:
        paid_amount = sum(_d(p.get('amount')) for p in payments_payload)

    err = _validate_jama_settlement(customer.id, settle_from_jama)
    if err:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    err = validate_scheme_settlements(customer.id, scheme_settlements)
    if err:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    err = _validate_status_amounts(
        quote_status, grand_total, paid_amount, settle_from_jama, settle_from_scheme
    )
    if err:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    account_snapshot = (
        balance_snapshot_for_storage(get_customer_store_balance(customer.id))
        if quote_status != CatalogueQuote.STATUS_DRAFT
        else {}
    )

    valid_from, valid_until = _quote_validity_window()

    admin_user = getattr(request, 'admin_user', None)

    expected_delivery_raw = (
        data.get('expectedDeliveryDate')
        or data.get('expected_delivery_date')
    )
    expected_delivery_date = None
    if expected_delivery_raw:
        from django.utils.dateparse import parse_date
        expected_delivery_date = parse_date(str(expected_delivery_raw)[:10])

    with transaction.atomic():
        quote = CatalogueQuote.objects.create(
            quote_number=_generate_quote_number(),
            status=quote_status,
            customer=customer,
            contact_mobile=contact_mobile,
            customer_name_snapshot=name,
            customer_email_snapshot=email,
            notes=notes,
            delivery_address=addr_obj,
            delivery_address_snapshot=snap,
            expected_delivery_date=expected_delivery_date,
            subtotal=subtotal,
            gst_total=gst_total,
            grand_total=grand_total,
            paid_amount=paid_amount,
            settle_from_jama=settle_from_jama,
            settle_from_scheme=settle_from_scheme,
            scheme_settlements=scheme_settlements,
            account_balance_snapshot=account_snapshot,
            valid_from=valid_from,
            valid_until=valid_until,
            created_by=admin_user,
            updated_by=admin_user,
        )
        line_rows = _line_models_from_payload(quote, lines_payload, admin_user)
        CatalogueQuoteLine.objects.bulk_create(line_rows)

        payment_rows = _payment_models_from_payload(quote, payments_payload)
        if payment_rows:
            CatalogueQuotePayment.objects.bulk_create(payment_rows)

        cart_meta = data.get('cartPricingMeta') or data.get('cart_pricing_meta')
        if isinstance(cart_meta, dict) and cart_meta:
            quote.cart_pricing_meta = cart_meta
            quote.save(update_fields=['cart_pricing_meta', 'system_updated_at'])

        if quote_status == CatalogueQuote.STATUS_DRAFT:
            has_negotiated = payload_has_negotiated_pricing(
                lines_payload if isinstance(lines_payload, list) else None,
                cart_meta if isinstance(cart_meta, dict) else None,
            ) or quote_has_negotiated_pricing(quote)
            if ensure_pricing_expires_at(quote, has_negotiated=has_negotiated):
                quote.save(update_fields=['pricing_expires_at', 'system_updated_at'])

        ensure_primary_contributor(quote, admin_user)
        if quote_status == CatalogueQuote.STATUS_DRAFT:
            branch_id = data.get('branchId') or data.get('branch_id') or data.get('storeId') or data.get('store_id')
            try:
                branch_id = int(branch_id) if branch_id not in (None, '') else None
            except (TypeError, ValueError):
                branch_id = None
            open_visit_for_quote(quote, admin_user, branch_id=branch_id)
        log_quote_change(
            quote,
            actor=admin_user,
            action=CatalogueQuoteChangeLog.ACTION_QUOTE_CREATED,
            summary=f'Quotation {quote.quote_number} created',
            payload={
                'status': quote_status,
                'grandTotal': float(grand_total),
                'baseline': True,
                'lines': [
                    {
                        'productName': ln.product_name,
                        'designCode': ln.design_code,
                        'quantity': int(ln.quantity or 1),
                        'lineTotal': float(ln.line_total),
                    }
                    for ln in quote.lines.filter(is_removed=False)
                ],
            },
        )
        for ln in quote.lines.filter(is_removed=False):
            log_quote_change(
                quote,
                actor=admin_user,
                action=CatalogueQuoteChangeLog.ACTION_LINE_ADDED,
                summary=f'Added {ln.product_name}',
                line=ln,
                payload={
                    'productName': ln.product_name,
                    'lineTotal': float(ln.line_total),
                    'after': {
                        'quantity': int(ln.quantity or 1),
                        'lineTotal': float(ln.line_total),
                        'unitPrice': float(ln.unit_price or 0),
                    },
                    'baseline': True,
                },
            )
        sync_contributors_from_line_sales(quote)

    if quote_status in (CatalogueQuote.STATUS_BOOKING, CatalogueQuote.STATUS_ORDER):
        sync_catalogue_quote_to_customer_ledger(quote)
        quote.refresh_from_db(fields=['sale_invoice_id'])

    return Response(
        {
            'id': quote.quote_number,
            'quoteId': quote.id,
            'status': quote.status,
            'createdAt': quote.system_created_at.isoformat(),
            'validUntil': quote.valid_until.isoformat(),
            **pricing_expires_payload(quote),
            **_sale_invoice_meta(quote),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET', 'PATCH'])
@admin_auth()
def catalogue_quote_detail(request, quote_id: str):
    """
    GET   /master/catalogue/quotes/<quote_id>/
    PATCH /master/catalogue/quotes/<quote_id>/  (draft only, not expired)
    """
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(_quote_detail_payload(quote))

    _ensure_not_expired(quote)
    if quote.status == CatalogueQuote.STATUS_EXPIRED:
        return Response(
            {'error': 'Quotation has expired. Create a new quotation.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if quote.status not in (CatalogueQuote.STATUS_DRAFT,):
        return Response(
            {'error': 'Only draft quotations can be edited.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    data = request.data or {}
    admin_user = getattr(request, 'admin_user', None)

    if not is_quote_contributor(quote, admin_user):
        try:
            join_quote_as_assistant(quote, admin_user)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    version_err = _check_quote_version(quote, data)
    if version_err:
        return version_err

    try:
        customer, addr_obj, snap, contact_mobile, name, email, notes = _resolve_customer_and_address(
            {**data, 'customerId': data.get('customerId') or quote.customer_id, 'customer': data.get('customer') or {
                'name': quote.customer_name_snapshot,
                'phone': quote.contact_mobile,
                'email': quote.customer_email_snapshot,
                'notes': quote.notes,
                'deliveryAddress': quote.delivery_address_snapshot,
            }}
        )
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    lines_payload = data.get('lines')
    subtotal = _d(data.get('subtotal', quote.subtotal))
    gst_total = _d(data.get('gstTotal', quote.gst_total))
    grand_total = _d(data.get('grandTotal', quote.grand_total))
    removed_line_ids = _parse_removed_line_ids(data)
    cart_meta = data.get('cartPricingMeta') or data.get('cart_pricing_meta')
    change_reason = str(data.get('changeReason') or data.get('change_reason') or '').strip()

    merge_result: dict = {}
    with transaction.atomic():
        quote.customer = customer
        quote.contact_mobile = contact_mobile
        quote.customer_name_snapshot = name
        quote.customer_email_snapshot = email
        quote.notes = notes
        quote.delivery_address = addr_obj
        quote.delivery_address_snapshot = snap
        quote.subtotal = subtotal
        quote.gst_total = gst_total
        quote.grand_total = grand_total
        quote.updated_by = admin_user
        if 'expectedDeliveryDate' in data or 'expected_delivery_date' in data:
            from django.utils.dateparse import parse_date
            raw_ed = data.get('expectedDeliveryDate', data.get('expected_delivery_date'))
            quote.expected_delivery_date = parse_date(str(raw_ed)[:10]) if raw_ed else None
        if isinstance(cart_meta, dict):
            if cart_meta:
                before_grand = float(quote.cart_pricing_meta.get('beforeGrandTotal') or quote.grand_total or 0) if isinstance(quote.cart_pricing_meta, dict) else float(quote.grand_total or 0)
                after_grand = float(cart_meta.get('targetGrandTotal') or cart_meta.get('afterGrandTotal') or grand_total or 0)
                if not before_grand:
                    before_grand = float(cart_meta.get('beforeGrandTotal') or grand_total or 0)
                discount_pct = float(_discount_percent(before_grand, after_grand))
                actor_limit = float(effective_discount_limit(admin_user))
                quote.cart_pricing_meta = cart_meta
                cart_entry = log_quote_change(
                    quote,
                    actor=admin_user,
                    action=CatalogueQuoteChangeLog.ACTION_CART_DISCOUNT,
                    summary='Cart-wide discount updated',
                    payload={
                        **cart_meta,
                        'before': {'grandTotal': before_grand},
                        'after': {'grandTotal': after_grand},
                        'beforeLineTotal': before_grand,
                        'afterLineTotal': after_grand,
                        'discountPercent': discount_pct,
                        'allowedDiscountPercent': actor_limit,
                        'requiresApproval': discount_pct > actor_limit,
                        'reason': change_reason,
                    },
                    reason=change_reason,
                )
                approval = maybe_create_discount_approval(
                    quote,
                    actor=admin_user,
                    change_log=cart_entry,
                    before_amount=before_grand,
                    after_amount=after_grand,
                    reason=change_reason,
                )
                if approval:
                    merge_result.setdefault('pendingDiscountApprovals', []).append(
                        serialize_discount_approval(approval)
                    )
            elif cart_meta == {} and quote.cart_pricing_meta:
                quote.cart_pricing_meta = {}
        quote.save()

        if lines_payload is not None:
            line_merge = merge_quote_lines(
                quote,
                lines_payload,
                admin_user,
                removed_line_ids=removed_line_ids,
                change_reason=change_reason,
            )
            for key, val in line_merge.items():
                if key == 'pendingDiscountApprovals':
                    merge_result.setdefault('pendingDiscountApprovals', []).extend(val or [])
                else:
                    merge_result[key] = val
        else:
            quote.version = (quote.version or 1) + 1
            quote.save(update_fields=['version', 'system_updated_at'])
            sync_contributors_from_line_sales(quote)

        has_negotiated = payload_has_negotiated_pricing(
            lines_payload if isinstance(lines_payload, list) else None,
            cart_meta if isinstance(cart_meta, dict) else None,
        ) or quote_has_negotiated_pricing(quote)
        if ensure_pricing_expires_at(quote, has_negotiated=has_negotiated):
            quote.save(update_fields=['pricing_expires_at', 'system_updated_at'])

        log_quote_change(
            quote,
            actor=admin_user,
            action=CatalogueQuoteChangeLog.ACTION_QUOTE_UPDATED,
            summary='Quotation details updated',
            payload={'grandTotal': float(grand_total), 'reason': change_reason},
            reason=change_reason,
        )

    quote.refresh_from_db()
    payload = _quote_detail_payload(quote)
    if merge_result.get('pendingRemovalRequests'):
        payload['pendingRemovalRequests'] = merge_result['pendingRemovalRequests']
        payload['removalApprovalRequired'] = True
    return Response(payload)


@api_view(['PATCH'])
@admin_auth()
def catalogue_quote_status(request, quote_id: str):
    """
    PATCH /master/catalogue/quotes/<quote_id>/status/
    Body: { status, paidAmount, payments? }
    """
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    _ensure_not_expired(quote)
    data = request.data or {}
    new_status = (data.get('status') or data.get('saleType') or '').strip().lower()
    if new_status not in (
        CatalogueQuote.STATUS_ORDER,
        CatalogueQuote.STATUS_BOOKING,
        CatalogueQuote.STATUS_CANCELLED,
    ):
        return Response({'error': 'Invalid status transition.'}, status=status.HTTP_400_BAD_REQUEST)

    if quote.status == CatalogueQuote.STATUS_EXPIRED or (
        quote.status == CatalogueQuote.STATUS_DRAFT and timezone.now() > quote.valid_until
    ):
        if new_status in (CatalogueQuote.STATUS_ORDER, CatalogueQuote.STATUS_BOOKING):
            return Response(
                {'error': 'Quotation has expired. Create a new quotation to proceed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if quote.status not in (CatalogueQuote.STATUS_DRAFT, CatalogueQuote.STATUS_BOOKING):
        return Response(
            {'error': f'Cannot transition from status {quote.status}.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if new_status == CatalogueQuote.STATUS_BOOKING and quote.status == CatalogueQuote.STATUS_BOOKING:
        return Response({'error': 'Already a booking.'}, status=status.HTTP_400_BAD_REQUEST)

    grand_total = quote.grand_total
    paid_amount = _d(data.get('paidAmount') or data.get('paid_amount') or quote.paid_amount)
    settle_from_jama = _d(
        data.get('settleFromJama')
        or data.get('settle_from_jama')
        or getattr(quote, 'settle_from_jama', 0)
    )
    scheme_settlements = parse_scheme_settlements_payload(
        data.get('schemeSettlements')
        or data.get('scheme_settlements')
        or getattr(quote, 'scheme_settlements', None)
    )
    if data.get('schemeSettlements') is not None or data.get('scheme_settlements') is not None:
        settle_from_scheme = total_scheme_settlement_amount(scheme_settlements)
    else:
        settle_from_scheme = _d(
            data.get('settleFromScheme')
            or data.get('settle_from_scheme')
            or getattr(quote, 'settle_from_scheme', 0)
        )
    payments_payload = data.get('payments')

    if new_status == CatalogueQuote.STATUS_ORDER and quote.status == CatalogueQuote.STATUS_BOOKING:
        pending = grand_total - settle_from_jama - settle_from_scheme
        if paid_amount < pending:
            paid_amount = pending

    err = _validate_jama_settlement(quote.customer_id, settle_from_jama)
    if err and new_status != CatalogueQuote.STATUS_CANCELLED:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    err = validate_scheme_settlements(quote.customer_id, scheme_settlements)
    if err and new_status != CatalogueQuote.STATUS_CANCELLED:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    err = _validate_status_amounts(
        new_status, grand_total, paid_amount, settle_from_jama, settle_from_scheme
    )
    if err and new_status != CatalogueQuote.STATUS_CANCELLED:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    account_snapshot = (
        balance_snapshot_for_storage(get_customer_store_balance(quote.customer_id))
        if new_status in (CatalogueQuote.STATUS_BOOKING, CatalogueQuote.STATUS_ORDER)
        else {}
    )

    admin_user = getattr(request, 'admin_user', None)

    with transaction.atomic():
        quote.status = new_status
        if new_status != CatalogueQuote.STATUS_CANCELLED:
            quote.paid_amount = paid_amount
            quote.settle_from_jama = settle_from_jama
            quote.settle_from_scheme = settle_from_scheme
            quote.scheme_settlements = scheme_settlements
            quote.account_balance_snapshot = account_snapshot
        quote.updated_by = admin_user
        quote.save(
            update_fields=[
                'status',
                'paid_amount',
                'settle_from_jama',
                'settle_from_scheme',
                'scheme_settlements',
                'account_balance_snapshot',
                'updated_by',
                'system_updated_at',
            ]
        )

        if payments_payload is not None and new_status in (
            CatalogueQuote.STATUS_ORDER,
            CatalogueQuote.STATUS_BOOKING,
        ):
            quote.payments.all().delete()
            rows = _payment_models_from_payload(quote, payments_payload)
            if rows:
                CatalogueQuotePayment.objects.bulk_create(rows)

        log_quote_change(
            quote,
            actor=admin_user,
            action=CatalogueQuoteChangeLog.ACTION_STATUS_CHANGED,
            summary=f'Status changed to {new_status}',
            payload={'status': new_status, 'paidAmount': float(paid_amount)},
        )

        if new_status in (CatalogueQuote.STATUS_ORDER, CatalogueQuote.STATUS_BOOKING):
            snapshot_sales_credit(quote)
            close_visit_for_quote(quote)
        elif new_status == CatalogueQuote.STATUS_CANCELLED:
            close_visit_for_quote(quote)

    if new_status in (CatalogueQuote.STATUS_BOOKING, CatalogueQuote.STATUS_ORDER):
        sync_catalogue_quote_to_customer_ledger(quote)

    payload = _quote_detail_payload(quote)
    return Response(
        {
            'id': quote.quote_number,
            'status': quote.status,
            'paidAmount': float(quote.paid_amount),
            'pendingAmount': float(quote.pending_amount),
            **payload,
        }
    )


@api_view(['GET'])
@admin_auth()
def catalogue_quote_pdf(request, quote_id: str):
    """
    GET /master/catalogue/quotes/<quote_id>/pdf/
    POS-style tax invoice PDF (same generator as Store POS).
    """
    from shared.services.pos_receipt_pdf import build_pos_invoice_pdf_bytes
    from shared.services.catalogue_invoice_service import ensure_catalogue_sale_invoice

    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    if quote.status not in (CatalogueQuote.STATUS_ORDER, CatalogueQuote.STATUS_BOOKING):
        return Response(
            {'error': 'PDF is available for saved orders and bookings only.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    admin_user = getattr(request, 'admin_user', None)
    try:
        if not quote.sale_invoice_id:
            ensure_catalogue_sale_invoice(quote, created_by=admin_user)
            quote.refresh_from_db(fields=['sale_invoice_id'])
        invoice = quote.sale_invoice
        if not invoice:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        pdf_bytes = build_pos_invoice_pdf_bytes(invoice)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    safe = ''.join(c if c.isalnum() or c in '-_' else '-' for c in invoice.invoice_number)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Receipt-{safe}.pdf"'
    return response


@api_view(['POST'])
@admin_auth()
def catalogue_quote_duplicate(request, quote_id: str):
    """
    POST /master/catalogue/quotes/<quote_id>/duplicate/
    Copy an expired or draft quote into a new end-of-day draft.
    """
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    detail = _quote_detail_payload(quote)
    valid_from, valid_until = _quote_validity_window()
    admin_user = getattr(request, 'admin_user', None)

    with transaction.atomic():
        new_quote = CatalogueQuote.objects.create(
            quote_number=_generate_quote_number(),
            status=CatalogueQuote.STATUS_DRAFT,
            customer=quote.customer,
            contact_mobile=quote.contact_mobile,
            customer_name_snapshot=quote.customer_name_snapshot,
            customer_email_snapshot=quote.customer_email_snapshot,
            notes=quote.notes,
            delivery_address=quote.delivery_address,
            delivery_address_snapshot=quote.delivery_address_snapshot,
            subtotal=quote.subtotal,
            gst_total=quote.gst_total,
            grand_total=quote.grand_total,
            paid_amount=Decimal('0'),
            settle_from_jama=Decimal('0'),
            account_balance_snapshot={},
            valid_from=valid_from,
            valid_until=valid_until,
            created_by=admin_user,
            updated_by=admin_user,
        )
        line_rows = []
        for ln in quote.lines.filter(is_removed=False).order_by('line_no', 'id'):
            line_rows.append(
                CatalogueQuoteLine(
                    quote=new_quote,
                    line_no=ln.line_no,
                    product_id=ln.product_id,
                    product_name=ln.product_name,
                    design_code=ln.design_code,
                    image=ln.image,
                    variant_label=ln.variant_label,
                    variant_key=ln.variant_key,
                    quantity=ln.quantity,
                    unit_price=ln.unit_price,
                    line_total=ln.line_total,
                    breakdown=ln.breakdown,
                    pricing_meta=ln.pricing_meta or {},
                    added_by=ln.added_by,
                )
            )
        if line_rows:
            CatalogueQuoteLine.objects.bulk_create(line_rows)

        ensure_primary_contributor(new_quote, admin_user)
        branch_id = None
        raw_branch = request.data.get('branchId') or request.data.get('branch_id') if hasattr(request, 'data') else None
        try:
            branch_id = int(raw_branch) if raw_branch not in (None, '') else None
        except (TypeError, ValueError):
            branch_id = None
        open_visit_for_quote(new_quote, admin_user, branch_id=branch_id)

    return Response(
        {
            'id': new_quote.quote_number,
            'quoteId': new_quote.id,
            'status': new_quote.status,
            'validUntil': new_quote.valid_until.isoformat(),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
@admin_auth()
def catalogue_quote_active_visit(request):
    """
    GET /master/catalogue/quotes/active-visit/?customerId=
    Returns open draft visit for a customer, if any.
    """
    customer_id = request.GET.get('customerId') or request.GET.get('customer_id')
    if not customer_id or not str(customer_id).isdigit():
        return Response({'error': 'customerId is required.'}, status=status.HTTP_400_BAD_REQUEST)

    visit = get_active_visit_for_customer(int(customer_id))
    if not visit:
        return Response({'active': False})

    return Response({'active': True, **active_visit_payload(visit)})


@api_view(['POST'])
@admin_auth()
def catalogue_quote_join(request, quote_id: str):
    """
    POST /master/catalogue/quotes/<quote_id>/join/
    Body: { sharePercent?: number, lines?: [...] }
    Optional lines are merged after joining (e.g. products added before linking the customer).
    """
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    _ensure_not_expired(quote)
    if quote.status != CatalogueQuote.STATUS_DRAFT:
        return Response(
            {'error': 'Only open draft quotations can be joined.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    admin_user = getattr(request, 'admin_user', None)
    data = request.data or {}
    share_raw = data.get('sharePercent') or data.get('share_percent')
    share = _d(share_raw) if share_raw is not None else None

    try:
        join_quote_as_assistant(quote, admin_user, share)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    lines_payload = data.get('lines')
    if lines_payload is not None:
        try:
            merge_quote_lines(quote, lines_payload, admin_user)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    quote.refresh_from_db()
    return Response(_quote_detail_payload(quote))


@api_view(['PATCH'])
@admin_auth()
def catalogue_quote_contributors(request, quote_id: str):
    """
    PATCH /master/catalogue/quotes/<quote_id>/contributors/
    Body: { contributors: [{ adminUserId, sharePercent }, ...] }
    """
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    if quote.status != CatalogueQuote.STATUS_DRAFT:
        return Response(
            {'error': 'Contributors can only be updated on draft quotations.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    admin_user = getattr(request, 'admin_user', None)
    data = request.data or {}
    shares = data.get('contributors') or data.get('shares') or []

    try:
        payload = update_contributor_shares(quote, shares, admin_user)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'contributors': payload, 'version': quote.version})


@api_view(['GET'])
@admin_auth()
def catalogue_quote_changes(request, quote_id: str):
    """
    GET /master/catalogue/quotes/<quote_id>/changes/?since=&limit=
    Review Changes feed for multi-user quotation tracking.
    """
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    qs = CatalogueQuoteChangeLog.objects.filter(quote=quote).select_related('actor', 'line')

    since = request.GET.get('since')
    if since:
        qs = qs.filter(created_at__gte=since)

    try:
        limit = min(200, max(1, int(request.GET.get('limit') or 50)))
    except ValueError:
        limit = 50

    rows = [serialize_change_log_entry(e) for e in qs.order_by('-created_at', '-id')[:limit]]
    pending_approvals = [
        serialize_discount_approval(a)
        for a in CatalogueQuoteDiscountApproval.objects.filter(
            quote=quote,
            status=CatalogueQuoteDiscountApproval.STATUS_PENDING,
        ).select_related('requested_by', 'reviewed_by', 'line').order_by('-id')[:50]
    ]
    return Response({
        'count': len(rows),
        'results': rows,
        'version': quote.version,
        'pendingDiscountApprovals': pending_approvals,
        'discountApprovalThresholdPercent': float(effective_discount_limit(getattr(request, 'admin_user', None))),
    })


@api_view(['GET'])
@admin_auth()
def catalogue_quote_discount_approvals_list(request, quote_id: str):
    """
    GET /master/catalogue/quotes/<quote_id>/discount-approvals/?status=pending
    """
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)

    qs = CatalogueQuoteDiscountApproval.objects.filter(quote=quote).select_related(
        'requested_by', 'reviewed_by', 'line',
    )
    status_filter = (request.GET.get('status') or '').strip().lower()
    if status_filter:
        qs = qs.filter(status=status_filter)
    rows = [serialize_discount_approval(a) for a in qs.order_by('-id')[:100]]
    return Response({'count': len(rows), 'results': rows})


@api_view(['POST'])
@admin_auth()
def catalogue_quote_discount_approval_approve(request, quote_id: str, approval_id: int):
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)
    admin_user = getattr(request, 'admin_user', None)
    row = CatalogueQuoteDiscountApproval.objects.filter(quote=quote, id=approval_id).first()
    if not row:
        return Response({'error': 'Approval not found'}, status=status.HTTP_404_NOT_FOUND)
    if row.status != CatalogueQuoteDiscountApproval.STATUS_PENDING:
        return Response({'error': 'Approval is not pending'}, status=status.HTTP_400_BAD_REQUEST)
    notes = str((request.data or {}).get('notes') or '').strip()
    row.status = CatalogueQuoteDiscountApproval.STATUS_APPROVED
    row.reviewed_by = admin_user
    row.reviewed_at = timezone.now()
    row.review_notes = notes
    row.updated_by = admin_user
    row.save()
    log_quote_change(
        quote,
        actor=admin_user,
        action=CatalogueQuoteChangeLog.ACTION_QUOTE_UPDATED,
        summary=f'Discount approval accepted ({row.discount_percent}%)',
        line=row.line,
        payload={'discountApprovalId': row.id, 'approved': True, 'discountPercent': float(row.discount_percent)},
        reason=notes,
    )
    return Response(serialize_discount_approval(row))


@api_view(['POST'])
@admin_auth()
def catalogue_quote_discount_approval_reject(request, quote_id: str, approval_id: int):
    quote = _get_quote_by_identifier(quote_id)
    if not quote:
        return Response({'error': 'Quote not found'}, status=status.HTTP_404_NOT_FOUND)
    admin_user = getattr(request, 'admin_user', None)
    row = CatalogueQuoteDiscountApproval.objects.filter(quote=quote, id=approval_id).first()
    if not row:
        return Response({'error': 'Approval not found'}, status=status.HTTP_404_NOT_FOUND)
    if row.status != CatalogueQuoteDiscountApproval.STATUS_PENDING:
        return Response({'error': 'Approval is not pending'}, status=status.HTTP_400_BAD_REQUEST)
    notes = str((request.data or {}).get('notes') or '').strip()
    row.status = CatalogueQuoteDiscountApproval.STATUS_REJECTED
    row.reviewed_by = admin_user
    row.reviewed_at = timezone.now()
    row.review_notes = notes
    row.updated_by = admin_user
    row.save()
    log_quote_change(
        quote,
        actor=admin_user,
        action=CatalogueQuoteChangeLog.ACTION_QUOTE_UPDATED,
        summary=f'Discount approval rejected ({row.discount_percent}%)',
        line=row.line,
        payload={'discountApprovalId': row.id, 'rejected': True, 'discountPercent': float(row.discount_percent)},
        reason=notes,
    )
    return Response(serialize_discount_approval(row))


@api_view(['GET'])
@admin_auth()
def catalogue_quote_removal_requests_list(request):
    """
    GET /master/catalogue/quotes/removal-requests/?status=pending&mine=1&quoteId=
    List line removal approval requests (for owner salesperson inbox).
    """
    admin_user = getattr(request, 'admin_user', None)
    qs = CatalogueQuoteLineRemovalRequest.objects.select_related(
        'quote', 'line', 'requested_by', 'owner_sales_user',
    )

    status_filter = (request.GET.get('status') or 'pending').strip().lower()
    if status_filter:
        qs = qs.filter(status=status_filter)

    mine = (request.GET.get('mine') or '').strip().lower()
    if mine in ('1', 'true', 'yes') and admin_user:
        qs = qs.filter(owner_sales_user_id=admin_user.id)

    as_requester = (request.GET.get('asRequester') or request.GET.get('as_requester') or '').strip().lower()
    if as_requester in ('1', 'true', 'yes') and admin_user:
        qs = qs.filter(requested_by_id=admin_user.id)

    quote_id = request.GET.get('quoteId') or request.GET.get('quote_id')
    if quote_id:
        if str(quote_id).isdigit():
            qs = qs.filter(Q(quote_id=int(quote_id)) | Q(quote__quote_number=quote_id))
        else:
            qs = qs.filter(quote__quote_number=quote_id)

    rows = [serialize_removal_request(r) for r in qs.order_by('-system_created_at')[:100]]
    return Response({'count': len(rows), 'results': rows})


@api_view(['POST'])
@admin_auth()
def catalogue_quote_removal_request_approve(request, request_id: int):
    """POST /master/catalogue/quotes/removal-requests/<id>/approve/"""
    admin_user = getattr(request, 'admin_user', None)
    try:
        req = approve_line_removal(request_id, admin_user)
    except CatalogueQuoteLineRemovalRequest.DoesNotExist:
        return Response({'error': 'Request not found'}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        'request': serialize_removal_request(req),
        'quote': _quote_detail_payload(req.quote),
    })


@api_view(['POST'])
@admin_auth()
def catalogue_quote_removal_request_reject(request, request_id: int):
    """POST /master/catalogue/quotes/removal-requests/<id>/reject/  Body: { notes? }"""
    admin_user = getattr(request, 'admin_user', None)
    data = request.data or {}
    notes = (data.get('notes') or data.get('reviewNotes') or '').strip()
    try:
        req = reject_line_removal(request_id, admin_user, notes)
    except CatalogueQuoteLineRemovalRequest.DoesNotExist:
        return Response({'error': 'Request not found'}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'request': serialize_removal_request(req)})
