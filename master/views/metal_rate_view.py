"""
Metal rate API: master rates (MetalMasterRate) for Masters section; branch rates (MetalBranchRate) for Stores only.
Legacy MetalRate table is no longer used.
"""
from datetime import datetime
from decimal import Decimal

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone

from master.permissions.permission_checker import admin_auth, admin_has_any_permission
from master.permissions.metal_read_auth import (
    MASTER_METAL_WRITE_AUTH,
    METAL_BRANCH_RATE_WRITE_AUTH,
    METAL_LIST_READ_AUTH,
)
from shared.models import Metal, MetalMasterRule, MetalMasterRate, MetalBranchRate, Branch
from shared.services.metal_service import get_base_purity, get_rules_for_metal, calculate_derived_rate
from shared.services.metal_rate_service import (
    _get_rate_row,
    calculate_and_update_metal_rates,
    upsert_branch_rates_manual,
)


@api_view(["GET"])
@admin_auth(*METAL_LIST_READ_AUTH)
def metal_rate_list(request):
    """
    List master metal rates (MetalMasterRate).
    Optional: metal_id, rate_date/effective_date (optional, defaults to latest available), purity_name (default 24K).
    
    Flexible date handling:
    - If date is provided and exists: returns that date's rates
    - If date is provided but not exists: returns nearest previous available date's rates
    - If no date is provided: returns latest available date's rates
    """
    queryset = MetalMasterRate.objects.select_related("metal").filter(is_active=True).order_by("-effective_date", "metal_id")
    metal_id = request.GET.get("metal_id")
    if metal_id is not None:
        try:
            queryset = queryset.filter(metal_id=int(metal_id))
        except ValueError:
            pass
    rate_date = request.GET.get("rate_date") or request.GET.get("effective_date")
    # Use flexible date parsing - allow None to get latest
    target_date = _parse_date_param(rate_date, allow_none=True)
    
    # Determine actual_date with flexible handling
    actual_date = None
    if target_date:
        # Check if we have data for the target date
        has_date = MetalMasterRate.objects.filter(
            metal_id=metal_id if metal_id else None, effective_date=target_date, is_active=True
        ).exists()
        if has_date:
            actual_date = target_date
        else:
            latest = MetalMasterRate.objects.filter(
                effective_date__lte=target_date, is_active=True
            )
            if metal_id:
                latest = latest.filter(metal_id=metal_id)
            latest = latest.order_by("-effective_date").first()
            if latest:
                actual_date = latest.effective_date
    else:
        # No date provided - get latest available
        latest = MetalMasterRate.objects.filter(is_active=True)
        if metal_id:
            latest = latest.filter(metal_id=metal_id)
        latest = latest.order_by("-effective_date").first()
        if latest:
            actual_date = latest.effective_date
    
    if actual_date:
        queryset = queryset.filter(effective_date=actual_date)
    purity = request.GET.get("purity_name") or request.GET.get("purity")
    if purity:
        queryset = queryset.filter(purity_name=purity)
    elif not (metal_id and actual_date):
        # When listing without metal+date, return only base purities to keep payload small
        queryset = queryset.filter(purity_name__in=["24K", "", None])

    data = [
        {
            "id": mr.id,
            "metal_id": mr.metal_id,
            "metal_name": mr.metal.metal_name if mr.metal else None,
            "branch_name": None,
            "purity_name": mr.purity_name or "24K",
            "effective_date": str(mr.effective_date),
            "rate_date": str(mr.effective_date),
            "sell_price": str(mr.sell_price),
            "buyback_price": str(mr.buyback_price),
            "rate_value": str(mr.sell_price),
            "is_active": mr.is_active,
            "is_locked": not mr.is_active,
            "system_created_at": mr.system_created_at,
            "actual_date": str(actual_date) if actual_date else None,
        }
        for mr in queryset
    ]
    return Response({
        "data": data,
        "results": data,
        "actual_date": str(actual_date) if actual_date else None,
        "requested_date": str(target_date) if target_date else None,
        "is_fallback": target_date is not None and actual_date != target_date if (target_date and actual_date) else False,
    })


