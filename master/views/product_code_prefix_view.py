"""
CRUD for product code prefix counters (LR, NP, PRG, …).
"""
import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from rest_framework.decorators import api_view

from master.permissions.permission_checker import admin_auth
from master.views.product_views import get_admin_user_from_request
from shared.models import ProductCodePrefix
from shared.services.product_code_prefix import (
    DEFAULT_TAG_SEQ_START,
    bootstrap_next_sequence,
    ensure_prefix_row,
    max_existing_tag_suffix,
    normalize_prefix,
    seed_prefixes_from_catalog,
    validate_product_code_mapping,
)


def _admin_user(request):
    return get_admin_user_from_request(request) or (
        request.user if getattr(request, "user", None) and request.user.is_authenticated else None
    )


def _row_to_dict(row: ProductCodePrefix) -> dict:
    last_used = max_existing_tag_suffix(row.prefix)
    return {
        "id": row.id,
        "prefix": row.prefix,
        "category_id": row.category_id,
        "subcategory_id": row.subcategory_id,
        "start_sequence": row.start_sequence,
        "next_sequence": row.next_sequence,
        "last_used_sequence": last_used,
        "description": row.description or "",
        "is_active": row.is_active,
        "system_created_at": row.system_created_at.isoformat() if row.system_created_at else None,
        "system_updated_at": row.system_updated_at.isoformat() if row.system_updated_at else None,
    }


