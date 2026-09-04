"""
Gold rate API: backed by Metal rate (Gold metal) only. No GoldRate model.
Same response shape for backward compatibility with any existing frontend.
"""
import math
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone

from master.permissions.permission_checker import admin_auth
from shared.models import MetalMasterRate, MetalMasterRule
from shared.services.metal_rate_service import (
    get_default_gold_metal_id,
    calculate_and_update_metal_rates,
)


@api_view(["GET"])
@admin_auth("CRM_MASTERS_DAILY_GOLD_RATES_VIEW")
def gold_rate_list(request):
    """List metal rates for Gold metal (same shape as legacy gold rate list)."""
    metal_id = get_default_gold_metal_id()
    if not metal_id:
        return Response([])
    qs = (
        MetalMasterRate.objects.filter(metal_id=metal_id, is_active=True)
        .order_by("-effective_date", "-system_created_at")
    )
    data = [
        {
            "id": mr.id,
            "rate_date": mr.effective_date,
            "purity": mr.purity_name or "24K",
            "rate_value": str(mr.sell_price),
            "is_locked": False,
            "system_created_at": mr.system_created_at,
        }
        for mr in qs
    ]
    return Response(data)


@api_view(["PUT"])
@admin_auth("CRM_MASTERS_DAILY_GOLD_RATES_UPDATE")
def update_gold_rate(request, pk):
    """
    Update 24K rate for Gold metal. Other purities derived via MetalMasterRule.
    Uses Metal rate (calculate_and_update_metal_rates) instead of GoldRate.
    """
    metal_id = get_default_gold_metal_id()
    if not metal_id:
        return Response(
            {"error": "Gold metal not found. Create a Metal with name 'Gold'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        base_rate = MetalMasterRate.objects.get(id=pk)
    except MetalMasterRate.DoesNotExist:
        return Response({"error": "Rate not found"}, status=status.HTTP_404_NOT_FOUND)
    if base_rate.metal_id != metal_id:
        return Response(
            {"error": "Rate is not for Gold metal"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    new_rate = request.data.get("rate_value")
    if not new_rate:
        return Response(
            {"error": "rate_value is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        new_rate = float(new_rate)
    except (TypeError, ValueError):
        return Response(
            {"error": "rate_value must be a number"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    today = base_rate.effective_date
    calculate_and_update_metal_rates(
        metal_id,
        new_rate,
        effective_date=today,
        base_buyback_price=new_rate,
        branch_id=None,
    )
    return Response({
        "message": "Gold rate updated successfully (metal rate)",
        "base_rate": {"purity": "24K", "rate_value": str(new_rate)},
    })


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GOLD_RATE_RULES_VIEW")
def gold_rate_rule_list(request):
    """List metal rules for Gold metal (same shape as legacy gold rate rules)."""
    metal_id = get_default_gold_metal_id()
    if not metal_id:
        return Response([])
    rules = MetalMasterRule.objects.filter(metal_id=metal_id)
    data = [
        {
            "id": r.id,
            "purity": r.purity_name or "24K",
            "rate_source": "MANUAL",
            "lock_type": "INSTALMENT",
            "rounding_rule": "FLOOR",
            "system_created_at": r.system_created_at,
        }
        for r in rules
    ]
    return Response(data)


@api_view(["PUT"])
@admin_auth("CRM_MASTERS_GOLD_RATE_RULES_UPDATE")
def gold_rate_rule_update(request, pk):
    """Update a metal rule for Gold (MetalMasterRule)."""
    try:
        rule = MetalMasterRule.objects.get(id=pk)
    except MetalMasterRule.DoesNotExist:
        return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)
    if rule.metal_id != get_default_gold_metal_id():
        return Response(
            {"error": "Rule is not for Gold metal"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = request.data
    if "purity" in data:
        rule.purity_name = data.get("purity", rule.purity_name)
    rule.save()
    return Response({"message": "Gold rate rule updated"})


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GOLD_RATE_RULES_CREATE")
def create_gold_rate_rule(request):
    """Create a metal rule for Gold (MetalMasterRule). Purity e.g. 22K, 20K."""
    metal_id = get_default_gold_metal_id()
    if not metal_id:
        return Response(
            {"error": "Gold metal not found. Create a Metal with name 'Gold'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = request.data
    purity = (data.get("purity") or "").strip() or None
    if not purity:
        return Response(
            {"error": "purity is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if MetalMasterRule.objects.filter(metal_id=metal_id, purity_name=purity).exists():
        return Response(
            {"error": f"Rule for {purity} already exists"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    rule = MetalMasterRule.objects.create(
        metal_id=metal_id,
        purity_name=purity,
        is_base=(purity == "24K"),
    )
    return Response(
        {
            "message": "Gold rate rule created successfully",
            "data": {
                "id": rule.id,
                "purity": rule.purity_name,
                "rate_source": "MANUAL",
                "lock_type": "INSTALMENT",
                "rounding_rule": "FLOOR",
                "system_created_at": rule.system_created_at,
            },
        },
        status=status.HTTP_201_CREATED,
    )