@api_view(["PUT"])
@admin_auth(*MASTER_METAL_WRITE_AUTH)
def update_metal_rate(request, pk):
    """
    Update master metal rate by id. Body: sell_price, buyback_price (optional).
    """
    try:
        metal_rate = MetalMasterRate.objects.select_related("metal").get(id=pk)
    except MetalMasterRate.DoesNotExist:
        return Response({"error": "Metal rate not found"}, status=status.HTTP_404_NOT_FOUND)

    sell_price = request.data.get("sell_price")
    buyback_price = request.data.get("buyback_price")
    if sell_price is None and buyback_price is None:
        sell_price = request.data.get("rate_value")
    if sell_price is None:
        return Response(
            {"error": "sell_price or rate_value is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Enforce: buyback (buy) must be <= sell.
    try:
        new_sell = Decimal(str(sell_price)) if sell_price is not None else metal_rate.sell_price
        new_buy = (
            Decimal(str(buyback_price))
            if buyback_price is not None
            else (metal_rate.buyback_price or new_sell)
        )
    except Exception:
        return Response({"error": "Invalid numeric values for sell_price/buyback_price"}, status=status.HTTP_400_BAD_REQUEST)
    if new_buy is not None and new_sell is not None and new_buy > new_sell:
        return Response(
            {"error": "buyback_price (buy) must be less than or equal to sell_price"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        metal_rate.sell_price = new_sell
        # If explicit buyback provided, use validated value; otherwise keep existing or align to sell.
        if buyback_price is not None or metal_rate.buyback_price is None:
            metal_rate.buyback_price = new_buy
        metal_rate.save(update_fields=["sell_price", "buyback_price", "system_updated_at"])
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        "message": "Metal rate updated successfully",
        "data": {
            "id": metal_rate.id,
            "metal_id": metal_rate.metal_id,
            "effective_date": str(metal_rate.effective_date),
            "sell_price": str(metal_rate.sell_price),
            "buyback_price": str(metal_rate.buyback_price),
        },
    })


@api_view(["POST"])
@admin_auth(*MASTER_METAL_WRITE_AUTH)
def create_or_update_metal_rate(request):
    """
    Create or update master metal rate (MetalMasterRate) for a metal and date.
    Body: metal_id, effective_date (YYYY-MM-DD), sell_price, buyback_price (optional), purity_name (default 24K).
    rate_date/rate_value accepted as aliases for effective_date/sell_price.
    """
    metal_id = request.data.get("metal_id")
    effective_date = request.data.get("effective_date") or request.data.get("rate_date")
    sell_price = request.data.get("sell_price") or request.data.get("rate_value")
    buyback_price = request.data.get("buyback_price")
    purity_name = request.data.get("purity_name") or "24K"

    if not metal_id or effective_date is None or sell_price is None:
        return Response(
            {"error": "metal_id, effective_date (or rate_date) and sell_price (or rate_value) are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return Response({"error": "Metal not found"}, status=status.HTTP_404_NOT_FOUND)
    if isinstance(effective_date, str):
        effective_date = datetime.strptime(effective_date[:10], "%Y-%m-%d").date()
    elif hasattr(effective_date, "date"):
        effective_date = effective_date.date()

    # Normalise numeric values and enforce buyback <= sell.
    try:
        sell_decimal = Decimal(str(sell_price))
        buy_decimal = Decimal(str(buyback_price)) if buyback_price is not None else sell_decimal
    except Exception:
        return Response({"error": "Invalid numeric values for sell_price/buyback_price"}, status=status.HTTP_400_BAD_REQUEST)
    if buy_decimal > sell_decimal:
        return Response(
            {"error": "buyback_price (buy) must be less than or equal to sell_price"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Delegate to centralised service: updates base rate and derives other purities.
    obj, created = calculate_and_update_metal_rates(
        metal_id=metal_id,
        base_sell_price=sell_decimal,
        effective_date=effective_date,
        base_buyback_price=buy_decimal,
        branch_id=None,
    )
    payload = {
        "message": "Metal rate created" if created else "Metal rate updated",
        "data": {
            "id": obj.id,
            "metal_id": obj.metal_id,
            "effective_date": str(obj.effective_date),
            "rate_date": str(obj.effective_date),
            "sell_price": str(obj.sell_price),
            "buyback_price": str(obj.buyback_price),
            "rate_value": str(obj.sell_price),
            "purity_name": obj.purity_name or "24K",
        },
    }
    return Response(payload, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def _parse_date_param(value, allow_none=True):
    """
    Parse date from query/body (YYYY-MM-DD or date object).
    
    Args:
        value: The date value to parse (can be string, date, or None)
        allow_none: If True (default), returns None when value is None.
                   If False, returns today's date as fallback.
    
    Returns:
        Parsed date object or None.
    """
    if value is None:
        if allow_none:
            return None
        return timezone.localdate()
    if isinstance(value, str):
        if not value.strip():
            return None if allow_none else timezone.localdate()
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    if hasattr(value, "date"):
        return value.date()
    return value


def _purity_key(name):
    """Normalize purity for grouping (e.g. 24K, '', None -> '24K')."""
    return (name or "24K").strip().upper() or "24K"


@api_view(["GET"])
@admin_auth(*METAL_LIST_READ_AUTH)
def store_metal_rates(request):
    """
    Store Metal Rate: returns master rate + branch override merged by purity.
    Query: branch_id (required), metal_id (required), date (optional, defaults to latest available).
    
    Flexible date handling:
    - If date is provided and exists: returns that date's rates
    - If date is provided but not exists: returns nearest previous available date's rates
    - If no date is provided: returns latest available date's rates
    
    Response: branch_id, branch_name, metal_id, metal_name, rates[] with master_rate,
    branch_rate, effective_rate (branch if exists else master), difference, is_override,
    actual_date (the date actually used).
    """
    branch_id = request.GET.get("branch_id")
    metal_id = request.GET.get("metal_id")
    if not branch_id or not metal_id:
        return Response(
            {"error": "branch_id and metal_id are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        branch_id = int(branch_id)
        metal_id = int(metal_id)
    except (TypeError, ValueError):
        return Response({"error": "branch_id and metal_id must be integers"}, status=status.HTTP_400_BAD_REQUEST)

    # Parse date - allow None to get latest available
    target_date = _parse_date_param(request.GET.get("date"), allow_none=True)

    try:
        metal = Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return Response({"error": "Metal not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        branch = Branch.objects.get(id=branch_id)
    except Branch.DoesNotExist:
        return Response({"error": "Branch not found"}, status=status.HTTP_404_NOT_FOUND)

    # Determine the actual date to use by querying for available dates
    actual_date = None
    if target_date:
        # Check if we have data for the target date
        has_master = MetalMasterRate.objects.filter(
            metal_id=metal_id, effective_date=target_date, is_active=True
        ).exists()
        has_branch = MetalBranchRate.objects.filter(
            branch_id=branch_id, metal_id=metal_id, effective_date=target_date,
            is_active=True, is_current=True
        ).exists()
        if has_master or has_branch:
            actual_date = target_date
        else:
            # Find nearest previous date
            latest_master = MetalMasterRate.objects.filter(
                metal_id=metal_id, effective_date__lte=target_date, is_active=True
            ).order_by("-effective_date").first()
            latest_branch = MetalBranchRate.objects.filter(
                branch_id=branch_id, metal_id=metal_id, effective_date__lte=target_date,
                is_active=True, is_current=True
            ).order_by("-effective_date").first()
            if latest_master and latest_branch:
                actual_date = max(latest_master.effective_date, latest_branch.effective_date)
            elif latest_master:
                actual_date = latest_master.effective_date
            elif latest_branch:
                actual_date = latest_branch.effective_date
    else:
        # No date provided - get latest available
        latest_master = MetalMasterRate.objects.filter(
            metal_id=metal_id, is_active=True
        ).order_by("-effective_date").first()
        latest_branch = MetalBranchRate.objects.filter(
            branch_id=branch_id, metal_id=metal_id, is_active=True, is_current=True
        ).order_by("-effective_date").first()
        if latest_master and latest_branch:
            actual_date = max(latest_master.effective_date, latest_branch.effective_date)
        elif latest_master:
            actual_date = latest_master.effective_date
        elif latest_branch:
            actual_date = latest_branch.effective_date

    if actual_date is None:
        return Response(
            {"error": f"No metal rate data available for metal_id {metal_id}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Fetch master rates for the actual_date
    master_qs = MetalMasterRate.objects.filter(
        metal_id=metal_id,
        effective_date=actual_date,
        is_active=True,
    ).order_by("purity_name")

    # Fetch branch rates for this branch/metal/actual_date
    branch_qs = MetalBranchRate.objects.filter(
        branch_id=branch_id,
        metal_id=metal_id,
        effective_date=actual_date,
        is_active=True,
        is_current=True,
    ).order_by("purity_name")

    master_by_purity = {}
    purity_display_from_master = {}
    for r in master_qs:
        k = _purity_key(r.purity_name)
        master_by_purity[k] = {
            "sell_price": int(r.sell_price) if r.sell_price is not None else None,
            "buy_price": int(r.buyback_price) if r.buyback_price is not None else None,
            "effective_date": str(r.effective_date),
        }
        purity_display_from_master[k] = r.purity_name or k

    branch_by_purity = {}
    purity_display_from_branch = {}
    for r in branch_qs:
        k = _purity_key(r.purity_name)
        branch_by_purity[k] = {
            "sell_price": int(r.sell_price) if r.sell_price is not None else None,
            "buy_price": int(r.buyback_price) if r.buyback_price is not None else None,
            "effective_date": str(r.effective_date),
        }
        purity_display_from_branch[k] = r.purity_name or k

    # Union of purities; display name: prefer branch then master
    all_purities = set(master_by_purity.keys()) | set(branch_by_purity.keys())
    purity_display = {
        k: purity_display_from_branch.get(k) or purity_display_from_master.get(k) or k
        for k in all_purities
    }

    # Canonical base purity key for this metal+branch (matches Metal Rules / save logic).
    base_purity_key = "24K"
    try:
        brule = get_base_purity(metal_id, branch_id=branch_id)
        base_purity_key = _purity_key(brule.purity_name)
    except Exception:
        pass

    rates = []
    for k in sorted(all_purities):
        master_rate = master_by_purity.get(k)
        branch_rate = branch_by_purity.get(k)
        is_override = branch_rate is not None

        if branch_rate:
            eff_sell = branch_rate["sell_price"]
            eff_buy = branch_rate["buy_price"]
        else:
            eff_sell = master_rate["sell_price"] if master_rate else None
            eff_buy = master_rate["buy_price"] if master_rate else None

        master_sell = (master_rate or {}).get("sell_price")
        master_buy = (master_rate or {}).get("buy_price")
        sell_diff = int(eff_sell - master_sell) if (eff_sell is not None and master_sell is not None) else 0
        buy_diff = int(eff_buy - master_buy) if (eff_buy is not None and master_buy is not None) else 0

        rates.append({
            "purity_name": purity_display.get(k, k),
            "master_rate": master_rate or None,
            "branch_rate": branch_rate if is_override else None,
            "effective_rate": {"sell_price": eff_sell, "buy_price": eff_buy},
            "difference": {"sell_diff": sell_diff, "buy_diff": buy_diff},
            "is_override": is_override,
            "is_base_purity": k == base_purity_key,
        })

    payload = {
        "branch_id": branch_id,
        "branch_name": branch.name or "",
        "metal_id": metal_id,
        "metal_name": metal.metal_name or "",
        "rates": rates,
        "actual_date": str(actual_date) if actual_date else None,
        "requested_date": str(target_date) if target_date else None,
        "is_fallback": target_date is not None and actual_date != target_date if (target_date and actual_date) else False,
    }
    return Response(payload)


@api_view(["GET"])
@admin_auth(*METAL_LIST_READ_AUTH)
def branch_rates_list(request):
    """
    List branch-wise rates for a metal.
    Query: metal_id (required), date (optional, defaults to latest available).
    
    Flexible date handling:
    - If date is provided and exists: returns that date's rates
    - If date is provided but not exists: returns nearest previous available date's rates
    - If no date is provided: returns latest available date's rates
    
    Branch = Metal (branch_id = metal_id). Returns from response.data.results or response.data.
    """
    metal_id = request.GET.get("metal_id")
    if not metal_id:
        return Response(
            {"error": "metal_id is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        metal_id = int(metal_id)
    except (TypeError, ValueError):
        return Response({"error": "Invalid metal_id"}, status=status.HTTP_400_BAD_REQUEST)
    target_date = _parse_date_param(request.GET.get("date"), allow_none=True)
    branch_id = request.GET.get("branch_id")
    try:
        branch_id = int(branch_id) if branch_id else None
    except (TypeError, ValueError):
        branch_id = None
    try:
        metal = Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return Response({"error": "Metal not found"}, status=status.HTTP_404_NOT_FOUND)
    base_rule = None
    try:
        base_rule = get_base_purity(metal_id, branch_id=branch_id)
    except MetalMasterRule.DoesNotExist:
        pass
    
    # Determine the actual date to use - flexible date handling
    actual_date = None
    if target_date:
        # Check if we have data for the target date
        if branch_id:
            has_branch = MetalBranchRate.objects.filter(
                branch_id=branch_id, metal_id=metal_id, effective_date=target_date,
                is_active=True, is_current=True
            ).exists()
            if has_branch:
                actual_date = target_date
            else:
                latest = MetalBranchRate.objects.filter(
                    branch_id=branch_id, metal_id=metal_id, effective_date__lte=target_date,
                    is_active=True, is_current=True
                ).order_by("-effective_date").first()
                if latest:
                    actual_date = latest.effective_date
        if actual_date is None:
            has_master = MetalMasterRate.objects.filter(
                metal_id=metal_id, effective_date=target_date, is_active=True
            ).exists()
            if has_master:
                actual_date = target_date
            else:
                latest = MetalMasterRate.objects.filter(
                    metal_id=metal_id, effective_date__lte=target_date, is_active=True
                ).order_by("-effective_date").first()
                if latest:
                    actual_date = latest.effective_date
    else:
        # No date provided - get latest available
        if branch_id:
            latest = MetalBranchRate.objects.filter(
                branch_id=branch_id, metal_id=metal_id, is_active=True, is_current=True
            ).order_by("-effective_date").first()
            if latest:
                actual_date = latest.effective_date
        if actual_date is None:
            latest = MetalMasterRate.objects.filter(
                metal_id=metal_id, is_active=True
            ).order_by("-effective_date").first()
            if latest:
                actual_date = latest.effective_date
    
    if actual_date is None:
        return Response(
            {"error": f"No metal rate data available for metal_id {metal_id}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    if branch_id:
        rates = list(MetalBranchRate.objects.filter(
            branch_id=branch_id, metal_id=metal_id,
            effective_date=actual_date, is_active=True, is_current=True,
        ).select_related("metal", "branch"))
        if not rates:
            mr, _ = _get_rate_row(metal_id, actual_date, None, branch_id=branch_id)
            rates = [mr] if mr else []
    else:
        rates = list(MetalMasterRate.objects.filter(
            metal_id=metal_id, effective_date=actual_date, is_active=True,
        ).select_related("metal"))
    results = []
    for mr in rates:
        branch_name = getattr(mr, "branch", None) and getattr(mr.branch, "name", None) or ""
        bid = getattr(mr, "branch_id", None) or mr.metal_id
        results.append(
            {
                "id": mr.id,
                "branch_id": bid,
                "branch_name": branch_name,
                "metal_id": mr.metal_id,
                "metal_name": metal.metal_name,
                "base_purity_name": base_rule.purity_name or "24K" if base_rule else "24K",
                "base_sell_price_per_gm": str(mr.sell_price),
                "base_buy_back_price_per_gm": str(mr.buyback_price),
                "rate_date": str(mr.effective_date),
                "effective_date": str(mr.effective_date),
                "created_at": mr.system_created_at,
                "updated_at": mr.system_updated_at,
            }
        )

    payload = {
        "data": results,
        "results": results,
        "actual_date": str(actual_date) if actual_date else None,
        "requested_date": str(target_date) if target_date else None,
        "is_fallback": target_date is not None and actual_date != target_date if (target_date and actual_date) else False,
    }

    # Optional comparison payload when both metal_id and branch_id are provided:
    # returns master vs branch rates grouped by purity_name, without breaking
    # the existing list response shape.
    if branch_id and metal_id:
        # Master rates for this metal/date
        master_qs = MetalMasterRate.objects.filter(
            metal_id=metal_id,
            effective_date=actual_date,
            is_active=True,
        )
        # Branch rates for this branch/metal/date
        branch_qs = MetalBranchRate.objects.filter(
            branch_id=branch_id,
            metal_id=metal_id,
            effective_date=actual_date,
            is_active=True,
            is_current=True,
        )

        combined: dict[str, dict] = {}

        def _key(name):
            return (name or "").strip().upper()

        for r in master_qs:
            k = _key(r.purity_name)
            item = combined.setdefault(
                k,
                {
                    "purity_name": r.purity_name or "",
                    "master_rate": None,
                    "branch_rate": None,
                    "is_override": False,
                },
            )
            item["purity_name"] = r.purity_name or item["purity_name"]
            item["master_rate"] = {
                "sell_price": float(r.sell_price) if r.sell_price is not None else None,
                "buy_price": float(r.buyback_price) if r.buyback_price is not None else None,
                "effective_date": str(r.effective_date),
            }

        for r in branch_qs:
            k = _key(r.purity_name)
            item = combined.setdefault(
                k,
                {
                    "purity_name": r.purity_name or "",
                    "master_rate": None,
                    "branch_rate": None,
                    "is_override": False,
                },
            )
            item["purity_name"] = r.purity_name or item["purity_name"]
            item["branch_rate"] = {
                "sell_price": float(r.sell_price) if r.sell_price is not None else None,
                "buy_price": float(r.buyback_price) if r.buyback_price is not None else None,
                "effective_date": str(r.effective_date),
            }
            item["is_override"] = True

        def _sort_key(item):
            # We don't store purity_percentage on rates; order by purity_name instead.
            return item.get("purity_name") or ""

        merged_rates = sorted(combined.values(), key=_sort_key)

        payload["comparison"] = {
            "metal_id": metal.id,
            "metal_name": metal.metal_name,
            "rates": merged_rates,
        }

    return Response(payload)


@api_view(["GET", "PUT"])
@admin_auth(*METAL_LIST_READ_AUTH)
def branch_rates_detail(request, branch_id):
    """
    GET: One branch's rate detail with purity_rates (derived from base).
    PUT: Update one branch's rates. Body: metal_id, rates: [{ purity_rule_id, buy_price_per_gm, sell_price_per_gm }],
    optional date, optional manual_branch_rates (true = save grid as-is, no derivation from base).
    Query (GET): metal_id (optional), date (optional, defaults to latest available). branch_id = metal_id.
    
    Flexible date handling:
    - If date is provided and exists: returns that date's rates
    - If date is provided but not exists: returns nearest previous available date's rates
    - If no date is provided: returns latest available date's rates
    """
    if request.method == "PUT":
        if not admin_has_any_permission(request.admin_user, METAL_BRANCH_RATE_WRITE_AUTH):
            return Response({"detail": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)
        return _branch_rates_update_impl(request, branch_id)
    metal_id = branch_id
    target_date = _parse_date_param(request.GET.get("date"), allow_none=True)
    metal_id_param = request.GET.get("metal_id")
    if metal_id_param is not None:
        try:
            metal_id = int(metal_id_param)
        except (TypeError, ValueError):
            pass
    try:
        metal = Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return Response({"error": "Metal not found"}, status=status.HTTP_404_NOT_FOUND)
    branch_id_param = request.GET.get("branch_id")
    try:
        branch_id_param = int(branch_id_param) if branch_id_param else None
    except (TypeError, ValueError):
        branch_id_param = None
    
    # Determine actual_date using flexible date handling
    actual_date = None
    if target_date:
        # Check if we have data for the target date
        if branch_id_param:
            has_branch = MetalBranchRate.objects.filter(
                branch_id=branch_id_param, metal_id=metal_id, effective_date=target_date,
                is_active=True, is_current=True
            ).exists()
            if has_branch:
                actual_date = target_date
            else:
                latest = MetalBranchRate.objects.filter(
                    branch_id=branch_id_param, metal_id=metal_id, effective_date__lte=target_date,
                    is_active=True, is_current=True
                ).order_by("-effective_date").first()
                if latest:
                    actual_date = latest.effective_date
        if actual_date is None:
            has_master = MetalMasterRate.objects.filter(
                metal_id=metal_id, effective_date=target_date, is_active=True
            ).exists()
            if has_master:
                actual_date = target_date
            else:
                latest = MetalMasterRate.objects.filter(
                    metal_id=metal_id, effective_date__lte=target_date, is_active=True
                ).order_by("-effective_date").first()
                if latest:
                    actual_date = latest.effective_date
    else:
        # No date provided - get latest available
        if branch_id_param:
            latest = MetalBranchRate.objects.filter(
                branch_id=branch_id_param, metal_id=metal_id, is_active=True, is_current=True
            ).order_by("-effective_date").first()
            if latest:
                actual_date = latest.effective_date
        if actual_date is None:
            latest = MetalMasterRate.objects.filter(
                metal_id=metal_id, is_active=True
            ).order_by("-effective_date").first()
            if latest:
                actual_date = latest.effective_date
    
    if actual_date is None:
        return Response(
            {"error": f"No metal rate data available for metal_id {metal_id}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    mr, _ = _get_rate_row(metal_id, actual_date, None, branch_id=branch_id_param)
    base_rule = None
    try:
        base_rule = get_base_purity(metal_id, branch_id=branch_id_param)
    except MetalMasterRule.DoesNotExist:
        pass
    branch_name = ""
    if branch_id_param:
        try:
            b = Branch.objects.filter(id=branch_id_param).first()
            branch_name = b.name or "" if b else ""
        except Exception:
            pass
    branch_obj = {
        "branch_id": branch_id_param or metal_id,
        "branch_name": branch_name,
        "metal_id": metal_id,
        "metal_name": metal.metal_name,
        "base_purity_name": base_rule.purity_name or "24K" if base_rule else "24K",
        "base_sell_price_per_gm": str(mr.sell_price) if mr else None,
        "base_buy_back_price_per_gm": str(mr.buyback_price) if mr else None,
        "rate_date": str(mr.effective_date) if mr else str(target_date),
        "effective_date": str(mr.effective_date) if mr else str(target_date),
        "actual_date": str(actual_date) if actual_date else None,
        "requested_date": str(target_date) if target_date else None,
        "is_fallback": target_date is not None and actual_date != target_date if (target_date and actual_date) else False,
        "id": mr.id if mr else None,
        "created_at": mr.system_created_at if mr else None,
        "updated_at": mr.system_updated_at if mr else None,
    }
    purity_rates = []
    rules = get_rules_for_metal(metal_id, branch_id=branch_id_param)
    if mr and base_rule and base_rule.purity_percentage:
        for rule in rules:
            sell_p = calculate_derived_rate(mr.sell_price, base_rule.purity_percentage, rule.purity_percentage or base_rule.purity_percentage)
            buy_p = calculate_derived_rate(mr.buyback_price, base_rule.purity_percentage, rule.purity_percentage or base_rule.purity_percentage)
            purity_rates.append({
                "purity_rule_id": rule.id,
                "purity_name": rule.purity_name or "",
                "purity_percentage": float(rule.purity_percentage) if rule.purity_percentage is not None else None,
                "sell_price_per_gm": str(sell_p),
                "buy_price_per_gm": str(buy_p),
                "is_base": bool(getattr(rule, "is_base", False)),
            })
    if not purity_rates and base_rule and mr:
        purity_rates.append({
            "purity_rule_id": base_rule.id,
            "purity_name": base_rule.purity_name or "24K",
            "purity_percentage": float(base_rule.purity_percentage) if base_rule.purity_percentage else None,
            "sell_price_per_gm": str(mr.sell_price) if mr else None,
            "buy_price_per_gm": str(mr.buyback_price) if mr else None,
            "is_base": True,
        })
    return Response({"branch": branch_obj, "purity_rates": purity_rates})


def _branch_rates_update_impl(request, branch_id):
    """Update one branch's rates. branch_id = metal_id."""
    data = request.data if hasattr(request, "data") and request.data else {}
    metal_id = data.get("metal_id") or branch_id
    try:
        metal_id = int(metal_id)
    except (TypeError, ValueError):
        return Response({"error": "Invalid metal_id"}, status=status.HTTP_400_BAD_REQUEST)
    rates_payload = data.get("rates") or []
    if not isinstance(rates_payload, list):
        return Response({"error": "rates must be an array"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        metal = Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return Response({"error": "Metal not found"}, status=status.HTTP_404_NOT_FOUND)
    branch_id_param = data.get("branch_id") or branch_id
    try:
        branch_id_param = int(branch_id_param) if branch_id_param else None
    except (TypeError, ValueError):
        branch_id_param = None
    base_rule = None
    try:
        base_rule = get_base_purity(metal_id, branch_id=branch_id_param)
    except MetalMasterRule.DoesNotExist:
        pass
    target_date = _parse_date_param(data.get("date"), allow_none=True)
    sell_price = None
    buyback_price = None
    for r in rates_payload:
        rid = r.get("purity_rule_id")
        if base_rule and rid == base_rule.id:
            sell_price = r.get("sell_price_per_gm")
            buyback_price = r.get("buy_price_per_gm")
            break
    if sell_price is None and rates_payload:
        first = rates_payload[0]
        sell_price = first.get("sell_price_per_gm")
        buyback_price = first.get("buy_price_per_gm")
    if sell_price is None:
        return Response({"error": "rates must include at least one entry with sell_price_per_gm (and optionally buy_price_per_gm)"}, status=status.HTTP_400_BAD_REQUEST)
    # Decide whether this should be treated as a branch update or a master update.
    # If we have a valid Branch, we keep previous behaviour (branch override);
    # otherwise we treat it as a master base‑rate update.
    branch_exists = bool(branch_id_param and Branch.objects.filter(id=branch_id_param).exists())

    # Store "Edit Metal Rates – Branch Wise": explicit flag = save each row as entered (no rule derivation).
    if branch_exists and data.get("manual_branch_rates"):
        try:
            obj, created = upsert_branch_rates_manual(
                metal_id=metal_id,
                branch_id=branch_id_param,
                effective_date=target_date,
                rates_payload=rates_payload,
            )
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "message": "Branch rates updated successfully",
            "data": {
                "id": obj.id,
                "metal_id": obj.metal_id,
                "effective_date": str(obj.effective_date),
                "sell_price": str(obj.sell_price),
                "buyback_price": str(obj.buyback_price),
                "manual_override": True,
            },
        })

    obj, created = calculate_and_update_metal_rates(
        metal_id=metal_id,
        base_sell_price=sell_price,
        effective_date=target_date,
        base_buyback_price=buyback_price or sell_price,
        branch_id=branch_id_param if branch_exists else None,
    )
    return Response({
        "message": "Branch rates updated successfully",
        "data": {
            "id": obj.id,
            "metal_id": obj.metal_id,
            "effective_date": str(obj.effective_date),
            "sell_price": str(obj.sell_price),
            "buyback_price": str(obj.buyback_price),
        },
    })
