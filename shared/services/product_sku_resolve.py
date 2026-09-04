"""
Resolve ProductSKU by sku_code string: reuse existing row or create a new one.
Used on publish (new product) and product item update so 22K/18K codes stay as separate SKUs.
"""
from django.db import IntegrityError

from shared.models import HSNMaster, LookupValue, ProductSKU


def build_sku_code_final(step1, step3=None, product_group_id=None, fallback_sku_code=None):
    """
    Use the wizard-computed sku_code when present (built on the client with the
    canonical segment order). Fall back only when legacy drafts omit sku_code.
    """
    step3 = step3 or {}
    sku_code_raw = (step1.get("sku_code") or step3.get("sku_code") or "").strip()
    if not sku_code_raw and fallback_sku_code:
        sku_code_raw = (fallback_sku_code or "").strip()
    if sku_code_raw:
        return sku_code_raw
    base_product_code = (step1.get("product_code") or "").strip()
    if base_product_code:
        return base_product_code
    if product_group_id is not None:
        return f"SKU{product_group_id}"
    return ""


def resolve_product_sku_by_code(
    *,
    sku_code_val,
    product_group,
    color_id,
    hsn_id,
    base_product_code=None,
    pattern_code=None,
    style_code=None,
    admin_user,
):
    """
    Find SKU by full sku_code; if missing, create a new row.
    Never overwrites sku_code on an existing row found via group+color only.

    Multiple SKUs may share the same product_code and pattern_code; only sku_code
    must be unique.

    Returns (sku, created).
    """
    sku_code_val = (sku_code_val or "").strip()
    if not sku_code_val:
        sku_code_val = f"SKU{product_group.id}"

    product_code = (base_product_code or "").strip()[:100] or None
    pattern_code_val = (pattern_code or "").strip()[:64] or ""

    existing = ProductSKU.objects.filter(sku_code=sku_code_val).first()
    if existing:
        sku_dirty = False
        update_fields = ["updated_by", "system_updated_at"]
        style_code = (style_code or "")[:100] or None
        if style_code is not None and (existing.style_code or "") != (style_code or ""):
            existing.style_code = style_code
            sku_dirty = True
            update_fields.append("style_code")
        if hsn_id and existing.hsn_id != hsn_id:
            existing.hsn = HSNMaster.objects.get(id=hsn_id)
            sku_dirty = True
            update_fields.append("hsn")
        if product_code and (existing.product_code or "") != product_code:
            existing.product_code = product_code
            sku_dirty = True
            update_fields.append("product_code")
        if pattern_code_val and (existing.pattern_code or "") != pattern_code_val:
            existing.pattern_code = pattern_code_val
            sku_dirty = True
            update_fields.append("pattern_code")
        if sku_dirty:
            existing.updated_by = admin_user
            existing.save(update_fields=update_fields)
        return existing, False

    color = LookupValue.objects.get(id=color_id)
    hsn = HSNMaster.objects.get(id=hsn_id)
    style_code = (style_code or "")[:100] or None

    try:
        sku = ProductSKU.objects.create(
            product_group=product_group,
            color=color,
            hsn=hsn,
            product_code=product_code,
            pattern_code=pattern_code_val,
            sku_code=sku_code_val,
            style_code=style_code,
            created_by=admin_user,
            updated_by=admin_user,
        )
    except IntegrityError as exc:
        raise ValueError(
            _integrity_error_message(
                exc,
                product_group=product_group,
                color_id=color_id,
                sku_code_val=sku_code_val,
            )
        ) from exc
    return sku, True


def _integrity_error_message(exc, *, product_group, color_id, sku_code_val):
    """Turn MySQL duplicate-key errors into an actionable publish message."""
    msg = str(exc)
    if "uniq_group_metal_purity_color" in msg or "unique_product_group" in msg:
        return (
            "A SKU already exists for this style group and colour "
            f"(product_group_id={product_group.id}, color_id={color_id}). "
            "Your database still has a legacy unique index on product_skus "
            "(product_group + metal + purity + colour) that blocks multiple SKU codes "
            "under the same group. Run migration shared.0017 or drop index "
            "uniq_group_metal_purity_color, then retry. "
            f"Attempted sku_code: {sku_code_val!r}."
        )
    if "uniq_product_sku_sku_code" in msg or "sku_code" in msg.lower():
        return (
            f"SKU code {sku_code_val!r} is already used by another product. "
            "Change metal, purity, pattern, or other SKU segments and try again."
        )
    return msg
