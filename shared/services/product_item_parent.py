"""Parent / child variant linking on ProductItem.

Variants are child product_items (own SKU), not rows in a separate table.
Vendors use product_item_linked_vendors only.
"""
import re


def config_parent_item(item, *, ProductItem):
    if not item:
        return None
    if item.is_parent_product:
        return item
    parent_id = getattr(item, "parent_product_item_id", None)
    if not parent_id:
        return item
    parent = (
        item.parent_product_item
        if hasattr(item, "parent_product_item") and item.parent_product_item_id
        else None
    )
    if parent is None:
        parent = ProductItem.objects.filter(id=parent_id).first()
    return parent or item


def parse_variant_step(step_custom):
    if not isinstance(step_custom, dict):
        return False, None
    is_parent = bool(step_custom.get("is_parent_product"))
    raw_parent = step_custom.get("parent_product_item_id")
    parent_id = None
    if raw_parent not in (None, ""):
        try:
            parent_id = int(raw_parent)
        except (TypeError, ValueError):
            parent_id = None
    return is_parent, parent_id


def validate_variant_step(is_parent, parent_id):
    if is_parent and parent_id:
        return "A product cannot be both a parent and a variant of another parent."
    return None


def _clean_variant_option(row):
    if not isinstance(row, dict):
        return None
    purity = str(row.get("purity") or "").strip()
    color = str(row.get("color") or "").strip()
    purity_id = row.get("purity_id")
    color_id = row.get("color_id")
    vendor_id = row.get("vendor_id")
    child_product_item_id = row.get("child_product_item_id")
    try:
        purity_id = int(purity_id) if purity_id not in (None, "") else None
    except (TypeError, ValueError):
        purity_id = None
    try:
        color_id = int(color_id) if color_id not in (None, "") else None
    except (TypeError, ValueError):
        color_id = None
    try:
        vendor_id = int(vendor_id) if vendor_id not in (None, "") else None
    except (TypeError, ValueError):
        vendor_id = None
    try:
        child_product_item_id = (
            int(child_product_item_id) if child_product_item_id not in (None, "") else None
        )
    except (TypeError, ValueError):
        child_product_item_id = None
    if not purity and not color and not purity_id and not color_id:
        return None
    return {
        "id": str(row.get("id") or "").strip() or None,
        "purity": purity,
        "purity_id": purity_id,
        "color": color,
        "color_id": color_id,
        "vendor_id": vendor_id,
        "vendor_name": str(row.get("vendor_name") or "").strip(),
        "vendor_code": str(row.get("vendor_code") or "").strip(),
        "child_product_item_id": child_product_item_id,
    }


def normalize_variant_options(step_custom):
    if not isinstance(step_custom, dict):
        return []
    rows = []
    for row in step_custom.get("variant_options") or []:
        cleaned = _clean_variant_option(row)
        if cleaned:
            rows.append(cleaned)
    return rows


def _purity_label(purity_obj):
    if not purity_obj:
        return ""
    return (purity_obj.purity_name or getattr(purity_obj, "type", "") or "").strip()


def _id_or_none(val):
    if val in (None, ""):
        return None
    try:
        n = int(val)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


# Karat / hallmark tokens used as the purity segment in generateSkuPreview.
_PURITY_TOKEN_RE = re.compile(r"^(?:\d{1,2}KT?|PT\d{3}|925|999)$", re.IGNORECASE)


def _sku_purity_token(purity: str) -> str:
    return (purity or "").strip().replace(" ", "").upper()


def _sku_color_token(color: str) -> str:
    """Match frontend colorCode: Yellow→Y, Rose Gold→RG."""
    words = [w for w in (color or "").strip().split() if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0][0].upper()
    return "".join(w[0].upper() for w in words)[:4]


def _is_purity_sku_token(token: str) -> bool:
    return bool(token and _PURITY_TOKEN_RE.match(token.strip()))


