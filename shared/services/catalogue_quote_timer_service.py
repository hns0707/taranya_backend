"""
Catalogue quotation timers:

1) Pricing hold (3h) — after first negotiated rate/discount, adjusted prices expire
   and lines revert to baseline (actual) price. Timer end is fixed (not restarted).

2) Stock hold (until midnight IST / valid_until) — draft quotes past valid_until become expired
   so reserved products are released; quote row is kept.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db import transaction
from django.utils import timezone
from datetime import timedelta

from shared.models import CatalogueQuote, CatalogueQuoteLine
from shared.services.catalogue_quote_visit_service import close_visit_for_quote

TWOPLACES = Decimal('0.01')
PRICING_HOLD_HOURS = 3


def _d(value) -> Decimal:
    if value is None or value == '':
        return Decimal('0')
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _line_has_negotiated_pricing(line: CatalogueQuoteLine) -> bool:
    meta = line.pricing_meta or {}
    ledger = meta.get('adjustmentLedger') or meta.get('adjustment_ledger') or []
    if isinstance(ledger, list) and len(ledger) > 0:
        return True
    baseline = meta.get('baselineBreakdown') or meta.get('baseline_breakdown')
    if not baseline or not isinstance(baseline, dict):
        return False
    bd = line.breakdown or {}
    if not isinstance(bd, dict):
        return False
    try:
        return _d(bd.get('finalPrice') or bd.get('final')) != _d(
            baseline.get('finalPrice') or baseline.get('final')
        )
    except Exception:
        return False


def _cart_has_negotiated_pricing(quote: CatalogueQuote) -> bool:
    meta = quote.cart_pricing_meta or {}
    if not isinstance(meta, dict) or not meta:
        return False
    return any(
        meta.get(k) is not None
        for k in ('targetGrandTotal', 'target_grand_total', 'beforeGrandTotal', 'savedAmount')
    )


def quote_has_negotiated_pricing(quote: CatalogueQuote, lines: list[CatalogueQuoteLine] | None = None) -> bool:
    if _cart_has_negotiated_pricing(quote):
        return True
    qs = lines if lines is not None else list(quote.lines.filter(is_removed=False))
    return any(_line_has_negotiated_pricing(ln) for ln in qs)


def payload_has_negotiated_pricing(lines_payload: list | None, cart_meta: dict | None) -> bool:
    if isinstance(cart_meta, dict) and cart_meta:
        if any(
            cart_meta.get(k) is not None
            for k in ('targetGrandTotal', 'target_grand_total', 'beforeGrandTotal', 'savedAmount')
        ):
            return True
    if not lines_payload:
        return False
    for raw in lines_payload:
        if not isinstance(raw, dict):
            continue
        meta = raw.get('pricingMeta') or raw.get('pricing_meta') or {}
        ledger = (
            meta.get('adjustmentLedger')
            or meta.get('adjustment_ledger')
            or raw.get('adjustmentLedger')
            or []
        )
        if isinstance(ledger, list) and len(ledger) > 0:
            return True
        baseline = (
            meta.get('baselineBreakdown')
            or meta.get('baseline_breakdown')
            or raw.get('baselineBreakdown')
        )
        bd = raw.get('breakdown') or {}
        if isinstance(baseline, dict) and isinstance(bd, dict):
            try:
                if _d(bd.get('finalPrice') or bd.get('final')) != _d(
                    baseline.get('finalPrice') or baseline.get('final')
                ):
                    return True
            except Exception:
                pass
    return False


def ensure_pricing_expires_at(quote: CatalogueQuote, *, has_negotiated: bool) -> bool:
    """
    Set pricing_expires_at once when negotiated pricing first appears.
    Does not restart an existing timer.
    Returns True if field was set/changed.
    """
    if quote.pricing_expires_at is not None:
        return False
    if not has_negotiated:
        return False
    quote.pricing_expires_at = timezone.now() + timedelta(hours=PRICING_HOLD_HOURS)
    return True


def _revert_line_to_baseline(line: CatalogueQuoteLine) -> bool:
    meta = dict(line.pricing_meta or {})
    baseline = meta.get('baselineBreakdown') or meta.get('baseline_breakdown')
    if not isinstance(baseline, dict) or not baseline:
        # No baseline — just clear ledger if present
        if meta.get('adjustmentLedger') or meta.get('adjustment_ledger'):
            meta.pop('adjustmentLedger', None)
            meta.pop('adjustment_ledger', None)
            line.pricing_meta = meta
            line.save(update_fields=['pricing_meta', 'system_updated_at'])
            return True
        return False

    final = _d(baseline.get('finalPrice') or baseline.get('final'))
    qty = max(1, int(line.quantity or 1))
    line.breakdown = baseline
    line.unit_price = final
    line.line_total = (final * Decimal(qty)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)

    meta.pop('adjustmentLedger', None)
    meta.pop('adjustment_ledger', None)
    baseline_rules = meta.get('baselineMakingChargeRules') or meta.get('baseline_making_charge_rules')
    if baseline_rules:
        meta['makingChargeRules'] = baseline_rules
    meta['baselineBreakdown'] = baseline
    line.pricing_meta = meta
    line.save(update_fields=['breakdown', 'unit_price', 'line_total', 'pricing_meta', 'system_updated_at'])
    return True


def _recompute_quote_totals(quote: CatalogueQuote) -> None:
    subtotal = Decimal('0')
    gst_total = Decimal('0')
    for ln in quote.lines.filter(is_removed=False):
        bd = ln.breakdown if isinstance(ln.breakdown, dict) else {}
        qty = Decimal(max(1, int(ln.quantity or 1)))
        line_sub = _d(bd.get('subtotal')) * qty
        line_gst = _d(bd.get('gstAmount') or bd.get('gst_amount')) * qty
        subtotal += line_sub
        gst_total += line_gst
    quote.subtotal = subtotal.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    quote.gst_total = gst_total.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    quote.grand_total = (quote.subtotal + quote.gst_total).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def revert_expired_pricing(quote: CatalogueQuote) -> bool:
    """If pricing hold elapsed, restore baseline prices. Returns True if changes applied."""
    if quote.status not in (CatalogueQuote.STATUS_DRAFT,):
        return False
    if not quote.pricing_expires_at:
        return False
    if timezone.now() <= quote.pricing_expires_at:
        return False

    changed = False
    with transaction.atomic():
        for ln in quote.lines.filter(is_removed=False).select_for_update():
            if _revert_line_to_baseline(ln):
                changed = True
        if quote.cart_pricing_meta:
            quote.cart_pricing_meta = {}
            changed = True
        if changed:
            _recompute_quote_totals(quote)
            quote.pricing_expires_at = None
            quote.version = (quote.version or 1) + 1
            quote.save(
                update_fields=[
                    'cart_pricing_meta',
                    'pricing_expires_at',
                    'subtotal',
                    'gst_total',
                    'grand_total',
                    'version',
                    'system_updated_at',
                ]
            )
        else:
            # Timer elapsed but nothing to revert — clear timer
            quote.pricing_expires_at = None
            quote.save(update_fields=['pricing_expires_at', 'system_updated_at'])
            changed = True
    return changed


def expire_quote_stock_hold(quote: CatalogueQuote) -> bool:
    """After valid_until (end of IST calendar day), mark draft expired so stock is released. Keeps the quote."""
    if quote.status != CatalogueQuote.STATUS_DRAFT:
        return False
    if not quote.valid_until or timezone.now() <= quote.valid_until:
        return False
    quote.status = CatalogueQuote.STATUS_EXPIRED
    quote.save(update_fields=['status', 'system_updated_at'])
    close_visit_for_quote(quote)
    return True


def process_quote_timers(quote: CatalogueQuote) -> dict[str, bool]:
    """Run pricing revert + stock expiry for one quote. Safe to call on every GET/PATCH."""
    pricing_reverted = revert_expired_pricing(quote)
    quote.refresh_from_db()
    stock_released = expire_quote_stock_hold(quote)
    if stock_released:
        quote.refresh_from_db()
    return {'pricing_reverted': pricing_reverted, 'stock_released': stock_released}


def process_all_due_quote_timers(*, limit: int = 500) -> dict[str, int]:
    now = timezone.now()
    pricing_count = 0
    expired_count = 0

    pricing_qs = CatalogueQuote.objects.filter(
        status=CatalogueQuote.STATUS_DRAFT,
        pricing_expires_at__isnull=False,
        pricing_expires_at__lte=now,
    ).order_by('id')[:limit]
    for quote in pricing_qs:
        if revert_expired_pricing(quote):
            pricing_count += 1

    expired_qs = CatalogueQuote.objects.filter(
        status=CatalogueQuote.STATUS_DRAFT,
        valid_until__lt=now,
    ).order_by('id')[:limit]
    for quote in expired_qs:
        if expire_quote_stock_hold(quote):
            expired_count += 1

    return {'pricing_reverted': pricing_count, 'stock_released': expired_count}


def pricing_expires_payload(quote: CatalogueQuote) -> dict[str, Any]:
    pe = quote.pricing_expires_at
    return {
        'pricingExpiresAt': pe.isoformat() if pe else None,
        'pricingHoldHours': PRICING_HOLD_HOURS,
    }
