import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from rest_framework import status as http_status

from master.permissions.permission_checker import admin_auth
from master.permissions.metal_read_auth import METAL_RULE_READ_AUTH, MASTER_METAL_RULE_WRITE_AUTH
from shared.models import Metal, MetalMasterRule, MetalBranchRule, Branch
from shared.services.metal_service import get_rules_for_metal


def _parse_purity_percentage(value):
    """Return float for percentage or None. Valid range 0-100."""
    if value is None:
        return None
    try:
        pct = float(value)
        return pct
    except (TypeError, ValueError):
        return None


def _rule_payload(rule):
    """Return rule payload; works for MetalMasterRule or MetalBranchRule."""
    pct = float(rule.purity_percentage) if rule.purity_percentage is not None else None
    metal_id = getattr(rule, "metal_id", None) or (rule.metal_id if hasattr(rule, "metal") else None)
    if metal_id is None and hasattr(rule, "metal"):
        metal_id = rule.metal.id
    is_branch = isinstance(rule, MetalBranchRule)
    return {
        "id": rule.id,
        "metal_id": metal_id,
        "purity_name": rule.purity_name or "",
        "purity_percentage": pct,
        "percentage": str(rule.purity_percentage) if rule.purity_percentage is not None else "",
        "description": rule.description or "",
        "type": rule.type or "",
        "is_base": rule.is_base,
        "is_branch_rule": is_branch,
        "branch_id": getattr(rule, "branch_id", None) if is_branch else None,
    }


