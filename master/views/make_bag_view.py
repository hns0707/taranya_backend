"""
Make Bag — sidebar lots + ProductItem search + save bag mapped to existing SKU lines.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from master.views.product_views import get_admin_user_from_request
from master.views.grn_lot_view import _G_WT_QUANT, _quantize_g_wt, lot_to_dict
from shared.models import (
    HSNMaster,
    LookupValue,
    MetalMasterRule,
    ProductAttribute,
    ProductBOM,
    ProductGroup,
    ProductItem,
    ProductItemLinkedVendor,
    ProductOperationCharge,
    ProductSKU,
    GrnBag,
    GrnLot,
    StockTransaction,
    Vendor,
)
from shared.services.product_item_vendors import (
    filter_items_by_vendor_name,
    filter_items_by_vendor_variant_name,
    primary_vendor_id_for_item,
    resolve_vendor_id_by_name,
    vendor_variant_name_for_item,
)
from shared.product_item_size import (
    SIZE_HW,
    SIZE_MM,
    SIZE_NUMBER,
    apply_create_payload_size_fields,
    format_size_kwargs_display,
    infer_item_size_type,
    item_filter_for_size,
    merge_size_distribution_rows,
    product_item_search_q,
    serialize_product_item_size_for_api,
    slug_from_size_display,
)
from shared.services.stock_service import adjust_product_item_qty
from shared.services.product_code_prefix import resolve_prefix_for_group


def _resolved_lot_product_code(lot) -> str:
    """Lot row product_code, or prefix registered for its item group + type."""
    stored = (getattr(lot, "product_code", "") or "").strip()
    if stored:
        return stored
    resolved = resolve_prefix_for_group(
        getattr(lot, "category_id", None),
        getattr(lot, "subcategory_id", None),
    )
    return resolved or ""


def _resolved_catalog_product_code(params: dict) -> str:
    explicit = (params.get("product_code") or "").strip()
    if explicit:
        return explicit
    return resolve_prefix_for_group(
        params.get("category_id"),
        params.get("subcategory_id"),
    ) or ""


def _dec_item_weight(v):
    if v is None:
        return ""
    s = format(v, "f").rstrip("0").rstrip(".")
    return s if s else "0"


_BAG_NO_PREFIX = "BAG-"
_BAG_NO_PAD = 5


def _max_bag_sequence() -> int:
    """Highest numeric suffix in bag_no values like BAG-00001 or BAG-00001-Size_12."""
    import re

    max_n = 0
    pattern = re.compile(r"^BAG-(\d+)", re.IGNORECASE)
    qs = GrnBag.objects.filter(bag_no__istartswith=_BAG_NO_PREFIX).values_list(
        "bag_no", flat=True
    )
    for s in qs:
        m = pattern.match((s or "").strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def _format_bag_no(n: int) -> str:
    return f"{_BAG_NO_PREFIX}{n:0{_BAG_NO_PAD}d}"


def peek_next_bag_no() -> str:
    """Next global BAG-NNNNN (not yet reserved)."""
    return _format_bag_no(_max_bag_sequence() + 1)


def _allocate_unique_bag_no(lot, attempt_seed: str = "") -> str:
    """
    Return a bag_no unique within this lot. Empty seed → next global BAG-NNNNN
    not already used as an exact bag_no anywhere.
    """
    seed = (attempt_seed or "").strip()
    if seed:
        if not GrnBag.objects.filter(lot=lot, bag_no=seed).exists():
            return seed
    start = _max_bag_sequence() + 1
    for offset in range(100):
        candidate = _format_bag_no(start + offset)
        if not GrnBag.objects.filter(bag_no=candidate).exists():
            return candidate
    return f"{_format_bag_no(start)}-{int(timezone.now().timestamp() * 1000) % 100000}"


def _stone_master_spec_line(stone):
    """Readable hints from expanded Stone master (catalog / Make Bag)."""
    if not stone:
        return ""
    parts = []
    if stone.stone_size is not None:
        sz = format(stone.stone_size, "f").rstrip("0").rstrip(".")
        u = ""
        if getattr(stone, "size_unit_id", None) and getattr(stone, "size_unit", None):
            u = (stone.size_unit.label or "").strip()
        parts.append(f"{sz} {u}".strip() if u else sz)
    if getattr(stone, "stone_group_id", None) and getattr(stone, "stone_group", None):
        g = (stone.stone_group.label or "").strip()
        if g:
            parts.append(g)
    if getattr(stone, "clarity_id", None) and getattr(stone, "clarity", None):
        c = (stone.clarity.label or "").strip()
        if c:
            parts.append(c)
    if getattr(stone, "cut_id", None) and getattr(stone, "cut", None):
        c = (stone.cut.label or "").strip()
        if c:
            parts.append(c)
    if stone.default_rate is not None:
        dr = format(stone.default_rate, "f").rstrip("0").rstrip(".")
        if dr:
            parts.append(f"Rate {dr}")
    return " · ".join(parts)


def _serialize_bom_charge_lines(bom_line):
    """
    ProductAttribute rows on this ProductBOM: charge_type (LookupValue label) + charge value (special_charge).
    """
    attrs = list(bom_line.attributes.all())
    attrs.sort(key=lambda a: (a.detail_number or 0, a.id or 0))
    lines = []
    for a in attrs:
        ct_label = ""
        if getattr(a, "charge_type_id", None) and getattr(a, "charge_type", None):
            ct_label = (a.charge_type.label or "").strip()
        val = (a.special_charge or "").strip() if a.special_charge else ""
        if not ct_label and not val:
            continue
        lines.append(
            {
                "id": a.id,
                "charge_type": ct_label,
                "charge_value": val,
            }
        )
    return lines


def _serialize_operation_charges_for_make_bag(operation_charges_qs):
    """
    ProductOperationCharge rows for Make Bag BOM panel (one row per charge, including multiple Other).
    """
    out = []
    for op in operation_charges_qs:
        name = (op.component_name or "").strip()
        raw = (str(op.charge_value).strip() if op.charge_value is not None else "")
        if name or raw:
            out.append(
                {
                    "id": op.id,
                    "charge_type": name,
                    "charge_value": raw,
                }
            )
    return out


def _resolve_charges_type_lookup(ct: str):
    """Map Make Bag free-text charge type to CHARGES_TYPE LookupValue."""
    name = (ct or "").strip()
    if not name:
        return None
    qs = LookupValue.objects.filter(lookup__code__iexact="CHARGES_TYPE")
    hit = qs.filter(label__iexact=name).first()
    if hit:
        return hit
    code = name.upper().replace(" ", "_").replace("-", "_")
    hit = qs.filter(code__iexact=code).first()
    if hit:
        return hit
    # Last resort: any lookup label (legacy data), prefer CHARGES_TYPE via ordering.
    return (
        LookupValue.objects.filter(label__iexact=name)
        .order_by("id")
        .first()
    )


def _persist_make_bag_charges(item, data, admin):
    """Save Make Bag special/other charges onto the catalog product item."""
    if item is None or not isinstance(data, dict):
        return
    ops = data.get("operation_charges")
    if isinstance(ops, list):
        ProductOperationCharge.objects.filter(product=item).delete()
        for op in ops:
            if not isinstance(op, dict):
                continue
            name = (op.get("charge_type") or op.get("component_name") or "").strip()
            val = op.get("charge_value") if "charge_value" in op else op.get("value")
            if not name and (val is None or str(val).strip() == ""):
                continue
            ProductOperationCharge.objects.create(
                product=item,
                component_name=(name or "Other")[:255],
                charge_value=str(val)[:50] if val not in (None, "") else None,
                created_by=admin,
                updated_by=admin,
            )
    bom_rows = data.get("bom_rows")
    if not isinstance(bom_rows, list):
        return
    for row in bom_rows:
        if not isinstance(row, dict):
            continue
        try:
            bom_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        bom = ProductBOM.objects.filter(pk=bom_id, product=item).first()
        if not bom:
            continue
        lines = row.get("charge_lines") or []
        ProductAttribute.objects.filter(product_bom=bom).delete()
        for line in lines:
            if not isinstance(line, dict):
                continue
            ct = (line.get("charge_type") or "").strip()
            cv = (line.get("charge_value") or "").strip()
            if not ct and not cv:
                continue
            charge_type = _resolve_charges_type_lookup(ct)
            ProductAttribute.objects.create(
                product_bom=bom,
                special_charge=cv or None,
                charge_type=charge_type,
                created_by=admin,
                updated_by=admin,
            )


def _stone_bom_display_parts(bom_line):
    """(combined_label, stone_name, master_spec_line) for a STONE BOM row."""
    s = (bom_line.stone.stone_name or "") if bom_line.stone_id and bom_line.stone else ""
    v = _stone_master_spec_line(bom_line.stone) if bom_line.stone_id and bom_line.stone else ""
    combined = f"{s} · {v}" if (s and v) else (s or v or "Stone")
    return combined, s, v


def _require_int(raw, field, errors):
    if raw is None or raw == "":
        errors.setdefault(field, []).append("Required.")
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        errors.setdefault(field, []).append("Must be an integer.")
        return None


def _require_decimal(raw, field, errors):
    if raw is None or raw == "":
        errors.setdefault(field, []).append("Required.")
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        errors.setdefault(field, []).append("Must be a valid number.")
        return None


def _create_item_template_from_product_item(item, errors):
    """Build internal item template from an existing catalog ProductItem (for multi-size bag-in)."""
    sku = item.sku
    if not sku:
        errors.setdefault("product_item_id", []).append("Selected item has no SKU.")
        return None
    bom = (
        ProductBOM.objects.filter(product=item, material_type="METAL")
        .order_by("id")
        .first()
    )
    if not bom or not bom.metal_id or not bom.purity_id:
        errors.setdefault("product_item_id", []).append(
            "Selected catalog line must have a METAL BOM row with metal and purity to clone for other sizes."
        )
        return None
    return {
        "product_code": (sku.product_code or "").strip(),
        "product_group_id": sku.product_group_id,
        "metal_id": bom.metal_id,
        "purity_id": bom.purity_id,
        "color_id": sku.color_id,
        "hsn_id": sku.hsn_id,
        "vendor_id": primary_vendor_id_for_item(item, ProductItemLinkedVendor=ProductItemLinkedVendor),
        "net_weight": _dec_item_weight(item.net_weight),
        "gross_weight": _dec_item_weight(item.gross_weight),
    }


def _persist_bag_receive_snapshot(
    bag,
    *,
    quantity,
    pcs=None,
    g_wt=None,
    stone_wt=None,
    net_wt=None,
    admin=None,
):
    """Store bag-level qty / pcs / weights so barcode UI can show each bag separately."""
    update_fields = []
    if quantity is not None:
        bag.quantity = int(quantity)
        update_fields.append("quantity")
    if pcs is not None:
        bag.pcs = int(pcs)
        update_fields.append("pcs")
    elif bag.pcs is None and bag.quantity is not None:
        bag.pcs = int(bag.quantity)
        update_fields.append("pcs")
    if g_wt is not None:
        bag.g_wt = g_wt
        update_fields.append("g_wt")
    if stone_wt is not None:
        bag.stone_wt = stone_wt
        update_fields.append("stone_wt")
    if net_wt is not None:
        bag.net_wt = net_wt
        update_fields.append("net_wt")
    if admin is not None:
        bag.updated_by = admin
        update_fields.append("updated_by")
    if update_fields:
        bag.save(update_fields=list(dict.fromkeys(update_fields)))
    return bag


def _bag_already_received(bag):
    return StockTransaction.objects.filter(bag=bag, txn_type="bag_in").exists()


def _lot_tracks_any_balance(lot):
    return any(
        [
            lot.g_wt is not None,
            lot.quantity is not None,
            lot.pcs is not None,
        ]
    )


def _lot_has_open_balance(lot):
    if (lot.status or "").strip().lower() in ("closed", "completed"):
        return False
    if lot.g_wt is not None and _quantize_g_wt(lot.g_wt) > 0:
        return True
    if lot.quantity is not None and lot.quantity > 0:
        return True
    if lot.pcs is not None and lot.pcs > 0:
        return True
    return False


def _lot_fully_depleted(lot):
    checks = []
    if lot.g_wt is not None:
        checks.append(lot.g_wt <= 0)
    if lot.quantity is not None:
        checks.append(lot.quantity <= 0)
    if lot.pcs is not None:
        checks.append(lot.pcs <= 0)
    return bool(checks) and all(checks)


def _save_lot_after_consuming_bag(lot, *, stock_qty_int, pcs_to_deduct, g_delta_opt, stone_delta_opt=None):
    """
    After a successful bag stock-in, reduce the lot's stored balances.
    pcs_to_deduct: whole pieces taken from the lot for this bag (defaults to stock_qty_int upstream).
    g_delta_opt: operator gross for this bag; required when the lot still tracks g_wt > 0.
    stone_delta_opt: accepted for API compat; stone is bag-level only (not deducted from lot).
    """
    update_fields = []
    _ = stone_delta_opt  # bag snapshot only; lot no longer tracks stone_wt

    if lot.quantity is not None:
        dq = Decimal(str(int(stock_qty_int)))
        if dq <= 0:
            raise ValidationError("Quantity to deduct from the lot must be positive.")
        if lot.quantity < dq:
            raise ValidationError(
                f"Bag quantity ({dq}) exceeds remaining lot quantity ({lot.quantity})."
            )
        lot.quantity = lot.quantity - dq
        if lot.quantity < 0:
            lot.quantity = Decimal("0")
        update_fields.append("quantity")

    if lot.pcs is not None:
        pd = int(pcs_to_deduct)
        if pd <= 0:
            raise ValidationError("PCS to deduct from the lot must be positive.")
        if lot.pcs < pd:
            raise ValidationError(f"Bag PCS ({pd}) exceeds remaining lot PCS ({lot.pcs}).")
        lot.pcs = lot.pcs - pd
        if lot.pcs < 0:
            lot.pcs = 0
        update_fields.append("pcs")

    if lot.g_wt is not None:
        rem = _quantize_g_wt(lot.g_wt)
        if rem > 0:
            if g_delta_opt is None:
                raise ValidationError(
                    "Enter G-wt for this bag — the lot still has a gross weight balance."
                )
            gq = _quantize_g_wt(g_delta_opt)
            if gq <= 0:
                raise ValidationError(
                    "G-wt for the bag must be greater than zero when the lot tracks gross weight."
                )
            if gq > rem:
                raise ValidationError(f"Bag G-wt ({gq}) exceeds remaining lot G-wt ({rem}).")
            new_g = rem - gq
            lot.g_wt = (
                _quantize_g_wt(new_g)
                if new_g > 0
                else Decimal("0").quantize(_G_WT_QUANT, rounding=ROUND_HALF_UP)
            )
            update_fields.append("g_wt")

    if _lot_fully_depleted(lot):
        lot.status = "Closed"
        update_fields.append("status")

    if update_fields:
        update_fields = list(dict.fromkeys(update_fields))
        if "system_updated_at" not in update_fields:
            update_fields.append("system_updated_at")
        lot.save(update_fields=update_fields)


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def make_bag_lots_sidebar(request):
    """
    Sidebar lots for Make Bag. By default only shows lots with remaining
    quantity or weight (i.e. not fully consumed by bags). Pass ?show_all=1
    to include fully consumed lots (future: lot history page).
    """
    show_all = request.GET.get("show_all") in ("1", "true")

    lots = (
        GrnLot.objects.select_related(
            "category", "subcategory", "batch", "updated_by"
        )
        .prefetch_related(
            Prefetch(
                "bags",
                queryset=GrnBag.objects.select_related("product_item").order_by(
                    "-system_updated_at"
                ),
            )
        )
        .order_by("-system_created_at")
    )
    results = []
    for lot in lots:
        bags = list(lot.bags.all())
        bags_count = len(bags)

        # Calculate consumed quantity from stock transactions for ALL bags in this lot
        # in a single query (avoids N+1).
        bag_ids = [b.id for b in bags]
        consumed_qty = 0
        if bag_ids:
            from django.db.models import Sum as _Sum
            agg = StockTransaction.objects.filter(
                bag_id__in=bag_ids, txn_type="bag_in"
            ).aggregate(total=_Sum("quantity"))
            consumed_qty = agg["total"] or 0

        # Remaining balances are stored on the lot row (decremented when bags are saved).
        # `consumed_qty` stays derived from stock txns for reference only.
        if lot.quantity is not None:
            remaining_qty = int(lot.quantity)
        elif lot.pcs is not None:
            remaining_qty = lot.pcs
        else:
            remaining_qty = 0

        is_fully_consumed = _lot_tracks_any_balance(lot) and not _lot_has_open_balance(lot)
        if is_fully_consumed and not show_all:
            continue

        if is_fully_consumed and lot.status not in ("Closed", "Completed"):
            GrnLot.objects.filter(pk=lot.pk).update(status="Closed")
            lot.status = "Closed"

        row = lot_to_dict(lot)
        resolved_pc = _resolved_lot_product_code(lot)
        if resolved_pc and not (row.get("product_code") or "").strip():
            row["product_code"] = resolved_pc
        mapped = next((b for b in bags if b.product_item_id), None)
        if mapped and mapped.product_item_id:
            it = mapped.product_item
            display_sku = (it.sku.product_code or "") if it.sku_id else ""
            mapped_product_item_id = mapped.product_item_id
            mapped_product_code = (it.sku.product_code or "") if it.sku_id else ""
            mapped_sku_size_type = (
                infer_item_size_type(
                    size_number=it.size_number,
                    size_mm=it.size_mm,
                    height_mm=it.height_mm,
                    width_mm=it.width_mm,
                )
                or SIZE_NUMBER
            )
        else:
            display_sku = f"{lot.lot_no} · map SKU"
            mapped_product_item_id = None
            mapped_product_code = ""
            mapped_sku_size_type = "NUMBER"
        row["display_sku"] = display_sku
        row["mapped_product_item_id"] = mapped_product_item_id
        row["mapped_product_code"] = mapped_product_code
        row["mapped_sku_size_type"] = mapped_sku_size_type
        row["bags_count"] = bags_count
        row["consumed_qty"] = consumed_qty
        row["remaining_qty"] = remaining_qty
        row["is_fully_consumed"] = is_fully_consumed
        results.append(row)
    return Response({"results": results, "next_bag_no": peek_next_bag_no()})


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def make_bag_next_bag_no(request):
    """GET /master/make-bag/next-bag-no/ — next unused global BAG-NNNNN."""
    return Response({"bag_no": peek_next_bag_no()})


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def make_bag_search_items(request):
    q = (request.GET.get("q") or "").strip()
    if len(q) < 1:
        return Response({"results": []})
    qs = (
        ProductItem.objects.select_related("sku", "sku__product_group")
        .filter(
            Q(sku__product_code__icontains=q)
            | Q(sku__sku_code__icontains=q)
            | Q(sku__pattern_code__icontains=q)
            | Q(sku__product_group__style_name__icontains=q)
            | Q(store_variant_name__icontains=q)
            | product_item_search_q(q)
        )
        .order_by("sku_id", "size_number", "size_mm", "height_mm", "width_mm")[:40]
    )
    results = []
    for item in qs:
        metal_name = ""
        if item.sku_id:
            bom = next(
                (b for b in item.bom_items.all() if b.material_type == "METAL" and b.metal_id),
                None,
            )
            if bom and bom.metal:
                metal_name = bom.metal.metal_name or ""
        style = ""
        if item.sku_id and item.sku.product_group_id:
            style = item.sku.product_group.style_name or ""
        row = {
            "id": item.id,
            "product_code": item.sku.product_code if item.sku_id else "",
            "pattern_code": (item.sku.pattern_code or "") if item.sku_id else "",
            "sku_code": (item.sku.sku_code or "") if item.sku_id else "",
            "qty": item.qty,
            "style_name": style,
            "metal_name": metal_name,
            "net_weight": _dec_item_weight(item.net_weight),
            "gross_weight": _dec_item_weight(item.gross_weight),
            "sku_id": item.sku_id,
        }
        row.update(serialize_product_item_size_for_api(item))
        results.append(row)
    return Response({"results": results})


def _catalog_filter_params_from_request(request):
    """Shared GET params for make-bag catalog items and facets."""
    return {
        "category_id": request.GET.get("category_id"),
        "subcategory_id": request.GET.get("subcategory_id"),
        "item_group": (request.GET.get("item_group") or "").strip(),
        "item_type": (request.GET.get("item_type") or "").strip(),
        "metal": (request.GET.get("metal") or "").strip(),
        "purity": (request.GET.get("purity") or "").strip(),
        "colour": (request.GET.get("colour") or "").strip(),
        "pattern_code": (request.GET.get("pattern_code") or "").strip(),
        "product_code": (request.GET.get("product_code") or "").strip(),
        "vendor_variant_name": (request.GET.get("vendor_variant_name") or "").strip(),
        "vendor_name": (request.GET.get("vendor_name") or "").strip(),
        "q": (request.GET.get("q") or "").strip(),
    }


def _catalog_items_base_queryset(*, with_prefetch=True):
    qs = ProductItem.objects.select_related(
        "sku",
        "sku__product_group",
        "sku__product_group__category",
        "sku__product_group__subcategory",
        "sku__color",
    ).filter(sku__isnull=False)
    if not with_prefetch:
        return qs
    return qs.prefetch_related(
        "bom_items__metal",
        "bom_items__purity",
        "bom_items__stone",
        "bom_items__stone__size_unit",
        "bom_items__stone__stone_group",
        "bom_items__stone__clarity",
        "bom_items__stone__cut",
        "bom_items__attributes",
        "bom_items__attributes__making_category",
        "bom_items__attributes__crafting_process",
        "bom_items__attributes__method",
        "bom_items__attributes__nature",
        "bom_items__attributes__finishing",
        "bom_items__attributes__charge_type",
        "operation_charges",
        "occasions__occasion",
    )


def _apply_catalog_item_filters(qs, params, *, depth="full"):
    """
    Apply lot/catalog filters. depth controls how far down the cascade is applied:
      scope — store variant only (item groups)
      types — + category (item types)
      metals — + subcategory
      purities — + metal
      full — all filters including purity, colour, search
    """
    vendor_variant_name = params.get("vendor_variant_name") or ""
    if vendor_variant_name:
        qs = filter_items_by_vendor_variant_name(qs, vendor_variant_name)

    vendor_name = params.get("vendor_name") or ""
    if vendor_name:
        qs = filter_items_by_vendor_name(qs, vendor_name, Vendor=Vendor)

    category_id = params.get("category_id")
    item_group = params.get("item_group") or ""
    if depth in ("types", "metals", "purities", "full"):
        if category_id not in (None, ""):
            try:
                qs = qs.filter(sku__product_group__category_id=int(category_id))
            except (TypeError, ValueError):
                pass
        elif item_group:
            qs = qs.filter(sku__product_group__category__name__iexact=item_group)

    subcategory_id = params.get("subcategory_id")
    item_type = params.get("item_type") or ""
    if depth in ("metals", "purities", "full"):
        if subcategory_id not in (None, ""):
            try:
                qs = qs.filter(sku__product_group__subcategory_id=int(subcategory_id))
            except (TypeError, ValueError):
                pass
        elif item_type:
            qs = qs.filter(sku__product_group__subcategory__name__iexact=item_type)

    pattern_code = (params.get("pattern_code") or "").strip()
    if depth == "full" and pattern_code:
        qs = qs.filter(sku__pattern_code__iexact=pattern_code)

    product_code = _resolved_catalog_product_code(params) if depth == "full" else ""
    if depth == "full" and product_code:
        qs = qs.filter(sku__product_code__iexact=product_code)

    # Make Bag: when both codes are provided, list is product_code ∩ pattern_code
    # (already applied above as AND filters).

    metal = params.get("metal") or ""
    if depth in ("purities", "full") and metal:
        qs = qs.filter(
            bom_items__material_type="METAL",
            bom_items__metal__metal_name__iexact=metal,
        )

    if depth == "full":
        purity = params.get("purity") or ""
        if purity:
            qs = qs.filter(
                bom_items__material_type="METAL",
                bom_items__purity__purity_name__iexact=purity,
            )
        colour = params.get("colour") or ""
        if colour:
            qs = qs.filter(sku__color__label__iexact=colour)
        q = params.get("q") or ""
        if q:
            qs = qs.filter(
                Q(sku__product_code__icontains=q)
                | Q(sku__sku_code__icontains=q)
                | Q(sku__pattern_code__icontains=q)
                | Q(sku__product_group__style_name__icontains=q)
                | Q(
                    bom_items__material_type="METAL",
                    bom_items__metal__metal_name__icontains=q,
                )
                | product_item_search_q(q)
            )

    return qs.distinct()


def _facet_item_groups(qs):
    rows = (
        qs.filter(sku__product_group__category_id__isnull=False)
        .values_list(
            "sku__product_group__category_id",
            "sku__product_group__category__name",
        )
        .distinct()
        .order_by("sku__product_group__category__name")
    )
    return [{"id": cid, "name": cname or ""} for cid, cname in rows if cid and cname]


def _facet_item_types(qs):
    rows = (
        qs.filter(sku__product_group__subcategory_id__isnull=False)
        .values_list(
            "sku__product_group__subcategory_id",
            "sku__product_group__subcategory__name",
            "sku__product_group__category_id",
        )
        .distinct()
        .order_by("sku__product_group__subcategory__name")
    )
    return [
        {"id": sid, "name": sname or "", "category_id": cid}
        for sid, sname, cid in rows
        if sid and sname
    ]


def _facet_metals(qs):
    names = (
        ProductBOM.objects.filter(
            product__in=qs,
            material_type="METAL",
            metal_id__isnull=False,
        )
        .values_list("metal__metal_name", flat=True)
        .distinct()
    )
    return sorted(
        {str(n).strip() for n in names if n and str(n).strip()},
        key=lambda s: s.lower(),
    )


def _facet_purities(qs, *, metal_name):
    bom_qs = ProductBOM.objects.filter(
        product__in=qs,
        material_type="METAL",
        metal__metal_name__iexact=metal_name,
        purity_id__isnull=False,
    )
    names = set()
    for pn, pt in bom_qs.values_list("purity__purity_name", "purity__type").distinct():
        label = (pn or pt or "").strip() if (pn or pt) else ""
        if label:
            names.add(label)
    return sorted(names, key=lambda s: s.lower())


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def make_bag_catalog_facets(request):
    """
    Product-based cascade options for Batch Creation (item group → type → metal → purity).
    Scoped by vendor_name (batch vendor) or vendor_variant_name when provided.
    """
    params = _catalog_filter_params_from_request(request)
    store_variant = params.get("vendor_variant_name") or ""
    category_id = params.get("category_id")
    item_group = params.get("item_group") or ""
    subcategory_id = params.get("subcategory_id")
    item_type = params.get("item_type") or ""
    metal = params.get("metal") or ""

    has_category = (category_id not in (None, "")) or bool(item_group)
    has_subcategory = (subcategory_id not in (None, "")) or bool(item_type)

    payload = {"item_groups": [], "item_types": [], "metals": [], "purities": []}

    scope_qs = _apply_catalog_item_filters(
        _catalog_items_base_queryset(with_prefetch=False),
        params,
        depth="scope",
    )
    payload["item_groups"] = _facet_item_groups(scope_qs)

    if has_category:
        types_qs = _apply_catalog_item_filters(
            _catalog_items_base_queryset(with_prefetch=False),
            params,
            depth="types",
        )
        payload["item_types"] = _facet_item_types(types_qs)

    if has_category and has_subcategory:
        metals_qs = _apply_catalog_item_filters(
            _catalog_items_base_queryset(with_prefetch=False),
            params,
            depth="metals",
        )
        payload["metals"] = _facet_metals(metals_qs)

    if has_category and has_subcategory and metal:
        purities_qs = _apply_catalog_item_filters(
            _catalog_items_base_queryset(with_prefetch=False),
            params,
            depth="purities",
        )
        payload["purities"] = _facet_purities(purities_qs, metal_name=metal)

    return Response(payload)


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def make_bag_catalog_items(request):
    """
    ProductItem rows whose SKU matches lot-style filters (item group / type / metal / purity / colour).
    Prefers category_id & subcategory_id when present; falls back to name match on ProductGroup.
    """
    params = _catalog_filter_params_from_request(request)

    has_scope = bool(
        (params["category_id"] not in (None, ""))
        or (params["subcategory_id"] not in (None, ""))
        or params["item_group"]
        or params["item_type"]
        or params["metal"]
        or params["purity"]
        or params["colour"]
        or params["vendor_variant_name"]
        or params["vendor_name"]
        or params["pattern_code"]
        or params["product_code"]
        or params["q"]
    )
    if not has_scope:
        return Response({"results": []})

    qs = _apply_catalog_item_filters(
        _catalog_items_base_queryset(with_prefetch=True),
        params,
        depth="full",
    )
    qs = qs.order_by("-system_created_at", "-id")[:500]

    resolved_vendor_id = None
    if params.get("vendor_name"):
        resolved_vendor_id = resolve_vendor_id_by_name(
            params["vendor_name"], Vendor=Vendor
        )

    results = []
    for item in qs:
        sku = item.sku
        if not sku:
            continue

        pg = sku.product_group if sku else None
        style = (pg.style_name or "") if pg else ""
        item_group = (pg.category.name or "") if pg and pg.category_id and pg.category else ""
        item_type = (pg.subcategory.name or "") if pg and pg.subcategory_id and pg.subcategory else ""
        # Cache bom_items list once — avoids re-evaluating the prefetch 3 times.
        bom_items_list = list(item.bom_items.all())
        metal_bom = next(
            (b for b in bom_items_list if b.material_type == "METAL" and b.metal_id),
            None,
        )
        metal_name = (metal_bom.metal.metal_name or "") if metal_bom else ""
        purity_name = (
            (metal_bom.purity.purity_name or "")
            if metal_bom and metal_bom.purity_id
            else ""
        )
        color_label = ""
        if sku and sku.color_id:
            color_label = sku.color.label or ""
        # Stone (first STONE bom row, if present) — used by the frontend to prefill
        # the Stone / Stone Variant attribute rows when this SKU is selected.
        stone_bom = next(
            (b for b in bom_items_list if b.material_type == "STONE" and b.stone_id),
            None,
        )
        stone_name = (stone_bom.stone.stone_name or "") if stone_bom else ""
        stone_variant_name = ""
        stone_master_spec = (
            _stone_master_spec_line(stone_bom.stone)
            if stone_bom and stone_bom.stone_id and stone_bom.stone
            else ""
        )

        # Full BOM rows for the Make Bag BOM table — one row per material line
        # the latest item was published with. Frontend renders these read-only
        # under the selected SKU.
        bom_rows = []
        for b in bom_items_list:
            if b.material_type == "METAL":
                m = b.metal.metal_name if b.metal_id and b.metal else ""
                p = b.purity.purity_name if b.purity_id and b.purity else ""
                variant = (
                    f"{m} · {p}" if m and p else (m or p or "Metal")
                )
                group_label = (m or "METAL").upper()
                g_wt = str(b.weight) if b.weight is not None else ""
                st_wt = ""
                row_out = {
                    "id": b.id,
                    "variant_name": variant,
                    "item_group": group_label,
                    "material_type": b.material_type,
                    "pcs": str(b.quantity if b.quantity is not None else 1),
                    "g_wt": g_wt,
                    "st_wt": st_wt,
                    "charge_lines": _serialize_bom_charge_lines(b),
                }
                bom_rows.append(row_out)
            else:  # STONE
                variant, s, spec = _stone_bom_display_parts(b)
                group_label = (s or "STONE").upper()
                g_wt = ""
                st_wt = str(b.weight) if b.weight is not None else ""
                row_out = {
                    "id": b.id,
                    "variant_name": variant,
                    "item_group": group_label,
                    "material_type": b.material_type,
                    "pcs": str(b.quantity if b.quantity is not None else 1),
                    "g_wt": g_wt,
                    "st_wt": st_wt,
                }
                if s or spec:
                    row_out["stone_name"] = s
                    row_out["stone_variant_name"] = ""
                    row_out["stone_master_spec"] = spec
                row_out["charge_lines"] = _serialize_bom_charge_lines(b)
                bom_rows.append(row_out)

        # Resolve attribute labels from the first METAL bom row's attributes (prefetched).
        attr_labels = {
            "mkg_category": "",
            "crafting_process": "",
            "method": "",
            "nature": "",
            "finishing": "",
        }
        attr_bom = metal_bom or stone_bom
        if attr_bom:
            attr = next(iter(attr_bom.attributes.all()), None)
            if attr:
                attr_labels["mkg_category"] = attr.making_category.label if attr.making_category_id and attr.making_category else ""
                attr_labels["crafting_process"] = attr.crafting_process.label if attr.crafting_process_id and attr.crafting_process else ""
                attr_labels["method"] = attr.method.label if attr.method_id and attr.method else ""
                attr_labels["nature"] = attr.nature.label if attr.nature_id and attr.nature else ""
                attr_labels["finishing"] = attr.finishing.label if attr.finishing_id and attr.finishing else ""

        # Occasion label(s) from ProductOccasion (prefetched).
        occasion_labels = [
            occ.occasion.label
            for occ in item.occasions.all()
            if occ.occasion_id and occ.occasion
        ]
        occasion = occasion_labels[0] if occasion_labels else ""

        results.append(
            {
                "id": item.id,                            # latest ProductItem id under this SKU
                "product_code": sku.product_code or "",
                "pattern_code": sku.pattern_code or "",
                "sku_id": sku.id,
                "sku_code": sku.sku_code or "",
                "style_name": style,
                "store_variant_name": (item.store_variant_name or "").strip(),
                "vendor_variant_name": vendor_variant_name_for_item(
                    item,
                    vendor_id=resolved_vendor_id,
                    ProductItemLinkedVendor=ProductItemLinkedVendor,
                ),
                "item_group": item_group,
                "item_type": item_type,
                "metal_name": metal_name,
                "purity_name": purity_name,
                "color_label": color_label,
                "stone_name": stone_name,
                "stone_variant_name": stone_variant_name,
                "stone_master_spec": stone_master_spec,
                "mkg_category": attr_labels["mkg_category"],
                "crafting_process": attr_labels["crafting_process"],
                "method": attr_labels["method"],
                "nature": attr_labels["nature"],
                "finishing": attr_labels["finishing"],
                "occasion": occasion,
                "net_weight": _dec_item_weight(item.net_weight),
                "gross_weight": _dec_item_weight(item.gross_weight),
                "bom_rows": bom_rows,
                "operation_charge_rows": _serialize_operation_charges_for_make_bag(
                    item.operation_charges.all()
                ),
            }
        )
        results[-1].update(serialize_product_item_size_for_api(item))
        if len(results) >= 100:
            break
    return Response({"results": results})


def _resolve_or_create_product_item(admin, create_payload, errors):
    code = (create_payload.get("product_code") or "").strip()
    if not code:
        errors.setdefault("product_code", []).append(
            "Required on selected catalog SKU template for size-distribution receive."
        )
        return None

    pg = _require_int(create_payload.get("product_group_id"), "product_group_id", errors)
    metal_id = _require_int(create_payload.get("metal_id"), "metal_id", errors)
    purity_id = _require_int(create_payload.get("purity_id"), "purity_id", errors)
    color_id = _require_int(create_payload.get("color_id"), "color_id", errors)
    hsn_id = _require_int(create_payload.get("hsn_id"), "hsn_id", errors)
    vendor_id = _require_int(create_payload.get("vendor_id"), "vendor_id", errors)
    net_w = _require_decimal(create_payload.get("net_weight"), "net_weight", errors)
    gross_w = _require_decimal(create_payload.get("gross_weight"), "gross_weight", errors)

    if errors:
        return None

    if not ProductGroup.objects.filter(pk=pg).exists():
        errors.setdefault("product_group_id", []).append("Invalid product_group_id.")
    if not MetalMasterRule.objects.filter(pk=purity_id, metal_id=metal_id).exists():
        errors.setdefault("purity_id", []).append("Must be a purity rule for the selected metal.")
    if not HSNMaster.objects.filter(pk=hsn_id).exists():
        errors.setdefault("hsn_id", []).append("Invalid hsn_id.")
    if not Vendor.objects.filter(pk=vendor_id).exists():
        errors.setdefault("vendor_id", []).append("Invalid vendor_id.")
    if errors:
        return None

    with transaction.atomic():
        sku = ProductSKU.objects.filter(
            product_group_id=pg,
            color_id=color_id,
            hsn_id=hsn_id,
        ).first()
        if not sku:
            sku = ProductSKU.objects.create(
                product_group_id=pg,
                color_id=color_id,
                hsn_id=hsn_id,
                product_code=code,
                created_by=admin,
                updated_by=admin,
            )
        else:
            if not sku.product_code:
                sku.product_code = code
                sku.updated_by = admin
                sku.save(update_fields=["product_code", "updated_by", "system_updated_at"])

        try:
            size_kw = apply_create_payload_size_fields(create_payload)
        except ValidationError as e:
            if hasattr(e, "error_dict") and e.error_dict:
                for k, msgs in e.error_dict.items():
                    errors.setdefault(k, []).extend([str(m) for m in msgs])
            else:
                errors.setdefault("size_distribution", []).append(str(e))
            return None

        flt = item_filter_for_size(
            sku,
            size_number=size_kw.get("size_number"),
            size_mm=size_kw.get("size_mm"),
            height_mm=size_kw.get("height_mm"),
            width_mm=size_kw.get("width_mm"),
        )
        existing = ProductItem.objects.filter(flt).first()
        if existing:
            return existing

        item = ProductItem.objects.create(
            sku=sku,
            qty=0,
            net_weight=net_w,
            gross_weight=gross_w,
            created_by=admin,
            updated_by=admin,
            **size_kw,
        )
        ProductItemLinkedVendor.objects.create(
            product_item=item,
            vendor_id=vendor_id,
            sort_order=0,
            created_by=admin,
            updated_by=admin,
        )
        return item


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_LOT_CREATE")
def make_bag_save(request):
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

    data = request.data or {}
    errors = {}

    lot_id = data.get("lot_id")
    try:
        lot_id = int(lot_id)
    except (TypeError, ValueError):
        lot_id = None
    if not lot_id:
        errors.setdefault("lot_id", []).append("Required.")

    # bag_no is now OPTIONAL — server auto-generates a unique one (BAG-NNNNN)
    # when the operator leaves the field blank. If they typed something, we
    # respect it but still verify uniqueness within the lot below.
    bag_no = (data.get("bag_no") or "").strip()

    remark = (data.get("remark") or "").strip()

    product_item_id = data.get("product_item_id")
    if product_item_id is not None and product_item_id != "":
        try:
            product_item_id = int(product_item_id)
        except (TypeError, ValueError):
            errors.setdefault("product_item_id", []).append("Invalid.")
            product_item_id = None
    else:
        product_item_id = None

    if "create_item" in data:
        errors.setdefault("create_item", []).append(
            "Creating SKU/item from Make Bag is disabled. Use Product Creation (+ New)."
        )

    raw_dist = data.get("size_distribution")
    dist_list = None
    if raw_dist is not None:
        if not isinstance(raw_dist, list):
            errors.setdefault("size_distribution", []).append("Must be a list.")
        elif len(raw_dist) > 0:
            dist_list = raw_dist

    # Operator-entered Make Bag values. `item_qty` becomes this bag's stock-in
    # delta (ProductItem.qty += item_qty). `item_g_wt`, when provided, overwrites
    # ProductItem.gross_weight (and net_weight if it's currently empty/zero).
    def _to_int(v, field):
        if v is None or v == "":
            return None
        try:
            n = int(str(v).strip())
            if n < 0:
                errors.setdefault(field, []).append("Must be ≥ 0.")
                return None
            return n
        except (TypeError, ValueError):
            errors.setdefault(field, []).append("Invalid integer.")
            return None

    def _to_decimal(v, field):
        if v is None or v == "":
            return None
        try:
            from decimal import Decimal as _D
            n = _D(str(v).strip())
            if n < 0:
                errors.setdefault(field, []).append("Must be ≥ 0.")
                return None
            return n
        except Exception:
            errors.setdefault(field, []).append("Invalid decimal.")
            return None

    item_qty_in = _to_int(data.get("item_qty"), "item_qty")
    item_pcs_in = _to_int(data.get("item_pcs"), "item_pcs")
    item_g_wt_in = _to_decimal(data.get("item_g_wt"), "item_g_wt")
    item_stone_wt_in = _to_decimal(data.get("item_stone_wt"), "item_stone_wt")

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    try:
        lot_for_bag_no = GrnLot.objects.get(pk=lot_id)
    except GrnLot.DoesNotExist:
        return Response({"errors": {"lot_id": ["Lot not found."]}}, status=status.HTTP_404_NOT_FOUND)

    # Resolve final bag_no — auto-generate when blank, validate uniqueness when typed.
    bag_no = _allocate_unique_bag_no(lot_for_bag_no, attempt_seed=bag_no)

    # ── Multi-size: one ProductItem + bag + stock txn per structured size line ──
    if dist_list:
        template = None
        size_type_st = None
        if product_item_id:
            try:
                tmpl_item = ProductItem.objects.select_related("sku").get(pk=product_item_id)
            except ProductItem.DoesNotExist:
                return Response(
                    {"errors": {"product_item_id": ["Product item not found."]}},
                    status=status.HTTP_404_NOT_FOUND,
                )
            template = _create_item_template_from_product_item(tmpl_item, errors)
            size_type_st = (data.get("size_type") or "").strip().upper()
            if size_type_st not in (SIZE_NUMBER, SIZE_MM, SIZE_HW):
                size_type_st = (
                    infer_item_size_type(
                        size_number=tmpl_item.size_number,
                        size_mm=tmpl_item.size_mm,
                        height_mm=tmpl_item.height_mm,
                        width_mm=tmpl_item.width_mm,
                    )
                    or SIZE_NUMBER
                )
        else:
            return Response(
                {
                    "errors": {
                        "_body": [
                            "size_distribution requires product_item_id (catalog template).",
                        ],
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if errors:
            return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

        try:
            merged_rows = merge_size_distribution_rows(size_type_st, dist_list)
        except ValidationError as e:
            ed = getattr(e, "error_dict", None) or {}
            if ed:
                return Response({"errors": {k: list(v) for k, v in ed.items()}}, status=status.HTTP_400_BAD_REQUEST)
            return Response(
                {"errors": {"size_distribution": [str(e)]}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bags_payload = []
        total_stock = 0
        with transaction.atomic():
            lot = GrnLot.objects.select_for_update().get(pk=lot_id)
            if (lot.status or "").strip().lower() in ("closed", "completed"):
                return Response(
                    {"errors": {"lot_id": ["This lot is closed; cannot add bags."]}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            for size_kw, qty in merged_rows:
                loop_errors = {}
                cp = {**template, **size_kw, "size_type": size_type_st}
                row_item = _resolve_or_create_product_item(admin, cp, loop_errors)
                if loop_errors:
                    return Response({"errors": loop_errors}, status=status.HTTP_400_BAD_REQUEST)

                disp = format_size_kwargs_display(size_type_st, size_kw)
                eff_bag_no = (
                    bag_no
                    if len(merged_rows) == 1
                    else f"{bag_no}-{slug_from_size_display(disp)}"
                )
                bag, created = GrnBag.objects.get_or_create(
                    lot=lot,
                    bag_no=eff_bag_no,
                    defaults={
                        "product_item": row_item,
                        "remark": remark,
                        "created_by": admin,
                        "updated_by": admin,
                    },
                )
                if not created:
                    if _bag_already_received(bag):
                        return Response(
                            {
                                "errors": {
                                    "bag_no": [
                                        f"Bag {eff_bag_no} was already received into stock; "
                                        "quantities cannot be changed.",
                                    ],
                                },
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    bag.product_item = row_item
                    bag.remark = remark
                    bag.updated_by = admin
                    bag.save()

                try:
                    adjust_product_item_qty(
                        product_item=row_item,
                        delta=qty,
                        txn_type="bag_in",
                        admin=admin,
                        branch=None,
                        bag=bag,
                        reference=f"Lot {lot.lot_no} / Bag {eff_bag_no}",
                        notes="",
                    )
                except ValidationError as e:
                    return Response(
                        {"errors": {"quantity": [str(e)]}},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                _persist_bag_receive_snapshot(
                    bag,
                    quantity=qty,
                    pcs=qty,
                    admin=admin,
                )

                pc = ""
                if row_item.sku_id:
                    pc = row_item.sku.product_code or ""
                total_stock += qty
                row_out = {
                    "id": bag.id,
                    "lot_id": lot.id,
                    "bag_no": bag.bag_no,
                    "product_item_id": bag.product_item_id,
                    "product_code": pc,
                    "remark": bag.remark or "",
                    "quantity": qty,
                    "stock_added": qty,
                }
                row_out.update(serialize_product_item_size_for_api(row_item))
                bags_payload.append(row_out)

            # Split operator G-wt / stone-wt across size bags by qty share.
            if bags_payload and (item_g_wt_in is not None or item_stone_wt_in is not None) and total_stock > 0:
                rem_g = item_g_wt_in
                rem_s = item_stone_wt_in
                for i, row in enumerate(bags_payload):
                    bag_row = GrnBag.objects.get(pk=row["id"])
                    qshare = Decimal(str(int(row["quantity"])))
                    if i == len(bags_payload) - 1:
                        share_g = rem_g
                        share_s = rem_s
                    else:
                        share_g = (
                            (item_g_wt_in * qshare / Decimal(total_stock)).quantize(
                                _G_WT_QUANT, rounding=ROUND_HALF_UP
                            )
                            if item_g_wt_in is not None
                            else None
                        )
                        share_s = (
                            (item_stone_wt_in * qshare / Decimal(total_stock)).quantize(
                                _G_WT_QUANT, rounding=ROUND_HALF_UP
                            )
                            if item_stone_wt_in is not None
                            else None
                        )
                        if rem_g is not None and share_g is not None:
                            rem_g = rem_g - share_g
                        if rem_s is not None and share_s is not None:
                            rem_s = rem_s - share_s
                    _persist_bag_receive_snapshot(
                        bag_row,
                        quantity=row["quantity"],
                        pcs=row["quantity"],
                        g_wt=share_g,
                        stone_wt=share_s,
                        net_wt=share_g,
                        admin=admin,
                    )

            try:
                pcs_ded = item_pcs_in if item_pcs_in is not None else total_stock
                _save_lot_after_consuming_bag(
                    lot,
                    stock_qty_int=total_stock,
                    pcs_to_deduct=pcs_ded,
                    g_delta_opt=item_g_wt_in,
                    stone_delta_opt=item_stone_wt_in,
                )
            except ValidationError as e:
                return Response(
                    {"errors": {"_body": [str(e)]}},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if product_item_id:
            try:
                tmpl = ProductItem.objects.get(pk=product_item_id)
                _persist_make_bag_charges(tmpl, data, admin)
            except ProductItem.DoesNotExist:
                pass

        first = bags_payload[0]
        return Response(
            {
                **first,
                "bags": bags_payload,
                "total_stock_added": total_stock,
            },
            status=status.HTTP_200_OK,
        )

    # ── Single line (legacy): one bag, one ProductItem ──
    item = None
    if product_item_id:
        try:
            item = ProductItem.objects.get(pk=product_item_id)
        except ProductItem.DoesNotExist:
            return Response(
                {"errors": {"product_item_id": ["Product item not found."]}},
                status=status.HTTP_404_NOT_FOUND,
            )
    else:
        return Response(
            {
                "errors": {
                    "_body": [
                        "Provide product_item_id (existing catalog SKU). "
                        "Create new SKU from Product Creation (+ New)."
                    ],
                }
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        lot = GrnLot.objects.select_for_update().get(pk=lot_id)
        if (lot.status or "").strip().lower() in ("closed", "completed"):
            return Response(
                {"errors": {"lot_id": ["This lot is closed; cannot add bags."]}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Stock-in quantity for THIS bag. Priority:
        #   1. operator-typed `item_qty` from the Make Bag card
        #   2. legacy `quantity` field
        #   3. lot.quantity / lot.pcs fallback (locked row)
        #   4. default 1
        quantity = item_qty_in
        if quantity is None:
            quantity = data.get("quantity")
            try:
                quantity = int(quantity) if quantity not in (None, "") else None
            except (TypeError, ValueError):
                quantity = None
        if quantity is None:
            quantity = lot.quantity
        if quantity is None:
            quantity = lot.pcs
        if quantity is None or quantity < 1:
            quantity = 1
        stock_qty_int = int(quantity)

        bag, created = GrnBag.objects.get_or_create(
            lot=lot,
            bag_no=bag_no,
            defaults={
                "product_item": item,
                "remark": remark,
                "created_by": admin,
                "updated_by": admin,
            },
        )
        if not created:
            if _bag_already_received(bag):
                return Response(
                    {
                        "errors": {
                            "bag_no": [
                                "This bag was already received into stock; quantities cannot be changed.",
                            ],
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            bag.product_item = item
            bag.remark = remark
            bag.updated_by = admin
            bag.save()

        try:
            adjust_product_item_qty(
                product_item=item,
                delta=stock_qty_int,
                txn_type="bag_in",
                admin=admin,
                branch=None,
                bag=bag,
                reference=f"Lot {lot.lot_no} / Bag {bag_no}",
                notes="",
            )
        except ValidationError as e:
            return Response(
                {"errors": {"quantity": [str(e)]}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        pcs_ded = item_pcs_in if item_pcs_in is not None else stock_qty_int
        # Keep catalog item weights as template; bag-level snapshot drives barcode UI.
        # Still refresh item gross when this is the only bag-in so legacy screens stay sane.
        if item_g_wt_in is not None:
            other_bags = (
                GrnBag.objects.filter(product_item_id=item.id)
                .exclude(pk=bag.pk)
                .exists()
            )
            if not other_bags:
                update_fields = ["gross_weight", "updated_by"]
                item.gross_weight = item_g_wt_in
                if not item.net_weight or item.net_weight == 0:
                    item.net_weight = item_g_wt_in
                    update_fields.append("net_weight")
                item.updated_by = admin
                item.save(update_fields=update_fields)

        _persist_bag_receive_snapshot(
            bag,
            quantity=stock_qty_int,
            pcs=pcs_ded,
            g_wt=item_g_wt_in,
            stone_wt=item_stone_wt_in,
            net_wt=item_g_wt_in,
            admin=admin,
        )

        try:
            _save_lot_after_consuming_bag(
                lot,
                stock_qty_int=stock_qty_int,
                pcs_to_deduct=pcs_ded,
                g_delta_opt=item_g_wt_in,
                stone_delta_opt=item_stone_wt_in,
            )
        except ValidationError as e:
            return Response(
                {"errors": {"_body": [str(e)]}},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if item is not None:
        _persist_make_bag_charges(item, data, admin)

    pc = ""
    if bag.product_item_id and bag.product_item.sku_id:
        pc = bag.product_item.sku.product_code or ""

    body = {
        "id": bag.id,
        "lot_id": lot.id,
        "bag_no": bag.bag_no,
        "product_item_id": bag.product_item_id,
        "product_code": pc,
        "remark": bag.remark or "",
        "quantity": stock_qty_int,
        "stock_added": stock_qty_int,
    }
    if bag.product_item_id:
        body.update(serialize_product_item_size_for_api(bag.product_item))
    return Response(body, status=status.HTTP_200_OK)