def _child_sku_code(base_sku, option, index):
    """
    Build a distinct sku_code per purity/color child by replacing those
    segments in the parent SKU (not appending).

    Parent:  L-CL-G-22K-RG-W-WEIGHT-10X12-P-14.9-PCCDF
    Variant: L-CL-G-18K-Y-W-WEIGHT-10X12-P-14.9-PCCDF
    """
    purity_tok = _sku_purity_token(option.get("purity") or "")
    color_tok = _sku_color_token(option.get("color") or "")
    base = (base_sku or "").strip()
    if not base:
        bits = [f"SKU-V{index + 1}"]
        if purity_tok:
            bits.append(purity_tok)
        if color_tok:
            bits.append(color_tok)
        return "-".join(bits)[:255]

    parts = [p for p in base.split("-") if p != ""]
    purity_idxs = [i for i, p in enumerate(parts) if _is_purity_sku_token(p)]

    # Drop a previously appended trailing -18K-Y if the real slot is earlier.
    if len(purity_idxs) >= 2:
        last = purity_idxs[-1]
        if last >= len(parts) - 2:
            parts = parts[:last]
            purity_idxs = [i for i, p in enumerate(parts) if _is_purity_sku_token(p)]

    if purity_idxs and (purity_tok or color_tok):
        i = purity_idxs[-1]
        if purity_tok:
            parts[i] = purity_tok
        if color_tok:
            if i + 1 < len(parts):
                parts[i + 1] = color_tok
            else:
                parts.append(color_tok)
        return "-".join(parts)[:255]

    bits = [base]
    if purity_tok:
        bits.append(purity_tok)
    if color_tok:
        bits.append(color_tok)
    code = "-".join(bits)
    if code == base:
        code = f"{base}-V{index + 1}"
    return code[:255]


