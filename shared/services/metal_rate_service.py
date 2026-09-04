"""
Metal rate service: daily rates per metal (24K base priority).
Uses only MetalBranchRate (when branch_id) and MetalMasterRate. Legacy MetalRate table is no longer used.

This module is the single source of truth for:
- Looking up metal rates for a given date / purity / branch
- Updating base rates and deriving other purities from rules

Gold locking: always 24K Gold; no metal_id or purity stored on scheme.
Flexible date handling: if target_date is provided but no record exists, returns nearest previous record.
If no date is provided, returns the latest available record.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from shared.models import Metal, MetalMasterRule, MetalMasterRate, MetalBranchRate
from shared.services.metal_service import (
    get_base_purity,
    get_purity_rule,
    get_rules_for_metal,
    calculate_derived_rate,
)


def _normalize_purity_name(purity_name):
    """Store empty label as None for consistent unique keys."""
    if purity_name is None:
        return None
    s = (purity_name or "").strip()
    return s if s else None


def _master_rates_for_purity(metal_id, effective_date, purity_name):
    """Queryset for master rows matching metal, date, and purity (handles NULL/blank)."""
    pn = _normalize_purity_name(purity_name)
    qs = MetalMasterRate.objects.filter(metal_id=metal_id, effective_date=effective_date)
    if pn is None:
        return qs.filter(Q(purity_name__isnull=True) | Q(purity_name=""))
    return qs.filter(purity_name=pn)


def _upsert_metal_master_rate(metal_id, effective_date, purity_name, sell_price, buyback_price):
    """
    If a row exists for (metal, date, purity), update prices; else create.
    Returns (instance, created).
    """
    pn = _normalize_purity_name(purity_name)
    row = _master_rates_for_purity(metal_id, effective_date, purity_name).order_by("id").first()
    if row:
        row.sell_price = sell_price
        row.buyback_price = buyback_price
        row.is_active = True
        row.save(update_fields=["sell_price", "buyback_price", "is_active", "system_updated_at"])
        return row, False
    obj = MetalMasterRate.objects.create(
        metal_id=metal_id,
        effective_date=effective_date,
        purity_name=pn,
        sell_price=sell_price,
        buyback_price=buyback_price,
        is_active=True,
    )
    return obj, True


def _branch_rates_for_purity(branch_id, metal_id, effective_date, purity_name):
    pn = _normalize_purity_name(purity_name)
    qs = MetalBranchRate.objects.filter(
        branch_id=branch_id,
        metal_id=metal_id,
        effective_date=effective_date,
    )
    if pn is None:
        return qs.filter(Q(purity_name__isnull=True) | Q(purity_name=""))
    return qs.filter(purity_name=pn)


def _upsert_metal_branch_rate(branch_id, metal_id, effective_date, purity_name, sell_price, buyback_price):
    """If a branch row exists for (branch, metal, date, purity), update; else create."""
    pn = _normalize_purity_name(purity_name)
    row = _branch_rates_for_purity(branch_id, metal_id, effective_date, purity_name).order_by("id").first()
    if row:
        row.sell_price = sell_price
        row.buyback_price = buyback_price
        row.is_current = True
        row.is_active = True
        row.save(
            update_fields=[
                "sell_price",
                "buyback_price",
                "is_current",
                "is_active",
                "system_updated_at",
            ]
        )
        return row, False
    obj = MetalBranchRate.objects.create(
        branch_id=branch_id,
        metal_id=metal_id,
        effective_date=effective_date,
        purity_name=pn,
        sell_price=sell_price,
        buyback_price=buyback_price,
        is_current=True,
        is_active=True,
    )
    return obj, True


def get_default_gold_metal_id():
    """
    Return the Metal id for Gold. Returns None if no metal with name 'Gold' exists.
    """
    m = Metal.objects.filter(metal_name__iexact="Gold").first()
    return m.id if m else None


def get_default_silver_metal_id():
    """
    Return the Metal id for Silver (exact name first, then contains 'silver').
    """
    m = Metal.objects.filter(metal_name__iexact="Silver").first()
    if m:
        return m.id
    m = Metal.objects.filter(metal_name__icontains="silver").first()
    return m.id if m else None


def _get_rate_row(metal_id, target_date, purity_name, branch_id=None):
    """
    Get rate row for metal/date/purity. Branch override → MetalMasterRate only (no legacy table).
    Returns tuple: (row_or_none, actual_date_used_or_none).
    """
    actual_date = None
    
    if branch_id:
        br = MetalBranchRate.objects.filter(
            branch_id=branch_id,
            metal_id=metal_id,
            effective_date=target_date,
            is_active=True,
            is_current=True,
        ).order_by("-system_created_at")
        if purity_name:
            br = br.filter(purity_name=purity_name)
        row = br.first()
        if not row and purity_name:
            br = MetalBranchRate.objects.filter(
                branch_id=branch_id, metal_id=metal_id,
                effective_date__lte=target_date, is_active=True, is_current=True,
            ).order_by("-effective_date").first()
            if br and not purity_name:
                row = br
        if row:
            actual_date = row.effective_date
            return row, actual_date
    # Master rate (per purity) — only table used when no branch
    pn = _normalize_purity_name(purity_name)
    mm = MetalMasterRate.objects.filter(
        metal_id=metal_id,
        effective_date=target_date,
        is_active=True,
    )
    if pn is None:
        mm = mm.filter(Q(purity_name__isnull=True) | Q(purity_name=""))
    else:
        mm = mm.filter(purity_name=pn)
    row = mm.order_by("-system_created_at").first()
    if not row:
        fallback_qs = MetalMasterRate.objects.filter(
            metal_id=metal_id,
            effective_date__lte=target_date,
            is_active=True,
        )
        if pn is None:
            fallback_qs = fallback_qs.filter(Q(purity_name__isnull=True) | Q(purity_name=""))
        else:
            fallback_qs = fallback_qs.filter(purity_name=pn)
        row = fallback_qs.order_by("-effective_date", "-system_created_at").first()
    if row:
        actual_date = row.effective_date
    return row, actual_date


def get_today_metal_rate(metal_id, purity_name="24K", branch_id=None, return_date_info=False):
    """
    Get today's metal rate for the given metal and purity (24K = base priority).
    If no today record exists, falls back to the nearest previous available record.
    If branch_id: branch rate first, else master rate only.
    
    Args:
        return_date_info: If True, returns tuple (rate_object, actual_date_used).
                         If False (default), returns just the rate_object for backward compatibility.
    """
    rate, actual_date = get_metal_rate_by_date(metal_id, timezone.localdate(), purity_name, branch_id=branch_id)
    if return_date_info:
        return rate, actual_date
    return rate


def get_metal_rate_by_date(metal_id, target_date, purity_name="24K", branch_id=None, return_date_info=False):
    """
    Get metal rate for a specific date and purity.
    Branch override → MetalMasterRate only. Purity rule: branch → master.
    
    If target_date is None, finds the latest available date.
    
    Args:
        return_date_info: If True, returns tuple (rate_object, actual_date_used).
                         If False (default), returns just the rate_object for backward compatibility.
    """
    if not metal_id:
        return (None, None) if return_date_info else None
    
    # If no target_date provided, find the latest available date
    if target_date is None:
        # Get the latest available date for this metal/branch
        if branch_id:
            latest_branch = MetalBranchRate.objects.filter(
                branch_id=branch_id,
                metal_id=metal_id,
                is_active=True,
                is_current=True,
            ).order_by("-effective_date").first()
            if latest_branch:
                target_date = latest_branch.effective_date
        if target_date is None:
            latest_master = MetalMasterRate.objects.filter(
                metal_id=metal_id,
                is_active=True,
            ).order_by("-effective_date").first()
            if latest_master:
                target_date = latest_master.effective_date
        if target_date is None:
            return (None, None) if return_date_info else None
    
    try:
        base_rule = get_base_purity(metal_id, branch_id=branch_id)
    except (MetalMasterRule.DoesNotExist, Exception):
        base_rule = None
    if not base_rule:
        return (None, None) if return_date_info else None
    metal_rate, actual_date = _get_rate_row(metal_id, target_date, purity_name, branch_id=branch_id)
    if not metal_rate:
        return (None, None) if return_date_info else None
    # If we already resolved an explicit row for the requested purity, use it as-is.
    # Do not derive again from base purity.
    req_pn = (_normalize_purity_name(purity_name) or "").upper()
    row_pn = (_normalize_purity_name(getattr(metal_rate, "purity_name", None)) or "").upper()
    if req_pn and row_pn and req_pn == row_pn:
        return (metal_rate, actual_date) if return_date_info else metal_rate
    base_rate_value = getattr(metal_rate, "sell_price", None) or getattr(metal_rate, "rate_value", metal_rate)
    if base_rate_value is None:
        return (None, None) if return_date_info else None
    pn = (purity_name or "").strip().upper()
    base_pn = (getattr(base_rule, "purity_name", None) or "").strip().upper()
    if pn in ("24K", "") or (base_pn and pn == base_pn):
        return (metal_rate, actual_date) if return_date_info else metal_rate
    target_rule = get_purity_rule(metal_id, purity_name=purity_name, branch_id=branch_id)
    if not target_rule or not base_rule.purity_percentage or not getattr(target_rule, "purity_percentage", None):
        return (metal_rate, actual_date) if return_date_info else metal_rate
    derived = calculate_derived_rate(
        base_rate_value,
        base_rule.purity_percentage,
        target_rule.purity_percentage,
    )
    class RateValue:
        rate_value = None
    r = RateValue()
    r.rate_value = Decimal(str(derived))
    return (r, actual_date) if return_date_info else r


# Customer portal silver board: headline purity and display unit (DB stores ₹/g).
SILVER_CUSTOMER_SPOT_PURITY = "Silver-100"
SILVER_CUSTOMER_DISPLAY_GRAMS = 10


def get_metal_master_rate_simple(metal_id, target_date=None, return_date_info=False):
    """
    Silver spot for customer portal — Silver-100 only (today, else nearest previous row).
    DB stores ₹/g; customer API multiplies to ₹/10g in the view layer.
    """
    if not metal_id:
        return (None, None) if return_date_info else None
    rate_date = target_date if target_date else timezone.localdate()
    row, actual_date = _get_rate_row(
        metal_id, rate_date, SILVER_CUSTOMER_SPOT_PURITY, branch_id=None
    )
    if not row:
        row = (
            MetalMasterRate.objects.filter(
                metal_id=metal_id,
                is_active=True,
                purity_name__iexact=SILVER_CUSTOMER_SPOT_PURITY,
            )
            .order_by("-effective_date", "-system_updated_at")
            .first()
        )
        actual_date = row.effective_date if row else None
    if return_date_info:
        return row, actual_date
    return row


def get_silver_spot_rate(metal_id, target_date=None, return_date_info=False):
    """Alias for get_metal_master_rate_simple (Silver-100 headline rate)."""
    return get_metal_master_rate_simple(metal_id, target_date, return_date_info=return_date_info)


def get_24k_gold_rate_for_lock(return_date_info=False, target_date=None):
    """
    Single rate for all gold locking. No scheme metal_id or purity used.
    Returns the 24K Gold rate for the given target_date if available,
    else the nearest previous available record.
    
    Args:
        return_date_info: If True, returns tuple (rate_object, actual_date_used).
                         If False (default), returns just the rate_object for backward compatibility.
        target_date: If provided, get rate for that specific date (for back-dated payments).
                     If not provided, uses today's date.
    """
    metal_id = get_default_gold_metal_id()
    if not metal_id:
        return (None, None) if return_date_info else None
    
    # Use target_date if provided, otherwise use today's date
    rate_date = target_date if target_date else timezone.localdate()
    
    # Use the updated function which handles fallback automatically
    rate, actual_date = get_metal_rate_by_date(metal_id, rate_date, "24K", return_date_info=True)
    if return_date_info:
        return rate, actual_date
    return rate


def get_lock_rate_for_scheme(scheme=None, return_date_info=False, target_date=None):
    """
    Get the rate to use for locking (payment/maturity). Scheme is not used;
    always 24K Gold with flexible date handling (today else nearest previous).
    
    Args:
        return_date_info: If True, returns tuple (rate_object, actual_date_used).
                         If False (default), returns just the rate_object.
        target_date: If provided, get rate for that specific date (for back-dated payments).
                     If not provided, uses today's date.
    """
    return get_24k_gold_rate_for_lock(return_date_info=return_date_info, target_date=target_date)


def calculate_and_update_metal_rates(
    metal_id,
    base_sell_price,
    effective_date=None,
    base_buyback_price=None,
    branch_id=None,
):
    """
    Centralised entry point for master/branch metal rates from the base purity rule.

    For the given effective_date:
    - If a row already exists for (metal, date, purity) [and branch when applicable],
      sell/buyback amounts are updated.
    - Otherwise a new row is created.
    Derived purities for the same date are updated or created the same way.

    Behaviour:
    - Master (branch_id is None):
        * Upserts MetalMasterRate for base purity, then upserts derived rows from rules.

    - Branch (branch_id is not None):
        * Upserts MetalBranchRate for base + derived purities at that branch.

    Returns:
        (obj, created) for the *base* purity row: created=True if that row was inserted.
    """
    if not metal_id:
        raise ValueError("metal_id is required")

    if effective_date is None:
        effective_date = timezone.localdate()

    if base_buyback_price is None:
        base_buyback_price = base_sell_price

    # Resolve base purity rule for master or branch context
    try:
        base_rule = get_base_purity(metal_id, branch_id=branch_id)
    except (MetalMasterRule.DoesNotExist, Exception):
        base_rule = None

    base_purity_name = (getattr(base_rule, "purity_name", None) or "24K") if base_rule else "24K"

    with transaction.atomic():
        if branch_id:
            obj, created = _upsert_metal_branch_rate(
                branch_id,
                metal_id,
                effective_date,
                base_purity_name,
                base_sell_price,
                base_buyback_price,
            )
            if base_rule and getattr(base_rule, "purity_percentage", None):
                try:
                    rules = get_rules_for_metal(metal_id, branch_id=branch_id)
                except Exception:
                    rules = []
                base_pct = base_rule.purity_percentage
                for rule in rules:
                    if rule.id == base_rule.id:
                        continue
                    target_pct = getattr(rule, "purity_percentage", None)
                    if not target_pct:
                        continue
                    try:
                        derived_sell = calculate_derived_rate(
                            base_sell_price,
                            base_pct,
                            target_pct,
                        )
                        derived_buy = calculate_derived_rate(
                            base_buyback_price,
                            base_pct,
                            target_pct,
                        )
                    except Exception:
                        continue
                    _upsert_metal_branch_rate(
                        branch_id,
                        metal_id,
                        effective_date,
                        rule.purity_name,
                        derived_sell,
                        derived_buy,
                    )
            return obj, created

        obj, created = _upsert_metal_master_rate(
            metal_id,
            effective_date,
            base_purity_name,
            base_sell_price,
            base_buyback_price,
        )

        if base_rule and getattr(base_rule, "purity_percentage", None):
            try:
                rules = get_rules_for_metal(metal_id, branch_id=None)
            except Exception:
                rules = []

            base_pct = base_rule.purity_percentage

            for rule in rules:
                if rule.id == base_rule.id:
                    continue
                target_pct = getattr(rule, "purity_percentage", None)
                if not target_pct:
                    continue

                try:
                    derived_sell = calculate_derived_rate(
                        base_sell_price,
                        base_pct,
                        target_pct,
                    )
                    derived_buy = calculate_derived_rate(
                        base_buyback_price,
                        base_pct,
                        target_pct,
                    )
                except Exception:
                    continue

                _upsert_metal_master_rate(
                    metal_id,
                    effective_date,
                    rule.purity_name,
                    derived_sell,
                    derived_buy,
                )

        return obj, created


def upsert_branch_rates_manual(metal_id, branch_id, effective_date, rates_payload):
    """
    Persist branch metal rates exactly as sent (Store "Edit branch wise" grid).
    No formula derivation — each purity_rule_id maps to its sell/buy on effective_date.

    rates_payload: list of dicts with purity_rule_id, sell_price_per_gm, buy_price_per_gm (optional).

    Returns:
        (base_row_or_first_saved, created_for_that_row) for API response compatibility.
    """
    if not metal_id or not branch_id:
        raise ValueError("metal_id and branch_id are required")
    if effective_date is None:
        effective_date = timezone.localdate()
    if not rates_payload:
        raise ValueError("rates list is empty")

    rules = get_rules_for_metal(metal_id, branch_id=branch_id)
    rule_by_id = {r.id: r for r in rules}
    base_rule = next((r for r in rules if getattr(r, "is_base", False)), None)

    response_obj = None
    response_created = False
    any_saved = False

    with transaction.atomic():
        for item in rates_payload:
            rid = item.get("purity_rule_id")
            if rid is None:
                continue
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            rule = rule_by_id.get(rid)
            if not rule:
                continue
            sell = item.get("sell_price_per_gm")
            if sell is None:
                continue
            buy = item.get("buy_price_per_gm")
            try:
                sell_d = Decimal(str(sell))
                buy_d = Decimal(str(buy)) if buy is not None else sell_d
            except Exception:
                raise ValueError("Invalid numeric values for sell_price_per_gm / buy_price_per_gm")
            if buy_d > sell_d:
                raise ValueError("buy_price_per_gm must be less than or equal to sell_price_per_gm")

            obj, created = _upsert_metal_branch_rate(
                branch_id,
                metal_id,
                effective_date,
                rule.purity_name,
                sell_d,
                buy_d,
            )
            any_saved = True
            if base_rule and rid == base_rule.id:
                response_obj = obj
                response_created = created
            elif response_obj is None:
                response_obj = obj
                response_created = created

    if not any_saved:
        raise ValueError("No matching purity rules or valid rates to save")

    return response_obj, response_created
