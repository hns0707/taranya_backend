from django.db.models import Count
from django.utils.text import slugify
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from master.permissions.permission_checker import admin_auth, ensure_admin_permission
from master.permissions.section_auth import (
    MASTERS_CATEGORIES_DELETE_AUTH,
    MASTERS_CATEGORIES_READ_AUTH,
    MASTERS_CATEGORIES_WRITE_AUTH,
)
from shared.models import Category, Subcategory


def _category_to_dict(cat: Category, include_counts: bool = False) -> dict:
    data = {
        "id": cat.id,
        "name": cat.name,
        "slug": cat.slug,
        "description": cat.description or "",
        "sort_order": cat.sort_order,
        "is_active": cat.is_active,
        "system_created_at": cat.system_created_at,
        "system_updated_at": cat.system_updated_at,
    }
    if include_counts and hasattr(cat, "subcategories_count"):
        data["subcategories_count"] = cat.subcategories_count
    return data


@api_view(["GET", "POST"])
@admin_auth()
def category_list_create(request):
    """
    GET: List categories (optionally all, including inactive, with ?all=true).
    POST: Create a new category.
    """
    if request.method == "GET":
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_READ_AUTH)
        if denied:
            return denied
    else:
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_WRITE_AUTH)
        if denied:
            return denied

    if request.method == "GET":
        all_param = request.query_params.get("all", "false").strip().lower() in ("true", "1", "yes")
        qs = Category.objects.all().order_by("sort_order", "name").annotate(
            subcategories_count=Count("subcategories")
        )
        if not all_param:
            qs = qs.filter(is_active=True)
        return Response([_category_to_dict(cat, include_counts=True) for cat in qs])

    # POST – create
    name = (request.data.get("name") or "").strip()
    slug = (request.data.get("slug") or "").strip()
    description = (request.data.get("description") or "").strip() or None
    sort_order = request.data.get("sort_order", 0)
    is_active = request.data.get("is_active", True)

    if not name:
        return Response({"error": "Category name is required."}, status=status.HTTP_400_BAD_REQUEST)

    if Category.objects.filter(name__iexact=name).exists():
        return Response({"error": "A category with this name already exists."}, status=status.HTTP_400_BAD_REQUEST)

    if not slug:
        slug = slugify(name)
    if Category.objects.filter(slug__iexact=slug).exists():
        return Response({"error": "Slug must be unique."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        sort_order_int = int(sort_order)
    except (TypeError, ValueError):
        sort_order_int = 0

    cat = Category.objects.create(
        name=name,
        slug=slug,
        description=description,
        sort_order=sort_order_int,
        is_active=bool(is_active),
        created_by=getattr(request, "admin_user", None),
    )
    return Response(_category_to_dict(cat), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@admin_auth()
def category_detail(request, category_id: int):
    """
    GET: Retrieve one category.
    PUT/PATCH: Update category.
    DELETE: Soft-delete (mark inactive) if no subcategories exist.
    """
    try:
        cat = Category.objects.get(id=category_id)
    except Category.DoesNotExist:
        return Response({"error": "Category not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_READ_AUTH)
        if denied:
            return denied
        return Response(_category_to_dict(cat))

    if request.method in ("PUT", "PATCH"):
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_WRITE_AUTH)
        if denied:
            return denied
        name = (request.data.get("name") or cat.name).strip()
        slug = (request.data.get("slug") or cat.slug).strip()
        description = (request.data.get("description") or cat.description or "").strip() or None
        sort_order = request.data.get("sort_order", cat.sort_order)
        is_active = request.data.get("is_active", cat.is_active)

        if not name:
            return Response({"error": "Category name is required."}, status=status.HTTP_400_BAD_REQUEST)

        if Category.objects.filter(name__iexact=name).exclude(id=cat.id).exists():
            return Response({"error": "A category with this name already exists."}, status=status.HTTP_400_BAD_REQUEST)

        if not slug:
            slug = slugify(name)
        if Category.objects.filter(slug__iexact=slug).exclude(id=cat.id).exists():
            return Response({"error": "Slug must be unique."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            sort_order_int = int(sort_order)
        except (TypeError, ValueError):
            sort_order_int = 0

        cat.name = name
        cat.slug = slug
        cat.description = description
        cat.sort_order = sort_order_int
        cat.is_active = bool(is_active)
        cat.updated_by = getattr(request, "admin_user", None)
        cat.save()
        return Response(_category_to_dict(cat))

    denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_DELETE_AUTH)
    if denied:
        return denied
    # DELETE – soft delete, only if no subcategories
    has_subcategories = Subcategory.objects.filter(category=cat).exists()
    if has_subcategories:
        return Response(
            {"error": "Category cannot be deleted while it has subcategories."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    cat.is_active = False
    cat.updated_by = getattr(request, "admin_user", None)
    cat.save()
    return Response({"message": "Category deactivated."})


def _subcategory_to_dict(sub: Subcategory) -> dict:
    return {
        "id": sub.id,
        "category_id": sub.category_id,
        "name": sub.name,
        "slug": sub.slug,
        "description": sub.description or "",
        "sort_order": sub.sort_order,
        "is_active": sub.is_active,
        "system_created_at": sub.system_created_at,
        "system_updated_at": sub.system_updated_at,
    }


@api_view(["GET", "POST"])
@admin_auth()
def subcategory_list_create(request, category_id: int):
    """
    GET: List subcategories for a category (optionally all with ?all=true).
    POST: Create a subcategory under the category.
    """
    try:
        category = Category.objects.get(id=category_id)
    except Category.DoesNotExist:
        return Response({"error": "Category not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_READ_AUTH)
        if denied:
            return denied
    else:
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_WRITE_AUTH)
        if denied:
            return denied

    if request.method == "GET":
        all_param = request.query_params.get("all", "false").strip().lower() in ("true", "1", "yes")
        qs = Subcategory.objects.filter(category=category).order_by("sort_order", "name")
        if not all_param:
            qs = qs.filter(is_active=True)
        return Response([_subcategory_to_dict(sub) for sub in qs])

    # POST – create
    name = (request.data.get("name") or "").strip()
    slug = (request.data.get("slug") or "").strip()
    description = (request.data.get("description") or "").strip() or None
    sort_order = request.data.get("sort_order", 0)
    is_active = request.data.get("is_active", True)

    if not name:
        return Response({"error": "Subcategory name is required."}, status=status.HTTP_400_BAD_REQUEST)

    if not slug:
        slug = slugify(name)

    if Subcategory.objects.filter(category=category, name__iexact=name).exists():
        return Response(
            {"error": "A subcategory with this name already exists in this category."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if Subcategory.objects.filter(category=category, slug__iexact=slug).exists():
        return Response(
            {"error": "Slug must be unique within this category."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        sort_order_int = int(sort_order)
    except (TypeError, ValueError):
        sort_order_int = 0

    sub = Subcategory.objects.create(
        category=category,
        name=name,
        slug=slug,
        description=description,
        sort_order=sort_order_int,
        is_active=bool(is_active),
        created_by=getattr(request, "admin_user", None),
    )
    return Response(_subcategory_to_dict(sub), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@admin_auth()
def subcategory_detail(request, category_id: int, subcategory_id: int):
    """
    GET: Retrieve a subcategory.
    PUT/PATCH: Update subcategory.
    DELETE: Soft delete (mark inactive).
    """
    try:
        category = Category.objects.get(id=category_id)
    except Category.DoesNotExist:
        return Response({"error": "Category not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        sub = Subcategory.objects.get(id=subcategory_id, category=category)
    except Subcategory.DoesNotExist:
        return Response({"error": "Subcategory not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_READ_AUTH)
        if denied:
            return denied
        return Response(_subcategory_to_dict(sub))

    if request.method in ("PUT", "PATCH"):
        denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_WRITE_AUTH)
        if denied:
            return denied
        name = (request.data.get("name") or sub.name).strip()
        slug = (request.data.get("slug") or sub.slug).strip()
        description = (request.data.get("description") or sub.description or "").strip() or None
        sort_order = request.data.get("sort_order", sub.sort_order)
        is_active = request.data.get("is_active", sub.is_active)

        if not name:
            return Response({"error": "Subcategory name is required."}, status=status.HTTP_400_BAD_REQUEST)

        if Subcategory.objects.filter(category=category, name__iexact=name).exclude(id=sub.id).exists():
            return Response(
                {"error": "A subcategory with this name already exists in this category."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not slug:
            slug = slugify(name)
        if Subcategory.objects.filter(category=category, slug__iexact=slug).exclude(id=sub.id).exists():
            return Response(
                {"error": "Slug must be unique within this category."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            sort_order_int = int(sort_order)
        except (TypeError, ValueError):
            sort_order_int = 0

        sub.name = name
        sub.slug = slug
        sub.description = description
        sub.sort_order = sort_order_int
        sub.is_active = bool(is_active)
        sub.updated_by = getattr(request, "admin_user", None)
        sub.save()
        return Response(_subcategory_to_dict(sub))

    denied = ensure_admin_permission(request, *MASTERS_CATEGORIES_DELETE_AUTH)
    if denied:
        return denied
    # DELETE – soft delete
    sub.is_active = False
    sub.updated_by = getattr(request, "admin_user", None)
    sub.save()
    return Response({"message": "Subcategory deactivated."})