def materialize_variant_children(
    *,
    parent_item,
    step1,
    step_custom,
    step_vendor,
    product_group,
    admin_user,
    ProductItem,
    ProductBOM,
    ProductItemLinkedVendor,
    resolve_product_sku_by_code,
    build_sku_code_final,
    save_linked_vendors_on_item,
):
    """
    Persist wizard variant option cards as child product_items.

    Parent row: is_parent_product=True
    Each card: child ProductItem + SKU (color) + METAL BOM (purity) + optional vendor link
    linked via parent_product_item_id.
    """
    if not parent_item or not getattr(parent_item, "id", None):
        return []

    options = normalize_variant_options(step_custom)
    if not options:
        # Explicit empty matrix → remove previous children.
        ProductItem.objects.filter(parent_product_item_id=parent_item.id).delete()
        return []

    metal_id = _id_or_none(step1.get("metal_id"))
    if not metal_id:
        metal_ids = step1.get("metal_ids") or []
        metal_id = _id_or_none(metal_ids[0] if metal_ids else None)
    hsn_id = _id_or_none(step1.get("hsn_id"))
    default_color_id = _id_or_none(step1.get("color_id"))
    default_purity_id = _id_or_none(step1.get("purity_id"))
    if not default_purity_id:
        purity_ids = step1.get("purity_ids") or []
        default_purity_id = _id_or_none(purity_ids[0] if purity_ids else None)

    if not metal_id or not hsn_id:
        raise ValueError(
            "Cannot save variant options: metal and HSN from Basic Info are required."
        )

    base_product_code = (step1.get("product_code") or "").strip()
    pattern_code = (step1.get("pattern_code") or "").strip()
    style_code = (step1.get("style_code") or "")[:100] or None
    parent_sku_code = ""
    if getattr(parent_item, "sku_id", None) and parent_item.sku:
        parent_sku_code = (parent_item.sku.sku_code or "").strip()
    if not parent_sku_code:
        parent_sku_code = build_sku_code_final(
            step1, step_vendor, product_group_id=product_group.id
        )

    # Vendor entries from parent step — reuse product terms when assigning per-option vendor.
    from shared.services.product_item_vendors import normalize_vendor_entries

    vendor_by_id = {
        row["vendor_id"]: row for row in normalize_vendor_entries(step_vendor or {})
    }

    kept_ids = set()
    children = []
    skipped = []

    for index, option in enumerate(options):
        color_id = option.get("color_id") or default_color_id
        purity_id = option.get("purity_id") or default_purity_id
        if not color_id or not purity_id:
            label = option.get("purity") or option.get("color") or f"#{index + 1}"
            skipped.append(label)
            continue

        child_step1 = dict(step1)
        child_step1["color_id"] = color_id
        child_step1["color"] = option.get("color") or step1.get("color") or ""
        child_step1["purity_id"] = purity_id
        child_step1["purity"] = option.get("purity") or step1.get("purity") or ""
        sku_code_val = _child_sku_code(parent_sku_code, option, index)
        child_step1["sku_code"] = sku_code_val

        child_sku, _ = resolve_product_sku_by_code(
            sku_code_val=sku_code_val,
            product_group=product_group,
            color_id=color_id,
            hsn_id=hsn_id,
            base_product_code=base_product_code,
            pattern_code=pattern_code,
            style_code=style_code,
            admin_user=admin_user,
        )

        child = None
        existing_id = option.get("child_product_item_id")
        if existing_id:
            child = ProductItem.objects.filter(
                id=existing_id, parent_product_item_id=parent_item.id
            ).first()
        if child is None:
            child = (
                ProductItem.objects.filter(
                    parent_product_item_id=parent_item.id, sku_id=child_sku.id
                )
                .order_by("id")
                .first()
            )

        if child is None:
            child = ProductItem.objects.create(
                sku=child_sku,
                qty=0,
                store_variant_name=parent_item.store_variant_name or "",
                customer_variant_name=parent_item.customer_variant_name or "",
                net_weight=parent_item.net_weight,
                gross_weight=parent_item.gross_weight,
                charge_apply=parent_item.charge_apply,
                geometrical_shape_id=parent_item.geometrical_shape_id,
                size_number=parent_item.size_number,
                size_mm=parent_item.size_mm,
                height_mm=parent_item.height_mm,
                width_mm=parent_item.width_mm,
                is_parent_product=False,
                parent_product_item=parent_item,
                created_by=admin_user,
                updated_by=admin_user,
            )
        else:
            child.sku = child_sku
            child.is_parent_product = False
            child.parent_product_item = parent_item
            child.store_variant_name = parent_item.store_variant_name or child.store_variant_name
            child.customer_variant_name = (
                parent_item.customer_variant_name or child.customer_variant_name
            )
            child.net_weight = parent_item.net_weight
            child.gross_weight = parent_item.gross_weight
            child.charge_apply = parent_item.charge_apply
            child.geometrical_shape_id = parent_item.geometrical_shape_id
            child.size_number = parent_item.size_number
            child.size_mm = parent_item.size_mm
            child.height_mm = parent_item.height_mm
            child.width_mm = parent_item.width_mm
            child.updated_by = admin_user
            child.save()

        # Metal BOM carries the option purity (edit UI reads purity from here).
        # Also copy making-charge attributes from the parent METAL BOM — otherwise
        # catalogue pricing on child/barcode rows drops to ₹0 or stale per-gm values.
        ProductBOM.objects.filter(product=child, material_type="METAL").delete()
        child_bom = ProductBOM.objects.create(
            product=child,
            material_type="METAL",
            metal_id=metal_id,
            purity_id=purity_id,
            weight=parent_item.net_weight,
            quantity=1,
            created_by=admin_user,
            updated_by=admin_user,
        )
        from shared.models import ProductAttribute

        parent_metal_bom = (
            ProductBOM.objects.filter(product=parent_item, material_type="METAL")
            .prefetch_related("attributes")
            .order_by("id")
            .first()
        )
        if parent_metal_bom is not None:
            for attr in parent_metal_bom.attributes.all():
                ProductAttribute.objects.create(
                    product_bom=child_bom,
                    making_category_id=attr.making_category_id,
                    crafting_process_id=attr.crafting_process_id,
                    method_id=attr.method_id,
                    nature_id=attr.nature_id,
                    finishing_id=attr.finishing_id,
                    special_charge=attr.special_charge,
                    charge_type_id=attr.charge_type_id,
                    detail_number=attr.detail_number or 1,
                    created_by=admin_user,
                    updated_by=admin_user,
                )

        # Optional per-option vendor (else inherit all parent vendors).
        vendor_id = option.get("vendor_id")
        if vendor_id and vendor_id in vendor_by_id:
            save_linked_vendors_on_item(
                child,
                {"vendor_entries": [vendor_by_id[vendor_id]]},
                admin_user=admin_user,
                ProductItemLinkedVendor=ProductItemLinkedVendor,
            )
        elif vendor_id:
            save_linked_vendors_on_item(
                child,
                {
                    "vendor_entries": [
                        {
                            "vendor_id": vendor_id,
                            "vendor_name": option.get("vendor_name") or "",
                            "vendor_code": option.get("vendor_code") or "",
                        }
                    ]
                },
                admin_user=admin_user,
                ProductItemLinkedVendor=ProductItemLinkedVendor,
            )
        elif vendor_by_id:
            save_linked_vendors_on_item(
                child,
                {"vendor_entries": list(vendor_by_id.values())},
                admin_user=admin_user,
                ProductItemLinkedVendor=ProductItemLinkedVendor,
            )

        kept_ids.add(child.id)
        children.append(child)

    if options and not kept_ids:
        detail = ", ".join(str(s) for s in skipped[:5]) if skipped else "missing purity/color"
        raise ValueError(
            "Variant options were not saved. Each option needs purity and color "
            f"(and Basic Info needs metal + HSN). Incomplete: {detail}."
        )

    # Remove children that were deleted from the wizard matrix.
    orphans = ProductItem.objects.filter(parent_product_item_id=parent_item.id)
    if kept_ids:
        orphans = orphans.exclude(id__in=kept_ids)
        orphans.delete()

    return children