@api_view(["GET"])
@admin_auth()
def list_product_code_prefixes(request):
    """GET /master/product-code-prefixes/?is_active=true&category_id=&subcategory_id="""
    try:
        qs = ProductCodePrefix.objects.all().order_by("prefix")
        active = request.GET.get("is_active")
        if active is not None:
            qs = qs.filter(is_active=str(active).lower() in ("1", "true", "yes"))
        category_id = request.GET.get("category_id")
        if category_id not in (None, ""):
            try:
                qs = qs.filter(category_id=int(category_id))
            except (TypeError, ValueError):
                pass
        subcategory_id = request.GET.get("subcategory_id")
        if subcategory_id not in (None, ""):
            try:
                qs = qs.filter(subcategory_id=int(subcategory_id))
            except (TypeError, ValueError):
                pass
        return JsonResponse([_row_to_dict(r) for r in qs], safe=False, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def create_product_code_prefix(request):
    """
    POST /master/product-code-prefixes/create/
    Body: { "prefix": "LR", "start_sequence": 1001, "description": "..." }
    """
    try:
        data = json.loads(request.body or "{}")
        prefix = normalize_prefix(data.get("prefix", ""))
        if not prefix:
            return JsonResponse({"error": "prefix is required"}, status=400)

        try:
            start_sequence = int(data.get("start_sequence", DEFAULT_TAG_SEQ_START))
        except (TypeError, ValueError):
            return JsonResponse({"error": "start_sequence must be a positive integer"}, status=400)
        if start_sequence < 1:
            return JsonResponse({"error": "start_sequence must be >= 1"}, status=400)

        admin = _admin_user(request)
        if ProductCodePrefix.objects.filter(prefix=prefix).exists():
            return JsonResponse({"error": f"Prefix '{prefix}' already exists"}, status=400)

        category_id = data.get("category_id")
        subcategory_id = data.get("subcategory_id")
        try:
            category_id = int(category_id) if category_id not in (None, "") else None
        except (TypeError, ValueError):
            category_id = None
        try:
            subcategory_id = int(subcategory_id) if subcategory_id not in (None, "") else None
        except (TypeError, ValueError):
            subcategory_id = None

        if not category_id or not subcategory_id:
            return JsonResponse(
                {"error": "category_id and subcategory_id are required"},
                status=400,
            )
        try:
            validate_product_code_mapping(prefix, category_id, subcategory_id)
        except ValidationError as e:
            msg = e.messages[0] if getattr(e, "messages", None) else str(e)
            return JsonResponse({"error": msg}, status=400)

        row = ProductCodePrefix.objects.create(
            prefix=prefix,
            category_id=category_id,
            subcategory_id=subcategory_id,
            start_sequence=start_sequence,
            next_sequence=start_sequence,
            description=(data.get("description") or "").strip()[:255],
            is_active=bool(data.get("is_active", True)),
            created_by=admin,
            updated_by=admin,
        )
        safe_next = bootstrap_next_sequence(prefix, floor=start_sequence)
        if row.next_sequence < safe_next:
            row.next_sequence = safe_next
            row.save(update_fields=["next_sequence", "system_updated_at"])

        return JsonResponse(_row_to_dict(row), status=201)
    except IntegrityError:
        return JsonResponse({"error": "Prefix already exists"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["GET"])
@admin_auth()
def get_product_code_prefix(request, prefix_id):
    try:
        row = ProductCodePrefix.objects.get(pk=prefix_id)
        return JsonResponse(_row_to_dict(row), status=200)
    except ProductCodePrefix.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)


@api_view(["PUT", "PATCH"])
@admin_auth()
def update_product_code_prefix(request, prefix_id):
    """
  PUT /master/product-code-prefixes/<id>/update/
  Body: { "description", "is_active", "next_sequence" (optional, must be > last used) }
    """
    try:
        data = json.loads(request.body or "{}")
        row = ProductCodePrefix.objects.get(pk=prefix_id)
        admin = _admin_user(request)

        if "description" in data:
            row.description = (data.get("description") or "").strip()[:255]
        if "is_active" in data:
            row.is_active = bool(data["is_active"])
        if "next_sequence" in data:
            try:
                new_next = int(data["next_sequence"])
            except (TypeError, ValueError):
                return JsonResponse({"error": "next_sequence must be an integer"}, status=400)
            if new_next < 1:
                return JsonResponse({"error": "next_sequence must be >= 1"}, status=400)
            last_used = max_existing_tag_suffix(row.prefix)
            min_allowed = (last_used + 1) if last_used is not None else row.start_sequence
            if new_next < min_allowed:
                return JsonResponse(
                    {
                        "error": (
                            f"next_sequence cannot be below {min_allowed} "
                            f"(tags already exist up to {last_used})."
                        )
                    },
                    status=400,
                )
            row.next_sequence = new_next

        row.updated_by = admin
        row.save()
        return JsonResponse(_row_to_dict(row), status=200)
    except ProductCodePrefix.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def sync_product_code_prefixes(request):
    """
    POST /master/product-code-prefixes/sync/
    Seed rows from product_sku.product_code values and align counters with existing tags.
    """
    try:
        admin = _admin_user(request)
        created = seed_prefixes_from_catalog(admin_user=admin)
        total = ProductCodePrefix.objects.count()
        return JsonResponse({"created": created, "total": total}, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def ensure_product_code_prefix(request):
    """
    POST /master/product-code-prefixes/ensure/
    Body: { "prefix": "LR", "start_sequence": 1001 }
    Get or create a single prefix row (used when saving a product with a new code).
    """
    try:
        data = json.loads(request.body or "{}")
        prefix = normalize_prefix(data.get("prefix", ""))
        if not prefix:
            return JsonResponse({"error": "prefix is required"}, status=400)
        try:
            start = int(data.get("start_sequence", DEFAULT_TAG_SEQ_START))
        except (TypeError, ValueError):
            start = DEFAULT_TAG_SEQ_START
        category_id = data.get("category_id")
        subcategory_id = data.get("subcategory_id")
        try:
            category_id = int(category_id) if category_id not in (None, "") else None
        except (TypeError, ValueError):
            category_id = None
        try:
            subcategory_id = int(subcategory_id) if subcategory_id not in (None, "") else None
        except (TypeError, ValueError):
            subcategory_id = None
        row = ensure_prefix_row(
            prefix,
            start_sequence=start,
            admin_user=_admin_user(request),
            category_id=category_id,
            subcategory_id=subcategory_id,
        )
        return JsonResponse(_row_to_dict(row), status=200)
    except ValidationError as e:
        msg = e.messages[0] if getattr(e, "messages", None) else str(e)
        return JsonResponse({"error": msg}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@admin_auth()
def validate_product_code_prefix_mapping(request):
    """
    POST /master/product-code-prefixes/validate/
    Body: { "prefix", "category_id", "subcategory_id" }
    """
    try:
        data = json.loads(request.body or "{}")
        validate_product_code_mapping(
            data.get("prefix", ""),
            data.get("category_id"),
            data.get("subcategory_id"),
        )
        return JsonResponse({"valid": True}, status=200)
    except ValidationError as e:
        msg = e.messages[0] if getattr(e, "messages", None) else str(e)
        return JsonResponse({"valid": False, "error": msg}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
