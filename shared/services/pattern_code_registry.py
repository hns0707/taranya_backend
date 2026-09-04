"""Pattern code registry scoped to item type (subcategory) + store variant 1:1."""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from shared.models import Category, PatternCodeRegistry, ProductItem, ProductSKU, Subcategory


def normalize_pattern_code(raw: str) -> str:
    return (raw or "").strip().upper().replace(" ", "")


def normalize_store_variant_name(raw: str) -> str:
    return (raw or "").strip()[:255]


def _parse_optional_id(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _row_to_dict(row: PatternCodeRegistry) -> dict:
    return {
        "id": row.id,
        "pattern_code": row.pattern_code,
        "store_variant_name": row.store_variant_name or "",
        "description": row.description or "",
        "is_active": row.is_active,
        "category_id": row.category_id,
        "subcategory_id": row.subcategory_id,
        "category_name": row.category.name if row.category_id and row.category else "",
        "subcategory_name": (
            row.subcategory.name if row.subcategory_id and row.subcategory else ""
        ),
    }


def _resolve_category_subcategory(
    category_id=None,
    subcategory_id=None,
) -> tuple[Category | None, Subcategory | None]:
    cat_id = _parse_optional_id(category_id)
    sub_id = _parse_optional_id(subcategory_id)
    category = Category.objects.filter(pk=cat_id).first() if cat_id else None
    subcategory = Subcategory.objects.filter(pk=sub_id).first() if sub_id else None
    if subcategory and category and subcategory.category_id != category.id:
        raise ValidationError("Item type does not belong to the selected item group.")
    if subcategory and not category and subcategory.category_id:
        category = subcategory.category
    return category, subcategory


def validate_pattern_item_type_mapping(
    pattern_code: str,
    *,
    category_id=None,
    subcategory_id=None,
) -> None:
    """Ensure pattern code belongs to the given item type (when registry is scoped)."""
    code = normalize_pattern_code(pattern_code)
    if not code:
        raise ValidationError("Pattern code is required.")
    sub_id = _parse_optional_id(subcategory_id)
    if not sub_id:
        raise ValidationError("Select item group and item type before choosing a pattern code.")

    row = PatternCodeRegistry.objects.filter(pattern_code=code).first()
    if not row:
        return
    if row.subcategory_id and int(row.subcategory_id) != int(sub_id):
        item_type = row.subcategory.name if row.subcategory_id and row.subcategory else "another item type"
        raise ValidationError(
            f'Pattern code "{code}" belongs to item type "{item_type}" and cannot be used here.'
        )
    cat_id = _parse_optional_id(category_id)
    if cat_id and row.category_id and int(row.category_id) != int(cat_id):
        raise ValidationError(
            f'Pattern code "{code}" belongs to another item group and cannot be used here.'
        )


def validate_pattern_store_mapping(
    pattern_code: str,
    store_variant_name: str,
    *,
    allow_empty_store_name: bool = False,
    category_id=None,
    subcategory_id=None,
) -> None:
    """Raise ValidationError when mapping breaks 1:1 or item-type ownership rules."""
    code = normalize_pattern_code(pattern_code)
    name = normalize_store_variant_name(store_variant_name)
    if not code:
        raise ValidationError("Pattern code is required.")
    if not name and not allow_empty_store_name:
        raise ValidationError("Store variant name is required.")

    if subcategory_id not in (None, ""):
        validate_pattern_item_type_mapping(
            code,
            category_id=category_id,
            subcategory_id=subcategory_id,
        )

    row = PatternCodeRegistry.objects.filter(pattern_code=code).first()
    if row and row.store_variant_name and name and row.store_variant_name != name:
        raise ValidationError(
            f'Pattern code "{code}" is already mapped to store variant name '
            f'"{row.store_variant_name}".'
        )

    if name:
        conflict = (
            PatternCodeRegistry.objects.filter(store_variant_name=name)
            .exclude(pattern_code=code)
            .first()
        )
        if conflict:
            raise ValidationError(
                f'Store variant name "{name}" is already mapped to pattern code '
                f'"{conflict.pattern_code}".'
            )


@transaction.atomic
def ensure_pattern_code_row(
    pattern_code: str,
    *,
    store_variant_name: str | None = None,
    description: str = "",
    category_id=None,
    subcategory_id=None,
    admin_user=None,
) -> PatternCodeRegistry:
    """Get or create a pattern code row scoped to item type."""
    code = normalize_pattern_code(pattern_code)
    if not code:
        raise ValueError("Pattern code is required.")

    category, subcategory = _resolve_category_subcategory(category_id, subcategory_id)
    if not subcategory:
        raise ValidationError("Item type (subcategory) is required to register a pattern code.")

    name = normalize_store_variant_name(store_variant_name or "")
    if name:
        validate_pattern_store_mapping(
            code,
            name,
            allow_empty_store_name=True,
            category_id=category.id if category else None,
            subcategory_id=subcategory.id,
        )
    else:
        validate_pattern_item_type_mapping(
            code,
            category_id=category.id if category else None,
            subcategory_id=subcategory.id,
        )

    row, created = PatternCodeRegistry.objects.select_for_update().get_or_create(
        pattern_code=code,
        defaults={
            "category": category,
            "subcategory": subcategory,
            "store_variant_name": name,
            "description": (description or "").strip()[:255],
            "is_active": True,
            "created_by": admin_user,
            "updated_by": admin_user,
        },
    )
    if not created:
        if row.subcategory_id and int(row.subcategory_id) != int(subcategory.id):
            item_type = (
                row.subcategory.name if row.subcategory_id and row.subcategory else "another item type"
            )
            raise ValidationError(
                f'Pattern code "{code}" already exists for item type "{item_type}".'
            )
        dirty = False
        if not row.subcategory_id:
            row.subcategory = subcategory
            dirty = True
        if not row.category_id and category:
            row.category = category
            dirty = True
        if name and not row.store_variant_name:
            row.store_variant_name = name
            dirty = True
        elif name and row.store_variant_name and row.store_variant_name != name:
            validate_pattern_store_mapping(
                code,
                name,
                category_id=category.id if category else None,
                subcategory_id=subcategory.id,
            )
        desc = (description or "").strip()[:255]
        if desc and row.description != desc:
            row.description = desc
            dirty = True
        if dirty:
            row.updated_by = admin_user
            row.save(
                update_fields=[
                    "category",
                    "subcategory",
                    "store_variant_name",
                    "description",
                    "updated_by",
                    "system_updated_at",
                ]
            )
    return row


@transaction.atomic
def bind_pattern_store_variant(
    pattern_code: str,
    store_variant_name: str,
    *,
    category_id=None,
    subcategory_id=None,
    admin_user=None,
) -> PatternCodeRegistry:
    """Persist 1:1 mapping when saving wizard step 1."""
    code = normalize_pattern_code(pattern_code)
    name = normalize_store_variant_name(store_variant_name)
    validate_pattern_store_mapping(
        code,
        name,
        category_id=category_id,
        subcategory_id=subcategory_id,
    )
    return ensure_pattern_code_row(
        code,
        store_variant_name=name,
        category_id=category_id,
        subcategory_id=subcategory_id,
        admin_user=admin_user,
    )


def seed_pattern_codes_from_catalog(*, admin_user=None) -> int:
    """Import distinct SKU pattern codes with parent store variant + item type."""
    created = 0
    skus = (
        ProductSKU.objects.exclude(pattern_code="")
        .exclude(pattern_code__isnull=True)
        .select_related("product_group", "product_group__category", "product_group__subcategory")
    )
    seen = set()
    for sku in skus:
        code = normalize_pattern_code(sku.pattern_code)
        if not code or code in seen:
            continue
        seen.add(code)
        if PatternCodeRegistry.objects.filter(pattern_code=code).exists():
            continue
        item = (
            ProductItem.objects.filter(sku_id=sku.id)
            .exclude(store_variant_name="")
            .order_by("-is_parent_product", "id")
            .first()
        )
        store_name = (item.store_variant_name if item else "").strip()
        group = sku.product_group if sku.product_group_id else None
        PatternCodeRegistry.objects.create(
            pattern_code=code,
            store_variant_name=store_name[:255],
            category_id=group.category_id if group else None,
            subcategory_id=group.subcategory_id if group else None,
            is_active=True,
            created_by=admin_user,
            updated_by=admin_user,
        )
        created += 1
    return created