def variant_options_from_child_items(
    parent_item,
    *,
    ProductItem,
    ProductBOM,
    ProductItemLinkedVendor=None,
    VendorAddress=None,
):
    """Variant rows come from published child product_items only."""
    if not parent_item or not getattr(parent_item, "id", None):
        return []
    from shared.services.product_item_vendors import linked_vendors_for_item

    children = (
        ProductItem.objects.filter(parent_product_item_id=parent_item.id)
        .select_related("sku", "sku__color")
        .order_by("id")
    )
    rows = []
    for child in children:
        bom = (
            ProductBOM.objects.filter(product=child, material_type="METAL")
            .select_related("purity")
            .order_by("id")
            .first()
        )
        vendor_id = None
        vendor_name = ""
        vendor_code = ""
        if ProductItemLinkedVendor is not None:
            vendor_entries = linked_vendors_for_item(
                child,
                ProductItemLinkedVendor=ProductItemLinkedVendor,
                VendorAddress=VendorAddress,
            )
            if vendor_entries:
                vendor_id = vendor_entries[0].get("vendor_id")
                vendor_name = vendor_entries[0].get("vendor_name") or ""
                vendor_code = vendor_entries[0].get("vendor_code") or ""
        rows.append(
            {
                "id": f"child-{child.id}",
                "purity": _purity_label(bom.purity if bom else None),
                "purity_id": bom.purity_id if bom else None,
                "color": (child.sku.color.label if child.sku_id and child.sku.color else "") or "",
                "color_id": child.sku.color_id if child.sku_id else None,
                "vendor_id": vendor_id,
                "vendor_name": vendor_name,
                "vendor_code": vendor_code,
                "child_product_item_id": child.id,
            }
        )
    return rows


def apply_variant_flags_to_items(product_rows, *, is_parent, parent_id, ProductItem):
    if not product_rows:
        return
    if is_parent:
        anchor = product_rows[0]
        anchor.is_parent_product = True
        anchor.parent_product_item_id = None
        anchor.save(update_fields=["is_parent_product", "parent_product_item_id"])
        for extra in product_rows[1:]:
            if extra.is_parent_product or extra.parent_product_item_id:
                extra.is_parent_product = False
                extra.parent_product_item_id = None
                extra.save(update_fields=["is_parent_product", "parent_product_item_id"])
        return

    if not parent_id:
        for row in product_rows:
            if row.is_parent_product or row.parent_product_item_id:
                row.is_parent_product = False
                row.parent_product_item_id = None
                row.save(update_fields=["is_parent_product", "parent_product_item_id"])
        return

    parent = ProductItem.objects.filter(id=parent_id, is_parent_product=True).first()
    if not parent:
        raise ValueError(
            "Parent product not found. Choose a product marked as a parent (virtual style)."
        )

    for row in product_rows:
        if row.id and row.id == parent_id:
            raise ValueError("A product cannot be its own parent.")
        row.is_parent_product = False
        row.parent_product_item_id = parent.id
        row.save(update_fields=["is_parent_product", "parent_product_item_id"])


def _item_short_label(item):
    if not item:
        return ""
    sku = item.sku if getattr(item, "sku_id", None) else None
    name = (item.store_variant_name or "").strip()
    code = (sku.product_code or sku.sku_code or "") if sku else ""
    if name and code:
        return f"{name} ({code})"
    return name or code or f"Item #{item.id}"


def variant_step_payload_for_item(
    item,
    *,
    ProductItem,
    ProductBOM,
    ProductItemLinkedVendor=None,
    VendorAddress=None,
    step_custom=None,
):
    children = []
    if item.is_parent_product:
        child_qs = (
            ProductItem.objects.filter(parent_product_item_id=item.id)
            .select_related("sku")
            .order_by("-system_updated_at", "-id")[:50]
        )
        children = [
            {
                "id": c.id,
                "sku_code": (c.sku.sku_code or "") if c.sku_id else "",
                "product_code": (c.sku.product_code or "") if c.sku_id else "",
                "store_variant_name": c.store_variant_name or "",
            }
            for c in child_qs
        ]

    parent_label = ""
    if item.parent_product_item_id:
        parent = (
            item.parent_product_item
            if getattr(item, "parent_product_item_id", None)
            and hasattr(item, "parent_product_item")
            else ProductItem.objects.filter(id=item.parent_product_item_id).first()
        )
        parent_label = _item_short_label(parent)

    parent_for_config = config_parent_item(item, ProductItem=ProductItem)
    variant_options = variant_options_from_child_items(
        parent_for_config,
        ProductItem=ProductItem,
        ProductBOM=ProductBOM,
        ProductItemLinkedVendor=ProductItemLinkedVendor,
        VendorAddress=VendorAddress,
    )
    # Draft-only fallback (before publish) — not a DB table.
    if not variant_options and step_custom:
        variant_options = normalize_variant_options(step_custom)

    return {
        "is_parent_product": bool(item.is_parent_product),
        "parent_product_item_id": item.parent_product_item_id,
        "parent_product_label": parent_label,
        "child_variants": children,
        "variant_options": variant_options,
    }
