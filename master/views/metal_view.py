import json
import logging
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from rest_framework import status as http_status

from master.permissions.permission_checker import admin_auth
from master.permissions.metal_read_auth import METAL_LIST_READ_AUTH
from shared.models import Metal, MetalMasterRule, Branch, BranchMetal, HSNMaster
from shared.services.metal_service import get_rules_for_metal

logger = logging.getLogger(__name__)

def _get_or_create_hsn(hsn_code: str, gst_rate, description: str = None):
    """
    Get HSN by code; create if not exists, else update gst_rate.
    Returns (hsn, error_message). error_message is None on success.
    """
    hsn_code = (hsn_code or "").strip()
    if not hsn_code:
        return None, "HSN code is required."
    try:
        gst = Decimal(str(gst_rate))
    except (InvalidOperation, TypeError, ValueError):
        return None, "GST rate must be a numeric value."
    if gst < 0:
        return None, "GST rate cannot be negative."

    hsn = HSNMaster.objects.filter(hsn_code__iexact=hsn_code).first()
    if hsn:
        hsn.gst_rate = gst
        if description is not None:
            hsn.description = (description or "").strip() or None
        hsn.save()
        return hsn, None
    hsn = HSNMaster.objects.create(
        hsn_code=hsn_code,
        description=(description or "").strip() or None,
        gst_rate=gst,
        cgst_rate=gst / 2,
        sgst_rate=gst / 2,
        igst_rate=gst,
        is_active=True,
    )
    return hsn, None


def _rule_to_json(rule):
    """Return rule payload matching MetalRule model; percentage for frontend compatibility."""
    pct = float(rule.purity_percentage) if rule.purity_percentage is not None else None
    return {
        "id": rule.id,
        "purity_name": rule.purity_name or "",
        "purity_percentage": pct,
        "percentage": str(rule.purity_percentage) if rule.purity_percentage is not None else "",
        "description": rule.description or "",
        "type": rule.type or "",
        "is_base": rule.is_base,
    }


@api_view(["GET"])
@admin_auth(*METAL_LIST_READ_AUTH)
def list_branches(request):
    """List branches (for Store section: branch selector)."""
    queryset = Branch.objects.filter(is_active=True).order_by("id")
    data = [{"id": b.id, "name": b.name or "", "code": b.code or ""} for b in queryset]
    return JsonResponse({"success": True, "data": data, "results": data})


