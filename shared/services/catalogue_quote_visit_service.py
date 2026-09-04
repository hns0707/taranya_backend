"""
Multi-user catalogue quotation: visits, contributors, change log, line merge.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from shared.services.catalogue_availability_service import max_addable_qty
from shared.models import (
    CatalogueQuote,
    CatalogueQuoteChangeLog,
    CatalogueQuoteContributor,
    CatalogueQuoteDiscountApproval,
    CatalogueQuoteLine,
    CatalogueQuoteLineRemovalRequest,
    CatalogueQuoteVisit,
    AdminUser,
)

TWOPLACES = Decimal('0.01')
# Fallback when user/role has no max_discount_percent configured.
DISCOUNT_APPROVAL_THRESHOLD_PCT = Decimal('10')


def _d(value) -> Decimal:
    if value is None:
        return Decimal('0')
    return Decimal(str(value)).quantize(TWOPLACES)


def _parse_max_discount_percent(raw, default: Decimal | None = None) -> Decimal:
    fallback = default if default is not None else DISCOUNT_APPROVAL_THRESHOLD_PCT
    if raw is None or raw == '':
        return fallback
    try:
        value = Decimal(str(raw))
    except Exception:
        return fallback
    if value < 0:
        value = Decimal('0')
    if value > 100:
        value = Decimal('100')
    return value.quantize(TWOPLACES)


def effective_discount_limit(admin_user) -> Decimal:
    """
    Max discount %% the actor may apply without approval.
    Prefers AdminUser.max_discount_percent; falls back to global default.
    Super admins with no explicit value default to 100%%.
    """
    if admin_user is None:
        return DISCOUNT_APPROVAL_THRESHOLD_PCT
    raw = getattr(admin_user, 'max_discount_percent', None)
    if raw is not None:
        return _parse_max_discount_percent(raw)
    if getattr(admin_user, 'is_super_admin', False):
        return Decimal('100')
    return DISCOUNT_APPROVAL_THRESHOLD_PCT


def _actor_label(user) -> str:
    if not user:
        return 'System'
    return getattr(user, 'full_name', None) or getattr(user, 'username', None) or 'Staff'


def _discount_percent(before_amount, after_amount) -> Decimal:
    before = _d(before_amount)
    after = _d(after_amount)
    if before <= 0:
        return Decimal('0')
    saved = before - after
    if saved <= 0:
        return Decimal('0')
    return (saved * Decimal('100') / before).quantize(TWOPLACES)


def _baseline_line_total(existing: CatalogueQuoteLine, fields: dict | None = None) -> Decimal:
    meta = {}
    if fields and isinstance(fields.get('pricing_meta'), dict):
        meta = fields['pricing_meta']
    elif isinstance(existing.pricing_meta, dict):
        meta = existing.pricing_meta
    baseline = meta.get('baselineBreakdown') or {}
    unit = baseline.get('finalPrice')
    qty = fields.get('quantity') if fields else existing.quantity
    if unit is not None:
        return _d(unit) * _d(qty or 1)
    return _d(existing.line_total)


def log_quote_change(
    quote: CatalogueQuote,
    *,
    actor,
    action: str,
    summary: str,
    payload: dict | None = None,
    line: CatalogueQuoteLine | None = None,
    reason: str = '',
) -> CatalogueQuoteChangeLog:
    return CatalogueQuoteChangeLog.objects.create(
        quote=quote,
        actor=actor,
        action=action,
        line=line,
        summary=summary[:512],
        payload=payload or {},
        reason=(reason or '')[:512],
    )


def maybe_create_discount_approval(
    quote: CatalogueQuote,
    *,
    actor,
    change_log: CatalogueQuoteChangeLog,
    before_amount,
    after_amount,
    line: CatalogueQuoteLine | None = None,
    reason: str = '',
    threshold: Decimal | None = None,
) -> CatalogueQuoteDiscountApproval | None:
    """Create pending manager approval when discount exceeds the actor's allowed max %."""
    thr = threshold if threshold is not None else effective_discount_limit(actor)
    pct = _discount_percent(before_amount, after_amount)
    if pct <= thr:
        return None
    # One open request per quote+line (or cart-wide when line is None)
    pending = CatalogueQuoteDiscountApproval.objects.filter(
        quote=quote,
        line=line,
        status=CatalogueQuoteDiscountApproval.STATUS_PENDING,
    ).first()
    if pending:
        pending.change_log = change_log
        pending.discount_percent = pct
        pending.before_amount = _d(before_amount)
        pending.after_amount = _d(after_amount)
        pending.threshold_percent = thr
        pending.request_notes = (reason or pending.request_notes or '')[:2000]
        pending.requested_by = actor
        pending.updated_by = actor
        pending.save()
        return pending
    return CatalogueQuoteDiscountApproval.objects.create(
        quote=quote,
        change_log=change_log,
        line=line,
        requested_by=actor,
        created_by=actor,
        updated_by=actor,
        discount_percent=pct,
        before_amount=_d(before_amount),
        after_amount=_d(after_amount),
        threshold_percent=thr,
        request_notes=(reason or '')[:2000],
    )


