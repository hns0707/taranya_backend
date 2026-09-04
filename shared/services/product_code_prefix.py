"""
Atomic allocation of barcode/tag values per product-code prefix.

Each prefix (LR, NP, PRG, …) maintains its own counter so tag values stay
unique and sequential: LR-1001, LR-1002, NP-1001, …

One item group + item type ↔ one product code (prefix); enforced in
`product_code_prefixes` and `validate_product_code_mapping`.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from shared.models import ProductCodePrefix, ProductTag, ProductSKU

DEFAULT_TAG_SEQ_START = 1001


def normalize_prefix(raw: str) -> str:
    return (raw or "").strip().upper()


def _parse_group_ids(category_id, subcategory_id):
    try:
        cat_id = int(category_id) if category_id not in (None, "") else None
    except (TypeError, ValueError):
        cat_id = None
    try:
        sub_id = int(subcategory_id) if subcategory_id not in (None, "") else None
    except (TypeError, ValueError):
        sub_id = None
    return cat_id, sub_id


def resolve_prefix_for_group(category_id, subcategory_id) -> str | None:
    """Registered product-code prefix for item group + type, if any."""
    cat_id, sub_id = _parse_group_ids(category_id, subcategory_id)
    if not cat_id or not sub_id:
        return None
    row = (
        ProductCodePrefix.objects.filter(
            category_id=cat_id,
            subcategory_id=sub_id,
            is_active=True,
        )
        .order_by("id")
        .first()
    )
    if not row or not (row.prefix or "").strip():
        return None
    return normalize_prefix(row.prefix)


def validate_product_code_mapping(
    prefix: str,
    category_id,
    subcategory_id,
    *,
    allow_missing_group: bool = False,
) -> None:
    """Raise ValidationError when product code breaks group/type 1:1 rules."""
    key = normalize_prefix(prefix)
    if not key:
        raise ValidationError("Product code is required.")

    cat_id, sub_id = _parse_group_ids(category_id, subcategory_id)
    if not allow_missing_group and (not cat_id or not sub_id):
        raise ValidationError("Item group and item type are required.")
    if not cat_id or not sub_id:
        return

    row = ProductCodePrefix.objects.filter(prefix=key).first()
    if row and row.category_id and row.subcategory_id:
        if row.category_id != cat_id or row.subcategory_id != sub_id:
            raise ValidationError(
                f'Product code "{key}" is already assigned to another item group and type.'
            )

    other = (
        ProductCodePrefix.objects.filter(category_id=cat_id, subcategory_id=sub_id)
        .exclude(prefix=key)
        .first()
    )
    if other:
        raise ValidationError(
            f'This item group and type already uses product code "{other.prefix}". '
            f"Only one product code is allowed per combination."
        )


def max_existing_tag_suffix(prefix: str) -> int | None:
    """Highest numeric suffix already used in product_tags for this prefix."""
    key = normalize_prefix(prefix)
    if not key:
        return None
    needle = f"{key}-"
    max_seq: int | None = None
    for tv in ProductTag.objects.filter(tag_value__startswith=needle).values_list(
        "tag_value", flat=True
    ):
        suffix = str(tv)[len(needle) :]
        if "~SOLD" in suffix:
            suffix = suffix.split("~SOLD", 1)[0]
        if suffix.isdigit():
            n = int(suffix)
            max_seq = n if max_seq is None else max(max_seq, n)
    return max_seq


def bootstrap_next_sequence(prefix: str, *, floor: int = DEFAULT_TAG_SEQ_START) -> int:
    """
    Next safe sequence: max(existing tags) + 1, but never below `floor`.
    """
    existing_max = max_existing_tag_suffix(prefix)
    if existing_max is not None:
        return max(existing_max + 1, floor)
    return floor


@transaction.atomic
def ensure_prefix_row(
    prefix: str,
    *,
    start_sequence: int = DEFAULT_TAG_SEQ_START,
    admin_user=None,
    category_id=None,
    subcategory_id=None,
) -> ProductCodePrefix:
    """
    Get or create the counter row for a prefix, syncing forward if tags
    already exist (legacy data or manual DB edits).
    """
    key = normalize_prefix(prefix)
    if not key:
        raise ValueError("Product code prefix is required.")

    cat_id, sub_id = _parse_group_ids(category_id, subcategory_id)
    if cat_id and sub_id:
        validate_product_code_mapping(key, cat_id, sub_id, allow_missing_group=True)

    start = max(int(start_sequence), 1)
    row, created = ProductCodePrefix.objects.select_for_update().get_or_create(
        prefix=key,
        defaults={
            "start_sequence": start,
            "next_sequence": start,
            "category_id": cat_id,
            "subcategory_id": sub_id,
            "created_by": admin_user,
            "updated_by": admin_user,
        },
    )
    dirty = False
    if cat_id and sub_id:
        if created:
            validate_product_code_mapping(key, cat_id, sub_id)
        elif not row.category_id or not row.subcategory_id:
            validate_product_code_mapping(key, cat_id, sub_id)
            if not row.category_id:
                row.category_id = cat_id
                dirty = True
            if not row.subcategory_id:
                row.subcategory_id = sub_id
                dirty = True
        else:
            validate_product_code_mapping(key, cat_id, sub_id)
    safe_next = bootstrap_next_sequence(key, floor=row.start_sequence)
    if row.next_sequence < safe_next:
        row.next_sequence = safe_next
        dirty = True
    if dirty:
        row.updated_by = admin_user
        row.save(
            update_fields=[
                "category_id",
                "subcategory_id",
                "next_sequence",
                "updated_by",
                "system_updated_at",
            ]
        )
    elif created:
        row.save()
    return row


@transaction.atomic
def allocate_tag_values(
    prefix: str,
    quantity: int,
    *,
    admin_user=None,
) -> list[str]:
    """
    Reserve `quantity` unique tag values for `prefix` atomically.
    Returns strings like ['LR-1001', 'LR-1002'].
    """
    if quantity < 1:
        return []

    row = ensure_prefix_row(prefix, admin_user=admin_user)
    row = ProductCodePrefix.objects.select_for_update().get(pk=row.pk)

    values: list[str] = []
    for _ in range(quantity):
        values.append(f"{row.prefix}-{row.next_sequence}")
        row.next_sequence += 1

    row.updated_by = admin_user
    row.save(update_fields=["next_sequence", "updated_by", "system_updated_at"])
    return values


def seed_prefixes_from_catalog(*, admin_user=None) -> int:
    """
    Create prefix rows for every distinct product_sku.product_code and align
    next_sequence with existing tags. Returns number of rows created.
    """
    codes = (
        ProductSKU.objects.exclude(product_code__isnull=True)
        .exclude(product_code="")
        .values_list("product_code", flat=True)
        .distinct()
    )
    created = 0
    for code in codes:
        key = normalize_prefix(code)
        if not key:
            continue
        before = ProductCodePrefix.objects.filter(prefix=key).exists()
        ensure_prefix_row(key, admin_user=admin_user)
        if not before:
            created += 1
    return created