@api_view(["POST"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_MASTERS_DAILY_GOLD_RATES_VIEW")
@csrf_exempt
def create_metal(request):
    """Create metal (global). metal_name required. Optional: hsn_code + gst_rate (create/update HSN first, then map) or hsn_id (legacy)."""
    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    metal_name = (data.get("metal_name") or "").strip()
    if not metal_name:
        return JsonResponse(
            {"success": False, "error": "metal_name is required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if Metal.objects.filter(metal_name=metal_name).exists():
        return JsonResponse(
            {"success": False, "error": "A metal with this name already exists."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    # HSN: create/update from hsn_code + gst_rate, or use hsn_id (legacy)
    hsn = None
    hsn_code = data.get("hsn_code")
    gst_rate = data.get("gst_rate")
    if hsn_code and gst_rate is not None and str(gst_rate).strip() != "":
        hsn, err = _get_or_create_hsn(hsn_code, gst_rate, description=metal_name)
        if err:
            return JsonResponse(
                {"success": False, "error": err},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
    else:
        hsn_id = data.get("hsn_id")
        if hsn_id not in (None, "", 0, "0"):
            try:
                hsn_id_int = int(hsn_id)
                hsn = HSNMaster.objects.get(id=hsn_id_int, is_active=True)
            except (TypeError, ValueError, HSNMaster.DoesNotExist):
                return JsonResponse(
                    {"success": False, "error": "Invalid HSN code"},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )

    is_active = data.get("is_active", True)
    if not isinstance(is_active, bool):
        is_active = str(is_active).lower() in ("true", "1", "yes")

    metal = Metal.objects.create(metal_name=metal_name, hsn=hsn, is_active=is_active)

    return JsonResponse({
        "success": True,
        "message": "Metal created",
        "id": metal.id,
        "data": {
            "id": metal.id,
            "metal_name": metal.metal_name,
            "is_active": metal.is_active,
        },
    }, status=http_status.HTTP_201_CREATED)


@api_view(["GET"])
@admin_auth(*METAL_LIST_READ_AUTH)
def list_metals(request):
    """
    List metals. Optional: is_active, branch_id (for rules).
    When branch_id given: return only metals mapped for that branch (JOIN branch_metal, is_active).
    When no branch_id: return all global metals (Masters).
    """
    is_active = request.GET.get("is_active")
    branch_id = request.GET.get("branch_id")
    if branch_id is not None:
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            branch_id = None

    if branch_id is not None:
        # Store: only metals mapped for this branch (JOIN branch_metal)
        metal_ids = BranchMetal.objects.filter(
            branch_id=branch_id, is_active=True
        ).values_list("metal_id", flat=True)
        queryset = Metal.objects.select_related("hsn").filter(id__in=metal_ids, is_active=True).order_by("-id")
    else:
        # Masters: all global metals
        queryset = Metal.objects.select_related("hsn").all().order_by("-id")

    if is_active is not None:
        if str(is_active).lower() in ("true", "1", "yes"):
            queryset = queryset.filter(is_active=True)
        elif str(is_active).lower() in ("false", "0", "no"):
            queryset = queryset.filter(is_active=False)

    data = []
    for metal in queryset:
        rules = get_rules_for_metal(metal.id, branch_id=branch_id)
        rule_list = [_rule_to_json(r) for r in rules]
        
        # Safe HSN access pattern - handle missing HSN gracefully
        hsn = None
        try:
            hsn = metal.hsn
        except Exception:
            hsn = None
        
        if not hsn:
            logger.warning(f"HSN missing for metal_id={metal.id}")
        
        data.append({
            "id": metal.id,
            "metal_name": metal.metal_name,
            "is_active": getattr(metal, "is_active", True),
            "hsn_id": hsn.id if hsn else None,
            "hsn_code": getattr(hsn, "hsn_code", None) if hsn else None,
            "hsn_description": getattr(hsn, "description", None) if hsn else None,
            "gst_rate": float(hsn.gst_rate) if hsn and hsn.gst_rate is not None else None,
            "rules": rule_list,
        })

    return JsonResponse({
        "success": True,
        "data": data,
        "results": data,
    })


@api_view(["GET"])
@admin_auth(*METAL_LIST_READ_AUTH)
def get_metal(request, metal_id):
    """Get single metal by id with rules. Optional branch_id: branch override else master."""
    try:
        metal = Metal.objects.select_related("hsn").get(id=metal_id)
    except Metal.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Metal not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )
    branch_id = request.GET.get("branch_id")
    if branch_id is not None:
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            branch_id = None
    rules = get_rules_for_metal(metal.id, branch_id=branch_id)
    rule_list = [_rule_to_json(r) for r in rules]

    # Safe HSN access pattern - handle missing HSN gracefully
    hsn = None
    try:
        hsn = metal.hsn
    except Exception:
        hsn = None

    if not hsn:
        logger.warning(f"HSN missing for metal_id={metal.id}")

    return JsonResponse({
        "success": True,
        "data": {
            "id": metal.id,
            "metal_name": metal.metal_name,
            "is_active": getattr(metal, "is_active", True),
            "hsn_id": hsn.id if hsn else None,
            "hsn_code": getattr(hsn, "hsn_code", None) if hsn else None,
            "hsn_description": getattr(hsn, "description", None) if hsn else None,
            "gst_rate": float(hsn.gst_rate) if hsn and hsn.gst_rate is not None else None,
            "rules": rule_list,
        },
    })


@api_view(["PUT"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_MASTERS_DAILY_GOLD_RATES_VIEW")
@csrf_exempt
def update_metal(request, metal_id):
    """Update metal (global). metal_name, optional hsn_code+gst_rate (create/update HSN first) or hsn_id (legacy), is_active."""
    try:
        metal = Metal.objects.get(id=metal_id)
    except Metal.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Metal not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    metal_name = data.get("metal_name")
    if metal_name is not None:
        metal_name = str(metal_name).strip()
        if not metal_name:
            return JsonResponse(
                {"success": False, "error": "metal_name cannot be empty"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if Metal.objects.filter(metal_name=metal_name).exclude(pk=metal.id).exists():
            return JsonResponse(
                {"success": False, "error": "A metal with this name already exists."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        metal.metal_name = metal_name

    # HSN: create/update from hsn_code + gst_rate, or use hsn_id (legacy)
    hsn_code = data.get("hsn_code")
    gst_rate = data.get("gst_rate")
    if hsn_code is not None and gst_rate is not None and str(gst_rate).strip() != "":
        hsn, err = _get_or_create_hsn(hsn_code, gst_rate, description=metal.metal_name)
        if err:
            return JsonResponse(
                {"success": False, "error": err},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        metal.hsn = hsn
    elif "hsn_id" in data:
        hsn_id = data.get("hsn_id")
        if hsn_id in (None, "", 0, "0"):
            metal.hsn = None
        else:
            try:
                hsn_id_int = int(hsn_id)
                hsn = HSNMaster.objects.get(id=hsn_id_int, is_active=True)
            except (TypeError, ValueError, HSNMaster.DoesNotExist):
                return JsonResponse(
                    {"success": False, "error": "Invalid HSN code"},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            metal.hsn = hsn

    if "is_active" in data:
        metal.is_active = bool(data["is_active"]) if isinstance(data["is_active"], bool) else str(data["is_active"]).lower() in ("true", "1", "yes")

    metal.save()

    return JsonResponse({
        "success": True,
        "message": "Updated successfully",
        "data": {
            "id": metal.id,
            "metal_name": metal.metal_name,
            "is_active": metal.is_active,
        },
    })


@api_view(["DELETE"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_MASTERS_DAILY_GOLD_RATES_VIEW")
@csrf_exempt
def delete_metal(request, metal_id):
    """Delete metal by id."""
    try:
        metal = Metal.objects.get(id=metal_id)
        metal.delete()
        return JsonResponse({
            "success": True,
            "message": "Deleted successfully",
        })
    except Metal.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Metal not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )


# ----- Branch–metal mapping (Store: control metal availability per branch) -----

@api_view(["GET"])
@admin_auth(*METAL_LIST_READ_AUTH)
def list_branch_metals(request):
    """List branch–metal mappings for a branch. Query: branch_id (required). Returns metals with is_active per branch."""
    branch_id = request.GET.get("branch_id")
    if not branch_id:
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
    bms = BranchMetal.objects.filter(branch_id=branch_id).select_related("metal", "branch").order_by("metal_id")
    data = [
        {
            "id": bm.id,
            "branch_id": bm.branch_id,
            "branch_name": bm.branch.name if bm.branch else "",
            "metal_id": bm.metal_id,
            "metal_name": bm.metal.metal_name if bm.metal else "",
            "is_active": bm.is_active,
        }
        for bm in bms
    ]
    return JsonResponse({"success": True, "data": data, "results": data})


@api_view(["POST"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_STORES_METAL_CONFIGURATION", "CRM_STORES")
@csrf_exempt
def create_or_toggle_branch_metal(request):
    """Create or toggle branch–metal. Body: branch_id, metal_id, is_active (optional, default True)."""
    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    branch_id = data.get("branch_id")
    metal_id = data.get("metal_id")
    if branch_id is None or metal_id is None:
        return JsonResponse(
            {"success": False, "error": "branch_id and metal_id are required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    try:
        branch_id = int(branch_id)
        metal_id = int(metal_id)
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "error": "branch_id and metal_id must be integers"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    is_active = data.get("is_active", True)
    if not isinstance(is_active, bool):
        is_active = str(is_active).lower() in ("true", "1", "yes")
    try:
        Branch.objects.get(id=branch_id)
        Metal.objects.get(id=metal_id)
    except Branch.DoesNotExist:
        return JsonResponse({"success": False, "error": "Branch not found"}, status=http_status.HTTP_404_NOT_FOUND)
    except Metal.DoesNotExist:
        return JsonResponse({"success": False, "error": "Metal not found"}, status=http_status.HTTP_404_NOT_FOUND)
    bm, created = BranchMetal.objects.update_or_create(
        branch_id=branch_id,
        metal_id=metal_id,
        defaults={"is_active": is_active},
    )
    return JsonResponse({
        "success": True,
        "message": "Created" if created else "Updated",
        "data": {
            "id": bm.id,
            "branch_id": bm.branch_id,
            "metal_id": bm.metal_id,
            "is_active": bm.is_active,
        },
    }, status=http_status.HTTP_201_CREATED if created else 200)


@api_view(["PUT"])
@admin_auth("CRM_MASTERS_METAL_VIEW", "CRM_STORES_METAL_CONFIGURATION", "CRM_STORES")
@csrf_exempt
def update_branch_metal(request, branch_metal_id):
    """Update branch–metal (toggle is_active). Body: is_active."""
    try:
        bm = BranchMetal.objects.select_related("metal", "branch").get(id=branch_metal_id)
    except BranchMetal.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Branch-metal mapping not found"},
            status=http_status.HTTP_404_NOT_FOUND,
        )
    try:
        data = request.data if hasattr(request, "data") and request.data else json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if "is_active" in data:
        bm.is_active = bool(data["is_active"]) if isinstance(data["is_active"], bool) else str(data["is_active"]).lower() in ("true", "1", "yes")
        bm.save()
    return JsonResponse({
        "success": True,
        "message": "Updated",
        "data": {
            "id": bm.id,
            "branch_id": bm.branch_id,
            "metal_id": bm.metal_id,
            "is_active": bm.is_active,
        },
    })