def serialize_discount_approval(row: CatalogueQuoteDiscountApproval) -> dict:
    return {
        'id': row.id,
        'status': row.status,
        'discountPercent': float(row.discount_percent or 0),
        'beforeAmount': float(row.before_amount or 0),
        'afterAmount': float(row.after_amount or 0),
        'thresholdPercent': float(row.threshold_percent or 10),
        'requestNotes': row.request_notes or '',
        'reviewNotes': row.review_notes or '',
        'lineId': row.line_id,
        'productName': row.line.product_name if row.line_id and row.line else None,
        'changeLogId': row.change_log_id,
        'createdAt': row.system_created_at.isoformat() if row.system_created_at else None,
        'reviewedAt': row.reviewed_at.isoformat() if row.reviewed_at else None,
        'requestedBy': {
            'adminUserId': row.requested_by_id,
            'name': _actor_label(row.requested_by),
        },
        'reviewedBy': {
            'adminUserId': row.reviewed_by_id,
            'name': _actor_label(row.reviewed_by),
        } if row.reviewed_by_id else None,
    }


def get_active_visit_for_customer(customer_id: int) -> CatalogueQuoteVisit | None:
    return (
        CatalogueQuoteVisit.objects.select_related(
            'quote', 'primary_sales_user', 'customer',
        )
        .prefetch_related('quote__contributors__admin_user')
        .filter(
            customer_id=customer_id,
            status=CatalogueQuoteVisit.STATUS_OPEN,
            quote__status=CatalogueQuote.STATUS_DRAFT,
        )
        .order_by('-system_created_at')
        .first()
    )


def open_visit_for_quote(quote: CatalogueQuote, primary_user, branch_id: int | None = None) -> CatalogueQuoteVisit:
    visit, _ = CatalogueQuoteVisit.objects.get_or_create(
        quote=quote,
        defaults={
            'customer_id': quote.customer_id,
            'primary_sales_user': primary_user,
            'status': CatalogueQuoteVisit.STATUS_OPEN,
            'created_by': primary_user,
            'updated_by': primary_user,
        },
    )
    try:
        from shared.services.crm_visit_service import record_crm_visit_from_quote
        from shared.models import CrmCustomerVisit

        source = CrmCustomerVisit.SOURCE_CATALOGUE
        record_crm_visit_from_quote(
            quote,
            catalogue_visit=visit,
            primary_user=primary_user,
            branch_id=branch_id,
            source=source,
        )
    except Exception:
        # CRM visit logging must not block POS quotation flow
        import logging
        logging.getLogger(__name__).exception('Failed to record CRM visit for quote %s', quote.id)
    return visit


def ensure_primary_contributor(quote: CatalogueQuote, admin_user, share_percent=Decimal('100')) -> CatalogueQuoteContributor:
    row, created = CatalogueQuoteContributor.objects.get_or_create(
        quote=quote,
        admin_user=admin_user,
        defaults={
            'role': CatalogueQuoteContributor.ROLE_PRIMARY,
            'share_percent': share_percent,
            'created_by': admin_user,
            'updated_by': admin_user,
        },
    )
    if not created and row.role != CatalogueQuoteContributor.ROLE_PRIMARY:
        row.role = CatalogueQuoteContributor.ROLE_PRIMARY
        row.save(update_fields=['role', 'system_updated_at'])
    return row


