"""CRUD for pattern code registry (scoped to item type + store variant 1:1)."""
import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from rest_framework.decorators import api_view

from master.permissions.permission_checker import admin_auth
from master.views.product_views import get_admin_user_from_request
from shared.models import PatternCodeRegistry
from shared.services.pattern_code_registry import (
    _row_to_dict,
    bind_pattern_store_variant,
    ensure_pattern_code_row,
    normalize_pattern_code,
    seed_pattern_codes_from_catalog,
    validate_pattern_store_mapping,
)


def _admin_user(request):
    return get_admin_user_from_request(request) or (
        request.user if getattr(request, "user", None) and request.user.is_authenticated else None
    )


def _body(request) -> dict:
    if isinstance(request.data, dict) and request.data:
        return request.data
    try:
        return json.loads(request.body or "{}")
    except Exception:
        return {}


@api_view(["GET"])
@admin_auth()
def list_pattern_codes(request):
    """GET /master/pattern-codes/?is_active=true&q=&subcategory_id=&category_id="""
    try:
        qs = PatternCodeRegistry.objects.select_related("category", "subcategory").order_by(
            "pattern_code"
        )
        active = request.GET.get("is_active")
        if active is not None:
            qs = qs.filter(is_active=str(active).lower() in ("1", "true", "yes"))
        sub_id = (request.GET.get("subcategory_id") or "").strip()
        if sub_id:
            qs = qs.filter(subcategory_id=sub_id)
        cat_id = (request.GET.get("category_id") or "").strip()
        if cat_id:
            qs = qs.filter(category_id=cat_id)
        q = (request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(pattern_code__icontains=q.replace(" ", "")) | qs.filter(
                store_variant_name__icontains=q
            )
        return JsonResponse([_row_to_dict(r) for r in qs], safe=False, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def create_pattern_code(request):
    """
    POST /master/pattern-codes/create/
    Body: { pattern_code, subcategory_id, category_id?, description? }
    """
    try:
        data = _body(request)
        code = normalize_pattern_code(data.get("pattern_code", ""))
        if not code:
            return JsonResponse({"error": "pattern_code is required"}, status=400)
        if not data.get("subcategory_id"):
            return JsonResponse({"error": "subcategory_id (item type) is required"}, status=400)
        if PatternCodeRegistry.objects.filter(pattern_code=code).exists():
            return JsonResponse({"error": f"Pattern code '{code}' already exists"}, status=400)

        row = ensure_pattern_code_row(
            code,
            description=(data.get("description") or "").strip(),
            category_id=data.get("category_id"),
            subcategory_id=data.get("subcategory_id"),
            admin_user=_admin_user(request),
        )
        return JsonResponse(_row_to_dict(row), status=201)
    except ValidationError as e:
        msg = e.messages[0] if getattr(e, "messages", None) else str(e)
        return JsonResponse({"error": msg}, status=400)
    except IntegrityError:
        return JsonResponse({"error": "Pattern code already exists"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def ensure_pattern_code(request):
    """
    POST /master/pattern-codes/ensure/
    Body: { pattern_code, subcategory_id, category_id?, description? }
    """
    try:
        data = _body(request)
        code = normalize_pattern_code(data.get("pattern_code", ""))
        if not code:
            return JsonResponse({"error": "pattern_code is required"}, status=400)
        if not data.get("subcategory_id"):
            return JsonResponse({"error": "subcategory_id (item type) is required"}, status=400)
        row = ensure_pattern_code_row(
            code,
            description=(data.get("description") or "").strip(),
            category_id=data.get("category_id"),
            subcategory_id=data.get("subcategory_id"),
            admin_user=_admin_user(request),
        )
        return JsonResponse(_row_to_dict(row), status=200)
    except ValidationError as e:
        msg = e.messages[0] if getattr(e, "messages", None) else str(e)
        return JsonResponse({"error": msg}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def bind_pattern_code(request):
    """
    POST /master/pattern-codes/bind/
    Body: { pattern_code, store_variant_name, subcategory_id, category_id? }
    """
    try:
        data = _body(request)
        code = normalize_pattern_code(data.get("pattern_code", ""))
        name = (data.get("store_variant_name") or "").strip()
        if not code:
            return JsonResponse({"error": "pattern_code is required"}, status=400)
        if not name:
            return JsonResponse({"error": "store_variant_name is required"}, status=400)
        if not data.get("subcategory_id"):
            return JsonResponse({"error": "subcategory_id (item type) is required"}, status=400)
        row = bind_pattern_store_variant(
            code,
            name,
            category_id=data.get("category_id"),
            subcategory_id=data.get("subcategory_id"),
            admin_user=_admin_user(request),
        )
        return JsonResponse(_row_to_dict(row), status=200)
    except ValidationError as e:
        msg = e.messages[0] if getattr(e, "messages", None) else str(e)
        return JsonResponse({"error": msg}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def validate_pattern_mapping(request):
    """
    POST /master/pattern-codes/validate/
    Body: { pattern_code, store_variant_name, subcategory_id, category_id? }
    """
    try:
        data = _body(request)
        validate_pattern_store_mapping(
            data.get("pattern_code", ""),
            data.get("store_variant_name", ""),
            category_id=data.get("category_id"),
            subcategory_id=data.get("subcategory_id"),
        )
        return JsonResponse({"valid": True}, status=200)
    except ValidationError as e:
        msg = e.messages[0] if getattr(e, "messages", None) else str(e)
        return JsonResponse({"valid": False, "error": msg}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def sync_pattern_codes(request):
    """POST /master/pattern-codes/sync/ — seed from published catalog."""
    try:
        created = seed_pattern_codes_from_catalog(admin_user=_admin_user(request))
        total = PatternCodeRegistry.objects.count()
        return JsonResponse({"created": created, "total": total}, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