@api_view(["POST"])
@admin_auth(*MASTER_METAL_RULE_WRITE_AUTH)
@csrf_exempt
def create_rule(request):
    """
    Create metal rule. Body: metal_id, purity_name, purity_percentage, is_base; optional: description, type.
    Validates: purity_name required; purity_percentage in 0-100; no duplicate purity per metal;
    if is_base=true, ensures no other rule for same metal has is_base=true (model save also unsets others).
    """
    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    metal_id = data.get("metal_id")
    if metal_id is None:
        return JsonResponse(
            {"success": False, "error": "metal_id is required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    try:
        metal = Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Metal not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    purity_name = (data.get("purity_name") or "").strip()
    if not purity_name:
        return JsonResponse(
            {"success": False, "error": "purity_name is required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    purity_percentage = _parse_purity_percentage(data.get("purity_percentage"))
    if purity_percentage is None:
        return JsonResponse(
            {"success": False, "error": "purity_percentage is required and must be a number."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if purity_percentage < 0 or purity_percentage > 100:
        return JsonResponse(
            {"success": False, "error": "purity_percentage must be between 0 and 100."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if MetalMasterRule.objects.filter(metal_id=metal_id, purity_name=purity_name).exists():
        return JsonResponse(
            {"success": False, "error": f"A rule with purity_name '{purity_name}' already exists for this metal."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    is_base = data.get("is_base", False)
    if not isinstance(is_base, bool):
        is_base = str(is_base).lower() in ("true", "1", "yes")

    if is_base:
        existing_base = MetalMasterRule.objects.filter(metal_id=metal_id, is_base=True).first()
        if existing_base:
            MetalMasterRule.objects.filter(metal_id=metal_id, is_base=True).update(is_base=False)

    description = (data.get("description") or "").strip() or None
    rule_type = (data.get("type") or "").strip() or None

    rule = MetalMasterRule.objects.create(
        metal=metal,
        purity_name=purity_name,
        purity_percentage=purity_percentage,
        description=description,
        type=rule_type,
        is_base=is_base,
    )

    return JsonResponse({
        "success": True,
        "message": "Rule created",
        "data": _rule_payload(rule),
    }, status=http_status.HTTP_201_CREATED)


@api_view(["GET"])
@admin_auth(*METAL_RULE_READ_AUTH)
def list_rules(request, metal_id=None):
    """
    List rules; optional metal_id, branch_id.

    Behaviour:
    - If only metal_id is provided → returns effective rules for that metal
      (branch override when present, else master) as a flat list in `data`.
    - If only branch_id is provided → returns all branch rules for that branch.
    - If neither is provided → returns all master rules.
    - If BOTH metal_id and branch_id are provided → returns:
        * `data`: effective rules for that metal at the branch (backwards compatible).
        * `comparison`: merged view of master + branch rules grouped by purity_name:
            {
              "metal_id": 1,
              "metal_name": "Gold",
              "rules": [
                {
                  "purity_name": "24K",
                  "master_rule": {...} | null,
                  "branch_rule": {...} | null,
                  "is_override": true | false
                },
                ...
              ]
            }
    This allows the frontend to compare master vs branch rules side‑by‑side
    without breaking existing consumers that rely on the `data` array.
    """
    metal_id_param = request.GET.get("metal_id") or metal_id
    branch_id = request.GET.get("branch_id")
    if branch_id is not None:
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            branch_id = None
    if metal_id_param is not None:
        try:
            metal_id_param = int(metal_id_param)
        except (TypeError, ValueError):
            return JsonResponse(
                {"success": False, "error": "Invalid metal_id"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
    payload = {"success": True}

    # BOTH metal_id and branch_id → return effective list + master/branch comparison
    if metal_id_param is not None and branch_id is not None:
        # Backwards‑compatible flat list (effective rules: branch override → master)
        rules_effective = get_rules_for_metal(metal_id_param, branch_id=branch_id)
        payload["data"] = [_rule_payload(r) for r in rules_effective]

        # Comparison payload: master + branch rules grouped by purity_name
        try:
            metal_obj = Metal.objects.get(id=metal_id_param)
            metal_name = metal_obj.metal_name or ""
        except Metal.DoesNotExist:
            metal_name = ""

        master_qs = MetalMasterRule.objects.filter(metal_id=metal_id_param).order_by("id")
        branch_qs = MetalBranchRule.objects.filter(
            branch_id=branch_id,
            metal_id=metal_id_param,
            is_current=True,
        ).order_by("id")

        combined: dict[str, dict] = {}

        # Helper to normalise purity key
        def _key(name):
            return (name or "").strip().upper()

        for r in master_qs:
            k = _key(r.purity_name)
            item = combined.setdefault(
                k,
                {
                    "purity_name": r.purity_name or "",
                    "master_rule": None,
                    "branch_rule": None,
                    "is_override": False,
                },
            )
            item["purity_name"] = r.purity_name or item["purity_name"]
            item["master_rule"] = {
                "id": r.id,
                "purity_percentage": float(r.purity_percentage)
                if r.purity_percentage is not None
                else None,
                "is_base": bool(r.is_base),
            }

        for r in branch_qs:
            k = _key(r.purity_name)
            item = combined.setdefault(
                k,
                {
                    "purity_name": r.purity_name or "",
                    "master_rule": None,
                    "branch_rule": None,
                    "is_override": False,
                },
            )
            item["purity_name"] = r.purity_name or item["purity_name"]
            item["branch_rule"] = {
                "id": r.id,
                "purity_percentage": float(r.purity_percentage)
                if r.purity_percentage is not None
                else None,
                "is_base": bool(r.is_base),
            }
            item["is_override"] = True

        # Stable ordering: by purity_percentage (desc) then name
        def _sort_key(item):
            pr = item.get("branch_rule") or item.get("master_rule") or {}
            pct = pr.get("purity_percentage") or 0
            return (-pct, item.get("purity_name") or "")

        merged_rules = sorted(combined.values(), key=_sort_key)

        payload["comparison"] = {
            "metal_id": metal_id_param,
            "metal_name": metal_name,
            "rules": merged_rules,
        }
    # Only metal_id (masters / effective rules at HO)
    elif metal_id_param is not None:
        rules = get_rules_for_metal(metal_id_param, branch_id=None)
        payload["data"] = [_rule_payload(r) for r in rules]
    # Only branch_id or neither → keep legacy behaviour
    else:
        if branch_id:
            queryset = (
                MetalBranchRule.objects.filter(branch_id=branch_id, is_current=True)
                .select_related("metal")
                .order_by("metal_id", "id")
            )
        else:
            queryset = (
                MetalMasterRule.objects.all()
                .select_related("metal")
                .order_by("metal_id", "id")
            )
        payload["data"] = [_rule_payload(r) for r in queryset]

    return JsonResponse(payload)


@api_view(["PUT"])
@admin_auth(*MASTER_METAL_RULE_WRITE_AUTH)
@csrf_exempt
def update_rule(request, rule_id):
    """
    Update rule: purity_name, purity_percentage, is_base; optional: description, type.
    If setting is_base=true, previous base for same metal is unset (via model save).
    Validates percentage 0-100 and duplicate purity per metal.
    """
    try:
        rule = MetalMasterRule.objects.select_related("metal").get(id=rule_id)
    except MetalMasterRule.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Rule not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if "purity_name" in data:
        purity_name = (data.get("purity_name") or "").strip()
        if not purity_name:
            return JsonResponse(
                {"success": False, "error": "purity_name cannot be empty"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if MetalMasterRule.objects.filter(metal_id=rule.metal_id, purity_name=purity_name).exclude(pk=rule.id).exists():
            return JsonResponse(
                {"success": False, "error": f"A rule with purity_name '{purity_name}' already exists for this metal."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        rule.purity_name = purity_name

    if "purity_percentage" in data:
        pct = _parse_purity_percentage(data["purity_percentage"])
        if pct is None:
            return JsonResponse(
                {"success": False, "error": "purity_percentage must be a number."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if pct < 0 or pct > 100:
            return JsonResponse(
                {"success": False, "error": "purity_percentage must be between 0 and 100."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        rule.purity_percentage = pct

    if "is_base" in data:
        rule.is_base = bool(data["is_base"]) if isinstance(data["is_base"], bool) else str(data["is_base"]).lower() in ("true", "1", "yes")
        # Model save() will unset other bases for same metal

    if "description" in data:
        rule.description = (data.get("description") or "").strip() or None
    if "type" in data:
        rule.type = (data.get("type") or "").strip() or None

    rule.save()

    return JsonResponse({
        "success": True,
        "message": "Rule updated",
        "data": _rule_payload(rule),
    })


@api_view(["DELETE"])
@admin_auth(*MASTER_METAL_RULE_WRITE_AUTH)
@csrf_exempt
def delete_rule(request, rule_id):
    """Delete rule. Prevents deleting the base purity unless another rule for the same metal is set as base."""
    try:
        rule = MetalMasterRule.objects.get(id=rule_id)
    except MetalMasterRule.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Rule not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    if rule.is_base:
        other_rules = MetalMasterRule.objects.filter(metal_id=rule.metal_id).exclude(pk=rule.id)
        if not other_rules.exists():
            return JsonResponse(
                {
                    "success": False,
                    "error": "Cannot delete the only rule for this metal. Add another rule first.",
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        other_base = other_rules.filter(is_base=True).exists()
        if not other_base:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Cannot delete the base purity rule. Set another rule as base first, then delete this one.",
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )

    rule.delete()
    return JsonResponse({
        "success": True,
        "message": "Rule deleted",
    })


# ----- Branch rules (Store section: branch-wise purity overrides) -----

@api_view(["POST"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_STORES_METAL_CONFIGURATION", "CRM_STORES")
@csrf_exempt
def create_branch_rule(request):
    """
    Create branch rule. Body: branch_id, metal_id, purity_name, purity_percentage, is_base; optional: description, type.
    For Store section: branch-wise purity override.
    """
    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    branch_id = data.get("branch_id")
    if branch_id is None:
        return JsonResponse(
            {"success": False, "error": "branch_id is required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "error": "branch_id must be an integer"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    try:
        branch = Branch.objects.get(id=branch_id)
    except Branch.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Branch not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    metal_id = data.get("metal_id")
    if metal_id is None:
        return JsonResponse(
            {"success": False, "error": "metal_id is required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    try:
        metal = Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Metal not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    purity_name = (data.get("purity_name") or "").strip()
    if not purity_name:
        return JsonResponse(
            {"success": False, "error": "purity_name is required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    purity_percentage = _parse_purity_percentage(data.get("purity_percentage"))
    if purity_percentage is None:
        return JsonResponse(
            {"success": False, "error": "purity_percentage is required and must be a number."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if purity_percentage < 0 or purity_percentage > 100:
        return JsonResponse(
            {"success": False, "error": "purity_percentage must be between 0 and 100."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if MetalBranchRule.objects.filter(branch_id=branch_id, metal_id=metal_id, purity_name=purity_name).exists():
        return JsonResponse(
            {"success": False, "error": f"A rule with purity_name '{purity_name}' already exists for this branch and metal."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    is_base = data.get("is_base", False)
    if not isinstance(is_base, bool):
        is_base = str(is_base).lower() in ("true", "1", "yes")

    if is_base:
        MetalBranchRule.objects.filter(branch_id=branch_id, metal_id=metal_id, is_base=True).update(is_base=False)

    description = (data.get("description") or "").strip() or None
    rule_type = (data.get("type") or "").strip() or None

    rule = MetalBranchRule.objects.create(
        branch=branch,
        metal=metal,
        purity_name=purity_name,
        purity_percentage=purity_percentage,
        description=description,
        type=rule_type,
        is_base=is_base,
        is_current=True,
    )

    return JsonResponse({
        "success": True,
        "message": "Branch rule created",
        "data": _rule_payload(rule),
    }, status=http_status.HTTP_201_CREATED)


@api_view(["PUT"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_STORES_METAL_CONFIGURATION", "CRM_STORES")
@csrf_exempt
def update_branch_rule(request, rule_id):
    """Update branch rule. Body: purity_name, purity_percentage, is_base; optional: description, type."""
    try:
        rule = MetalBranchRule.objects.select_related("metal", "branch").get(id=rule_id)
    except MetalBranchRule.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Branch rule not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if "purity_name" in data:
        purity_name = (data.get("purity_name") or "").strip()
        if not purity_name:
            return JsonResponse(
                {"success": False, "error": "purity_name cannot be empty"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if MetalBranchRule.objects.filter(
            branch_id=rule.branch_id, metal_id=rule.metal_id, purity_name=purity_name
        ).exclude(pk=rule.id).exists():
            return JsonResponse(
                {"success": False, "error": f"A rule with purity_name '{purity_name}' already exists for this branch and metal."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        rule.purity_name = purity_name

    if "purity_percentage" in data:
        pct = _parse_purity_percentage(data["purity_percentage"])
        if pct is None:
            return JsonResponse(
                {"success": False, "error": "purity_percentage must be a number."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if pct < 0 or pct > 100:
            return JsonResponse(
                {"success": False, "error": "purity_percentage must be between 0 and 100."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        rule.purity_percentage = pct

    if "is_base" in data:
        rule.is_base = bool(data["is_base"]) if isinstance(data["is_base"], bool) else str(data["is_base"]).lower() in ("true", "1", "yes")
        if rule.is_base:
            MetalBranchRule.objects.filter(
                branch_id=rule.branch_id, metal_id=rule.metal_id, is_base=True
            ).exclude(pk=rule.id).update(is_base=False)

    if "description" in data:
        rule.description = (data.get("description") or "").strip() or None
    if "type" in data:
        rule.type = (data.get("type") or "").strip() or None

    rule.save()

    return JsonResponse({
        "success": True,
        "message": "Branch rule updated",
        "data": _rule_payload(rule),
    })


@api_view(["DELETE"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_STORES_METAL_CONFIGURATION", "CRM_STORES")
@csrf_exempt
def delete_branch_rule(request, rule_id):
    """Delete branch rule. Prevents deleting the base purity unless another rule is set as base."""
    try:
        rule = MetalBranchRule.objects.get(id=rule_id)
    except MetalBranchRule.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Branch rule not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    if rule.is_base:
        other_rules = MetalBranchRule.objects.filter(
            branch_id=rule.branch_id, metal_id=rule.metal_id
        ).exclude(pk=rule.id)
        if not other_rules.exists():
            return JsonResponse(
                {
                    "success": False,
                    "error": "Cannot delete the only rule for this branch and metal. Add another rule first.",
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if not other_rules.filter(is_base=True).exists():
            return JsonResponse(
                {
                    "success": False,
                    "error": "Cannot delete the base purity rule. Set another rule as base first.",
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )

    rule.delete()
    return JsonResponse({
        "success": True,
        "message": "Branch rule deleted",
    })