def is_quote_contributor(quote: CatalogueQuote, admin_user) -> bool:
    if not admin_user:
        return False
    if quote.created_by_id == admin_user.id:
        return True
    return CatalogueQuoteContributor.objects.filter(quote=quote, admin_user=admin_user).exists()


@transaction.atomic
def join_quote_as_assistant(
    quote: CatalogueQuote,
    admin_user,
    share_percent: Decimal | None = None,
) -> CatalogueQuoteContributor:
    """Join as assistant — sales credit is calculated from line totals, not fixed %."""
    if quote.status != CatalogueQuote.STATUS_DRAFT:
        raise ValueError('Only draft quotations can be joined.')

    existing = CatalogueQuoteContributor.objects.filter(quote=quote, admin_user=admin_user).first()
    if existing:
        return existing

    row = CatalogueQuoteContributor.objects.create(
        quote=quote,
        admin_user=admin_user,
        role=CatalogueQuoteContributor.ROLE_ASSISTANT,
        share_percent=Decimal('0'),
        created_by=admin_user,
        updated_by=admin_user,
    )

    log_quote_change(
        quote,
        actor=admin_user,
        action=CatalogueQuoteChangeLog.ACTION_CONTRIBUTOR_JOINED,
        summary=f'{_actor_label(admin_user)} joined as assistant (credit based on line sales)',
        payload={'sharePercent': 0},
    )
    sync_contributors_from_line_sales(quote)
    return row


def compute_line_sales_credit(quote: CatalogueQuote) -> list[dict]:
    """Sales credit % derived from each salesperson's active line totals."""
    lines = quote.lines.filter(is_removed=False).select_related('added_by')
    by_user: dict[int, dict] = {}
    grand = Decimal('0')

    for ln in lines:
        uid = ln.added_by_id or quote.created_by_id
        if not uid:
            continue
        amount = _d(ln.line_total)
        grand += amount
        if uid not in by_user:
            user = ln.added_by or (quote.created_by if quote.created_by_id == uid else None)
            by_user[uid] = {
                'adminUserId': uid,
                'name': _actor_label(user),
                'username': getattr(user, 'username', None) if user else None,
                'lineTotal': Decimal('0'),
                'lineCount': 0,
            }
        by_user[uid]['lineTotal'] += amount
        by_user[uid]['lineCount'] += 1

    if grand <= 0:
        primary = quote.created_by
        if primary:
            return [{
                'adminUserId': primary.id,
                'name': _actor_label(primary),
                'username': getattr(primary, 'username', None),
                'lineTotal': 0.0,
                'lineCount': 0,
                'sharePercent': 100.0,
            }]
        return []

    rows = []
    for uid, data in by_user.items():
        pct = (data['lineTotal'] / grand * Decimal('100')).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        rows.append({
            'adminUserId': uid,
            'name': data['name'],
            'username': data['username'],
            'lineTotal': float(data['lineTotal']),
            'lineCount': data['lineCount'],
            'sharePercent': float(pct),
        })
    rows.sort(key=lambda r: (-r['sharePercent'], r['name']))
    return rows


def sync_contributors_from_line_sales(quote: CatalogueQuote) -> list[dict]:
    """Keep contributor rows in sync with line-based credit."""
    credit_rows = compute_line_sales_credit(quote)
    credit_by_user = {r['adminUserId']: r for r in credit_rows}

    for uid, row in credit_by_user.items():
        contrib, created = CatalogueQuoteContributor.objects.get_or_create(
            quote=quote,
            admin_user_id=uid,
            defaults={
                'role': CatalogueQuoteContributor.ROLE_PRIMARY
                if uid == quote.created_by_id
                else CatalogueQuoteContributor.ROLE_ASSISTANT,
                'share_percent': _d(row['sharePercent']),
                'created_by_id': uid,
                'updated_by_id': uid,
            },
        )
        if not created:
            contrib.share_percent = _d(row['sharePercent'])
            contrib.save(update_fields=['share_percent', 'system_updated_at'])

    return credit_rows


def _contributors_payload(quote: CatalogueQuote) -> list[dict]:
    """Contributors with share % from line sales (falls back to stored rows)."""
    credit = compute_line_sales_credit(quote)
    if credit:
        return [
            {
                'adminUserId': r['adminUserId'],
                'name': r['name'],
                'username': r.get('username'),
                'role': (
                    CatalogueQuoteContributor.ROLE_PRIMARY
                    if r['adminUserId'] == quote.created_by_id
                    else CatalogueQuoteContributor.ROLE_ASSISTANT
                ),
                'sharePercent': r['sharePercent'],
                'lineTotal': r.get('lineTotal', 0),
                'lineCount': r.get('lineCount', 0),
            }
            for r in credit
        ]
    rows = []
    for c in quote.contributors.select_related('admin_user').order_by('role', 'id'):
        u = c.admin_user
        rows.append({
            'adminUserId': c.admin_user_id,
            'name': _actor_label(u),
            'username': getattr(u, 'username', None),
            'role': c.role,
            'sharePercent': float(c.share_percent),
            'lineTotal': 0,
            'lineCount': 0,
        })
    return rows


@transaction.atomic
def update_contributor_shares(quote: CatalogueQuote, shares: list[dict], actor) -> list[dict]:
    total = Decimal('0')
    parsed = []
    for raw in shares:
        uid = raw.get('adminUserId') or raw.get('admin_user_id')
        pct = _d(raw.get('sharePercent') or raw.get('share_percent'))
        if not uid:
            continue
        parsed.append((int(uid), pct))
        total += pct

    if total != Decimal('100'):
        raise ValueError(f'Share percentages must total 100 (got {total}).')

    for uid, pct in parsed:
        CatalogueQuoteContributor.objects.filter(quote=quote, admin_user_id=uid).update(
            share_percent=pct,
            updated_by=actor,
        )

    log_quote_change(
        quote,
        actor=actor,
        action=CatalogueQuoteChangeLog.ACTION_SHARE_UPDATED,
        summary='Sales credit shares updated',
        payload={'contributors': _contributors_payload(quote)},
    )
    return _contributors_payload(quote)


def close_visit_for_quote(quote: CatalogueQuote) -> None:
    visit = getattr(quote, 'visit', None)
    if visit is None:
        try:
            visit = CatalogueQuoteVisit.objects.get(quote=quote)
        except CatalogueQuoteVisit.DoesNotExist:
            return
    if visit.status == CatalogueQuoteVisit.STATUS_CLOSED:
        return
    visit.status = CatalogueQuoteVisit.STATUS_CLOSED
    visit.closed_at = timezone.now()
    visit.save(update_fields=['status', 'closed_at', 'system_updated_at'])


def snapshot_sales_credit(quote: CatalogueQuote) -> list[dict]:
    snap = compute_line_sales_credit(quote)
    quote.sales_credit_snapshot = snap
    quote.save(update_fields=['sales_credit_snapshot', 'system_updated_at'])
    return snap


def _parse_line_server_id(raw: dict) -> int | None:
    for key in ('serverLineId', 'server_line_id', 'lineId', 'line_id'):
        val = raw.get(key)
        if val is not None and str(val).isdigit():
            return int(val)
    lid = raw.get('id')
    if lid is not None and str(lid).startswith('line_') is False and str(lid).isdigit():
        return int(lid)
    return None


def _pricing_meta_from_payload(raw: dict) -> dict:
    meta = raw.get('pricingMeta') or raw.get('pricing_meta') or {}
    if not isinstance(meta, dict):
        meta = {}
    if raw.get('adjustmentLedger'):
        meta['adjustmentLedger'] = raw['adjustmentLedger']
    if raw.get('baselineBreakdown'):
        meta['baselineBreakdown'] = raw['baselineBreakdown']
    return meta


def _resolve_added_by_user(raw: dict, admin_user):
    """New lines belong to the salesperson who added them (must match the saving user)."""
    ab = raw.get('addedBy') or raw.get('added_by')
    if isinstance(ab, dict):
        uid = ab.get('adminUserId') or ab.get('admin_user_id')
        try:
            if uid is not None and int(uid) == int(admin_user.id):
                return admin_user
        except (TypeError, ValueError):
            pass
    return admin_user


def _line_fields_from_payload(raw: dict, quote: CatalogueQuote, admin_user, line_no: int) -> dict:
    qty = int(raw.get('quantity') or 1)
    if qty < 1:
        qty = 1
    unit_price = _d(raw.get('unitPrice') or raw.get('unit_price') or 0)
    line_total = raw.get('lineTotal') or raw.get('line_total')
    line_total_d = _d(line_total if line_total is not None else unit_price * qty)
    return {
        'quote': quote,
        'line_no': line_no,
        'product_id': str(raw.get('productId') or raw.get('product_id') or ''),
        'product_name': str(raw.get('productName') or raw.get('product_name') or ''),
        'design_code': str(raw.get('designCode') or raw.get('design_code') or ''),
        'image': str(raw.get('image') or ''),
        'variant_label': str(raw.get('variantLabel') or raw.get('variant_label') or ''),
        'variant_key': str(raw.get('variantKey') or raw.get('variant_key') or ''),
        'quantity': qty,
        'unit_price': unit_price,
        'line_total': line_total_d,
        'breakdown': raw.get('breakdown') or {},
        'pricing_meta': _pricing_meta_from_payload(raw),
        'added_by': _resolve_added_by_user(raw, admin_user),
        'created_by': admin_user,
        'updated_by': admin_user,
    }


def _line_changed(existing: CatalogueQuoteLine, fields: dict) -> bool:
    checks = (
        ('product_id', 'product_id'),
        ('product_name', 'product_name'),
        ('quantity', 'quantity'),
        ('unit_price', 'unit_price'),
        ('line_total', 'line_total'),
        ('breakdown', 'breakdown'),
        ('pricing_meta', 'pricing_meta'),
    )
    for attr, key in checks:
        if getattr(existing, attr) != fields.get(key):
            return True
    return False


def _line_owner_user_id(line: CatalogueQuoteLine) -> int | None:
    """Who gets credit / must approve removal — added_by, else quote creator."""
    if line.added_by_id:
        return line.added_by_id
    quote = getattr(line, 'quote', None)
    if quote and quote.created_by_id:
        return quote.created_by_id
    return None


def _requires_removal_approval(line: CatalogueQuoteLine, admin_user) -> bool:
    if not admin_user:
        return False
    owner_id = _line_owner_user_id(line)
    if not owner_id:
        return False
    return owner_id != admin_user.id


def _notify_removal_request(removal: CatalogueQuoteLineRemovalRequest) -> None:
    from shared.models import AdminNotification, AdminUserNotification

    quote = removal.quote
    line = removal.line
    requester = removal.requested_by
    owner = removal.owner_sales_user
    if not owner:
        return
    owner_id = owner.id
    msg = (
        f'{_actor_label(requester)} requested removal of "{line.product_name}" '
        f'from quotation {quote.quote_number}. '
        f'Request ID: {removal.id}'
    )
    notification = AdminNotification.objects.create(
        title='Quotation line removal approval',
        section_code='CATALOGUE_QUOTE',
        type='QUOTE_LINE_REMOVAL',
        customer_id=quote.customer_id,
        message=msg,
        created_by=requester,
        updated_by=requester,
    )
    AdminUserNotification.objects.create(
        admin_user_id=owner_id,
        notification=notification,
        created_by=requester,
        updated_by=requester,
    )


def serialize_removal_request(req: CatalogueQuoteLineRemovalRequest) -> dict:
    line = req.line
    return {
        'id': req.id,
        'status': req.status,
        'quoteId': req.quote_id,
        'quoteNumber': req.quote.quote_number if req.quote_id else None,
        'lineId': req.line_id,
        'productName': line.product_name if line else '',
        'lineTotal': float(line.line_total) if line else 0,
        'requestedBy': {
            'adminUserId': req.requested_by_id,
            'name': _actor_label(req.requested_by),
        },
        'ownerSalesUser': {
            'adminUserId': req.owner_sales_user_id,
            'name': _actor_label(req.owner_sales_user),
        },
        'requestNotes': req.request_notes or '',
        'reviewNotes': req.review_notes or '',
        'createdAt': req.system_created_at.isoformat() if req.system_created_at else None,
        'reviewedAt': req.reviewed_at.isoformat() if req.reviewed_at else None,
    }


def _create_pending_removal(quote, line, admin_user) -> CatalogueQuoteLineRemovalRequest:
    existing = CatalogueQuoteLineRemovalRequest.objects.filter(
        quote=quote,
        line=line,
        status=CatalogueQuoteLineRemovalRequest.STATUS_PENDING,
    ).first()
    if existing:
        return existing

    owner_id = _line_owner_user_id(line)
    if not owner_id:
        raise ValueError('Cannot request removal: line owner unknown.')

    req = CatalogueQuoteLineRemovalRequest.objects.create(
        quote=quote,
        line=line,
        requested_by=admin_user,
        owner_sales_user_id=owner_id,
        status=CatalogueQuoteLineRemovalRequest.STATUS_PENDING,
        created_by=admin_user,
        updated_by=admin_user,
    )
    log_quote_change(
        quote,
        actor=admin_user,
        action=CatalogueQuoteChangeLog.ACTION_LINE_UPDATED,
        summary=f'Removal approval requested for {line.product_name} (owner: {_actor_label(line.added_by)})',
        line=line,
        payload={'removalRequestId': req.id, 'pending': True},
    )
    _notify_removal_request(req)
    return req


def _soft_remove_line(quote, line, admin_user) -> None:
    line.is_removed = True
    line.removed_at = timezone.now()
    line.removed_by = admin_user
    line.updated_by = admin_user
    line.save(update_fields=['is_removed', 'removed_at', 'removed_by', 'updated_by', 'system_updated_at'])
    added_by_name = _actor_label(line.added_by)
    log_quote_change(
        quote,
        actor=admin_user,
        action=CatalogueQuoteChangeLog.ACTION_LINE_REMOVED,
        summary=f'Removed {line.product_name} (added by {added_by_name})',
        line=line,
        payload={
            'productId': line.product_id,
            'productName': line.product_name,
            'lineTotal': float(line.line_total),
            'addedByUserId': line.added_by_id,
        },
    )


@transaction.atomic
def approve_line_removal(request_id: int, reviewer) -> CatalogueQuoteLineRemovalRequest:
    req = CatalogueQuoteLineRemovalRequest.objects.select_related(
        'quote', 'line', 'requested_by', 'owner_sales_user',
    ).get(pk=request_id)
    if req.status != CatalogueQuoteLineRemovalRequest.STATUS_PENDING:
        raise ValueError('Request is not pending.')
    if reviewer.id != req.owner_sales_user_id:
        raise ValueError('Only the line owner salesperson can approve this removal.')

    _soft_remove_line(req.quote, req.line, reviewer)
    req.status = CatalogueQuoteLineRemovalRequest.STATUS_APPROVED
    req.reviewed_by = reviewer
    req.reviewed_at = timezone.now()
    req.updated_by = reviewer
    req.save()
    sync_contributors_from_line_sales(req.quote)
    return req


@transaction.atomic
def reject_line_removal(request_id: int, reviewer, notes: str = '') -> CatalogueQuoteLineRemovalRequest:
    req = CatalogueQuoteLineRemovalRequest.objects.select_related('quote', 'line').get(pk=request_id)
    if req.status != CatalogueQuoteLineRemovalRequest.STATUS_PENDING:
        raise ValueError('Request is not pending.')
    if reviewer.id != req.owner_sales_user_id:
        raise ValueError('Only the line owner salesperson can reject this removal.')

    req.status = CatalogueQuoteLineRemovalRequest.STATUS_REJECTED
    req.reviewed_by = reviewer
    req.reviewed_at = timezone.now()
    req.review_notes = (notes or '').strip()
    req.updated_by = reviewer
    req.save()
    log_quote_change(
        req.quote,
        actor=reviewer,
        action=CatalogueQuoteChangeLog.ACTION_LINE_UPDATED,
        summary=f'Rejected removal of {req.line.product_name}',
        line=req.line,
        payload={'removalRequestId': req.id, 'rejected': True},
    )
    return req


@transaction.atomic
def merge_quote_lines(
    quote: CatalogueQuote,
    lines_payload: list,
    admin_user,
    *,
    removed_line_ids: list[int] | None = None,
    change_reason: str = '',
) -> dict:
    """Merge incoming cart lines — soft-delete missing, upsert present, log all changes."""
    removed_line_ids = set(removed_line_ids or [])
    incoming_ids: set[int] = set()
    pending_removals: list[dict] = []
    pending_discount_approvals: list[dict] = []
    reason = (change_reason or '').strip()

    for raw in lines_payload or []:
        sid = _parse_line_server_id(raw)
        if sid:
            incoming_ids.add(sid)

    active_lines = list(
        quote.lines.filter(is_removed=False).order_by('line_no', 'id')
    )

    for ln in active_lines:
        should_remove = ln.id in removed_line_ids or ln.id not in incoming_ids
        if not should_remove:
            continue

        if _requires_removal_approval(ln, admin_user):
            req = _create_pending_removal(quote, ln, admin_user)
            pending_removals.append(serialize_removal_request(req))
            continue

        _soft_remove_line(quote, ln, admin_user)

    def _infer_line_source(raw: dict) -> str:
        row_type = str(raw.get("rowType") or raw.get("row_type") or "").strip().lower()
        if row_type in ("virtual", "barcode"):
            return row_type
        meta = raw.get("pricingMeta") or raw.get("pricing_meta") or {}
        if isinstance(meta, dict):
            rt = str(meta.get("rowType") or meta.get("row_type") or "").strip().lower()
            if rt in ("virtual", "barcode"):
                return rt
        vk = str(raw.get("variantKey") or raw.get("variant_key") or "")
        if vk.startswith("barcode|") or "|tag|" in vk:
            return "barcode"
        return "virtual"

    sources = {_infer_line_source(raw) for raw in (lines_payload or []) if isinstance(raw, dict)}
    if len(sources) > 1:
        raise ValueError(
            "Cannot mix Virtual (make-to-order) and Barcode (in-stock) products on one quotation. "
            "Use separate quotations for each catalogue type."
        )

    running_by_product: dict[str, int] = {}
    for raw in lines_payload or []:
        pid = str(raw.get("productId") or raw.get("product_id") or "").strip()
        if not pid.isdigit():
            continue
        qty = int(raw.get("quantity") or 1)
        if qty < 1:
            qty = 1
        running_by_product[pid] = running_by_product.get(pid, 0) + qty

    for pid, total_qty in running_by_product.items():
        cap = max_addable_qty(int(pid), exclude_quote_id=quote.id, already_in_session=0)
        if cap is not None and total_qty > cap:
            raise ValueError(
                f"Only {cap} piece(s) available for this product (requested {total_qty})."
            )

    line_no = 1
    for raw in lines_payload or []:
        sid = _parse_line_server_id(raw)
        fields = _line_fields_from_payload(raw, quote, admin_user, line_no)

        if sid:
            existing = quote.lines.filter(id=sid, is_removed=False).first()
            if existing:
                if _line_changed(existing, fields):
                    before_total = float(existing.line_total)
                    before_qty = int(existing.quantity or 1)
                    before_breakdown = existing.breakdown if isinstance(existing.breakdown, dict) else {}
                    for key in (
                        'line_no', 'product_id', 'product_name', 'design_code', 'image',
                        'variant_label', 'variant_key', 'quantity', 'unit_price',
                        'line_total', 'breakdown', 'pricing_meta',
                    ):
                        setattr(existing, key, fields[key])
                    existing.updated_by = admin_user
                    existing.save()
                    meta = fields['pricing_meta'] or {}
                    after_total = float(existing.line_total)
                    baseline_total = float(_baseline_line_total(existing, fields))
                    compare_before = baseline_total if baseline_total > 0 else before_total
                    discount_pct = float(_discount_percent(compare_before, after_total))
                    after_breakdown = existing.breakdown if isinstance(existing.breakdown, dict) else {}
                    payload = {
                        'productName': existing.product_name,
                        'designCode': existing.design_code,
                        'before': {
                            'quantity': before_qty,
                            'lineTotal': before_total,
                            'unitPrice': float(before_breakdown.get('finalPrice') or existing.unit_price or 0),
                            'breakdown': before_breakdown,
                        },
                        'after': {
                            'quantity': int(existing.quantity or 1),
                            'lineTotal': after_total,
                            'unitPrice': float(after_breakdown.get('finalPrice') or existing.unit_price or 0),
                            'breakdown': after_breakdown,
                        },
                        'baselineLineTotal': baseline_total,
                        'beforeLineTotal': before_total,
                        'afterLineTotal': after_total,
                        'discountPercent': discount_pct,
                        'allowedDiscountPercent': float(effective_discount_limit(admin_user)),
                        'requiresApproval': discount_pct > float(effective_discount_limit(admin_user)),
                        'adjustmentLedger': meta.get('adjustmentLedger') or [],
                        'reason': reason,
                    }
                    if meta.get('adjustmentLedger'):
                        entry = log_quote_change(
                            quote,
                            actor=admin_user,
                            action=CatalogueQuoteChangeLog.ACTION_DISCOUNT_APPLIED,
                            summary=f'Discount/pricing updated on {existing.product_name}',
                            line=existing,
                            payload=payload,
                            reason=reason,
                        )
                        approval = maybe_create_discount_approval(
                            quote,
                            actor=admin_user,
                            change_log=entry,
                            before_amount=compare_before,
                            after_amount=after_total,
                            line=existing,
                            reason=reason,
                        )
                        if approval:
                            pending_discount_approvals.append(serialize_discount_approval(approval))
                            payload['approvalId'] = approval.id
                            entry.payload = payload
                            entry.save(update_fields=['payload'])
                    else:
                        log_quote_change(
                            quote,
                            actor=admin_user,
                            action=CatalogueQuoteChangeLog.ACTION_LINE_UPDATED,
                            summary=f'Updated {existing.product_name}',
                            line=existing,
                            payload=payload,
                            reason=reason,
                        )
                else:
                    existing.line_no = line_no
                    existing.save(update_fields=['line_no', 'system_updated_at'])
                line_no += 1
                continue

        new_line = CatalogueQuoteLine.objects.create(**fields)
        log_quote_change(
            quote,
            actor=admin_user,
            action=CatalogueQuoteChangeLog.ACTION_LINE_ADDED,
            summary=f'Added {new_line.product_name}',
            line=new_line,
            payload={
                'productId': new_line.product_id,
                'productName': new_line.product_name,
                'lineTotal': float(new_line.line_total),
                'after': {
                    'quantity': int(new_line.quantity or 1),
                    'lineTotal': float(new_line.line_total),
                    'unitPrice': float(new_line.unit_price or 0),
                },
                'reason': reason,
            },
            reason=reason,
        )
        line_no += 1

    quote.version = (quote.version or 1) + 1
    quote.save(update_fields=['version', 'system_updated_at'])
    sales_credit = sync_contributors_from_line_sales(quote)
    return {
        'pendingRemovalRequests': pending_removals,
        'pendingDiscountApprovals': pending_discount_approvals,
        'salesCredit': sales_credit,
    }


def serialize_change_log_entry(entry: CatalogueQuoteChangeLog) -> dict:
    actor = entry.actor
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    approval = None
    try:
        approval = (
            entry.discount_approvals.order_by('-id').first()
            if hasattr(entry, 'discount_approvals')
            else None
        )
    except Exception:
        approval = None
    return {
        'id': entry.id,
        'action': entry.action,
        'summary': entry.summary,
        'reason': entry.reason or payload.get('reason') or '',
        'payload': payload,
        'lineId': entry.line_id,
        'createdAt': entry.created_at.isoformat() if entry.created_at else None,
        'actor': {
            'adminUserId': entry.actor_id,
            'name': _actor_label(actor),
            'username': getattr(actor, 'username', None) if actor else None,
        },
        'approval': serialize_discount_approval(approval) if approval else None,
    }


def active_visit_payload(visit: CatalogueQuoteVisit) -> dict:
    quote = visit.quote
    primary = visit.primary_sales_user
    return {
        'visitId': visit.id,
        'customerId': visit.customer_id,
        'quoteId': quote.id,
        'quoteNumber': quote.quote_number,
        'status': visit.status,
        'primarySalesUser': {
            'adminUserId': primary.id if primary else None,
            'name': _actor_label(primary),
        },
        'contributors': _contributors_payload(quote),
        'version': quote.version,
        'validUntil': quote.valid_until.isoformat() if quote.valid_until else None,
    }
