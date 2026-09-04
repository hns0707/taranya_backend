"""
Barcode / tag generation — single source of truth for the tagging system.
A "barcode" and a "tag" are the same thing in this project; the public name on
the admin UI is "Barcode generator", the backing model is `ProductTag`.

Function-based views, no serializers.
Endpoints all live under /master/barcode/...
"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth, ensure_admin_permission
from master.views.make_bag_view import _serialize_operation_charges_for_make_bag
from master.views.product_views import get_admin_user_from_request, _stone_master_spec_readonly
from django.db.models import Q, Count, F, Value, IntegerField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from shared.models import (
    Branch,
    ProductBOM,
    ProductItem,
    ProductItemLinkedVendor,
    ProductOperationCharge,
    ProductTag,
    ProductTagMetal,
    ProductTagPhoto,
    GrnBag,
    StockTransaction,
    MetalMasterRule,
)
from shared.grn_weight_parse import parse_optional_weight_decimal
from shared.product_item_size import (
    product_item_search_q,
    serialize_product_item_size_for_api,
)
from shared.services.product_code_prefix import allocate_tag_values
from shared.services.product_item_vendors import primary_vendor_name_for_item
from shared.services.tag_print import tag_scan_lookup_q


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tag_weight_decimal(val) -> Decimal:
    try:
        return Decimal(str(val or "0").strip() or "0")
    except Exception:
        return Decimal("0")



def _resolve_stock_weight_totals(
    stored_gross: Decimal,
    stored_net: Decimal,
    stock_dec: Decimal,
    tags: list,
):
    """
    Resolve ProductItem gross/net into TOTALS for the full stock qty.

    Make Bag writes operator G-wt as the bag TOTAL (same value deducted from the lot)
    onto ProductItem.gross_weight. Older catalog data may store per-piece weights.
    """
    if stock_dec <= 0:
        total_gross = stored_gross
    elif stock_dec == 1:
        total_gross = stored_gross
    else:
        # If existing tags have weights, detect per-piece vs bag-total storage.
        tagged_vals = [
            _tag_weight_decimal(t.get("gross_weight"))
            for t in tags
            if _tag_weight_decimal(t.get("gross_weight")) > 0
        ]
        treat_as_per_piece = False
        if tagged_vals and stored_gross > 0:
            avg = sum(tagged_vals) / Decimal(len(tagged_vals))
            err_piece = abs(stored_gross - avg)
            err_total = abs(stored_gross - (avg * stock_dec))
            # Clearly closer to one piece than to bag total -> stored is per-piece.
            if err_piece < err_total:
                treat_as_per_piece = True

        if treat_as_per_piece:
            total_gross = stored_gross * stock_dec
        else:
            # Default GRN / Make Bag semantics: stored value is bag total.
            total_gross = stored_gross

    if stored_net <= 0:
        total_net = total_gross
    elif stock_dec <= 1:
        total_net = stored_net
    else:
        tagged_nets = [
            _tag_weight_decimal(t.get("net_weight"))
            for t in tags
            if _tag_weight_decimal(t.get("net_weight")) > 0
        ]
        treat_net_as_per_piece = False
        if tagged_nets and stored_net > 0:
            avg_n = sum(tagged_nets) / Decimal(len(tagged_nets))
            if abs(stored_net - avg_n) < abs(stored_net - (avg_n * stock_dec)):
                treat_net_as_per_piece = True
        # Legacy: net stored larger than gross often meant bag-total net already.
        if stored_gross > 0 and stored_net > stored_gross and not treat_net_as_per_piece:
            total_net = stored_net
        elif treat_net_as_per_piece:
            total_net = stored_net * stock_dec
        else:
            total_net = stored_net

    return total_gross, total_net


def _bag_received_qty(bag) -> int:
    """Pieces received into this bag (prefer snapshot; else bag_in txns)."""
    if bag is None:
        return 0
    if getattr(bag, "quantity", None) is not None:
        return max(0, int(bag.quantity))
    agg = (
        StockTransaction.objects.filter(bag_id=bag.id, txn_type="bag_in").aggregate(
            s=Sum("quantity")
        )
    )
    return max(0, int(agg.get("s") or 0))


def _bag_tag_count(bag_id: int) -> int:
    if not bag_id:
        return 0
    return ProductTag.objects.filter(grn_bag_id=bag_id, is_active=True).count()


def _bag_gross_weight(bag, item=None, *, allow_item_fallback=True):
    """Bag-level gross; fall back to product item only when allowed."""
    if bag is not None and getattr(bag, "g_wt", None) is not None:
        return Decimal(bag.g_wt)
    if allow_item_fallback and item is not None and item.gross_weight is not None:
        return Decimal(item.gross_weight or 0)
    return Decimal("0")


def _bag_net_weight(bag, item=None, *, allow_item_fallback=True):
    if bag is not None and getattr(bag, "net_wt", None) is not None:
        return Decimal(bag.net_wt)
    if allow_item_fallback and item is not None and item.net_weight is not None:
        return Decimal(item.net_weight or 0)
    return Decimal("0")


def _build_weight_summary(
    *,
    item: ProductItem,
    stock_total: int,
    tags: list,
    metal_total_wt: float,
    stone_total_wt: float,
    stone_total_pcs: int,
    bag=None,
) -> dict:
    """
    Align remaining tags (PCS) with remaining available weights — bag-scoped when bag given.

    Tags remaining = bag (or item) stock minus generated tags (PCS).
    Make Bag stores bag.g_wt as THIS bag's total gross for received qty.
    ProductBOM metal/stone weights are PER PIECE and scale by remaining PCS.
    """
    remaining_qty = max(int(stock_total) - len(tags), 0)
    stock_dec = Decimal(max(stock_total, 0))

    if bag is not None:
        # Never share sibling bags' product_item weight across multi-bag SKUs.
        allow_fallback = True
        if getattr(bag, "g_wt", None) is None and item is not None:
            allow_fallback = (
                GrnBag.objects.filter(product_item_id=item.id).count() <= 1
            )
        stored_gross = _bag_gross_weight(
            bag, item, allow_item_fallback=allow_fallback
        )
        stored_net = _bag_net_weight(
            bag, item, allow_item_fallback=allow_fallback
        )
        if getattr(bag, "net_wt", None) is None and stored_gross > 0 and stored_net <= 0:
            stored_net = stored_gross
    else:
        stored_gross = Decimal(item.gross_weight or 0)
        stored_net = Decimal(item.net_weight or 0)

    tagged_gross = sum(_tag_weight_decimal(t.get("gross_weight")) for t in tags)
    tagged_net = sum(_tag_weight_decimal(t.get("net_weight")) for t in tags)

    if stock_dec > 0:
        # Bag snapshots are always bag-totals for stock_total pieces (not per-piece).
        if bag is not None and getattr(bag, "g_wt", None) is not None:
            total_gross = stored_gross
            total_net = stored_net if stored_net > 0 else stored_gross
        else:
            total_gross, total_net = _resolve_stock_weight_totals(
                stored_gross, stored_net, stock_dec, tags
            )
    else:
        total_gross = tagged_gross
        total_net = tagged_net

    remaining_gross = max(Decimal("0"), total_gross - tagged_gross)
    remaining_net = max(Decimal("0"), total_net - tagged_net)

    # BOM rows are catalog per-piece weights for this product item template.
    metal_per_piece = Decimal(str(metal_total_wt or 0))
    stone_per_piece = Decimal(str(stone_total_wt or 0))
    stone_pcs_per_piece = int(stone_total_pcs or 0)

    metal_total_stock = metal_per_piece * stock_dec
    stone_total_stock = stone_per_piece * stock_dec
    stone_pcs_stock = stone_pcs_per_piece * int(stock_dec)

    remaining_metal = metal_per_piece * Decimal(remaining_qty)
    remaining_stone_wt = stone_per_piece * Decimal(remaining_qty)
    remaining_stone_pcs = stone_pcs_per_piece * remaining_qty

    rem_qty_dec = Decimal(remaining_qty)
    if remaining_qty > 0:
        per_piece_gross_rem = remaining_gross / rem_qty_dec
        per_piece_net_rem = remaining_net / rem_qty_dec
        per_piece_metal_rem = metal_per_piece
        per_piece_stone_wt_rem = stone_per_piece
    else:
        per_piece_gross_rem = Decimal("0")
        per_piece_net_rem = Decimal("0")
        per_piece_metal_rem = Decimal("0")
        per_piece_stone_wt_rem = Decimal("0")

    return {
        "total_gross": _dec(total_gross),
        "tagged_gross": _dec(tagged_gross),
        "remaining_gross": _dec(remaining_gross),
        "total_net": _dec(total_net),
        "tagged_net": _dec(tagged_net),
        "remaining_net": _dec(remaining_net),
        "total_metal_wt": _dec(metal_total_stock),
        "remaining_metal_wt": _dec(remaining_metal),
        "total_stone_wt": _dec(stone_total_stock),
        "remaining_stone_wt": _dec(remaining_stone_wt),
        "total_stone_pcs": stone_pcs_stock,
        "remaining_stone_pcs": remaining_stone_pcs,
        "per_piece_gross": _dec(
            per_piece_gross_rem
            if remaining_qty
            else (stored_gross / stock_dec if stock_dec > 0 else stored_gross)
        ),
        "per_piece_net": _dec(
            per_piece_net_rem
            if remaining_qty
            else (stored_net / stock_dec if stock_dec > 0 and stored_net > 0 else stored_net)
        ),
        "per_piece_metal_wt": _dec(per_piece_metal_rem),
        "per_piece_stone_wt": _dec(per_piece_stone_wt_rem),
    }



WEIGHT_DISPLAY_QUANT = Decimal("0.0001")


def _dec(v):
    """Format weight/decimal for API — max 4 decimal places, trim trailing zeros."""
    if v is None:
        return ""
    try:
        d = Decimal(str(v)).quantize(WEIGHT_DISPLAY_QUANT, rounding=ROUND_HALF_UP)
    except Exception:
        return str(v)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


MAX_TAG_PHOTOS_PER_TAG = 10
ALLOWED_TAG_PHOTO_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}


def _tag_prefix_for_item(item) -> str:
    """SKU pattern_code used as tag prefix (e.g. MCRK-1001 → MCRK-1001-1001)."""
    sku = item.sku if item.sku_id and item.sku else None
    if sku:
        pattern = (sku.pattern_code or "").strip()
        if pattern:
            return pattern
        # Legacy fallback when pattern is not set on the SKU.
        product = (sku.product_code or "").strip()
        if product:
            return product
    return f"ITEM{item.id}"


def _parse_and_validate_tag_weights(gross_raw, less_raw, item, errors: dict):
    """
    Per-piece tag weights: less must not exceed gross or per-piece GRN gross.
    Make Bag stores item.gross_weight as bag TOTAL — divide by stock for the piece ceiling.
    Returns (gross_dec, less_dec, net_dec) or (None, None, None) when errors are appended.
    """
    gross_dec = parse_optional_weight_decimal(gross_raw, "gross_weight", errors)
    less_dec = parse_optional_weight_decimal(less_raw, "less_weight", errors)
    if errors:
        return None, None, None

    gross = gross_dec if gross_dec is not None else Decimal("0")
    less = less_dec if less_dec is not None else Decimal("0")
    grn_total = item.gross_weight if item and item.gross_weight is not None else Decimal("0")
    stock = int(item.qty or 0) if item else 0
    if stock > 1 and grn_total > 0:
        grn = (grn_total / Decimal(stock)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    else:
        grn = grn_total

    if gross <= 0:
        errors.setdefault("gross_weight", []).append(
            "Gross weight must be greater than zero."
        )
    if gross > 0 and less > gross:
        errors.setdefault("less_weight", []).append(
            "Less weight cannot exceed gross weight (Gr. Wt.)."
        )
    if grn > 0 and less > grn:
        errors.setdefault("less_weight", []).append(
            f"Less weight cannot exceed GRN per-piece gross weight ({_dec(grn)} g)."
        )
    net = gross - less
    if net < 0:
        errors.setdefault("less_weight", []).append(
            "Less weight is too high; net weight would be negative."
        )

    if errors:
        return None, None, None
    return gross, less, net


def _build_tag_snapshot(item):
    """Snapshot fields printed on the physical label."""
    sku = item.sku
    metal_info = ""
    bom = item.bom_items.filter(
        material_type="METAL", metal__isnull=False
    ).select_related("metal", "purity").first()
    if bom:
        m = bom.metal.metal_name if bom.metal else ""
        p = bom.purity.purity_name if bom.purity else ""
        metal_info = f"{m} {p}".strip()

    weight_info = ""
    if item.gross_weight is not None:
        weight_info = f"{item.gross_weight}g"

    return {
        "display_name": item.store_variant_name or (
            sku.product_group.style_name if sku and sku.product_group_id else ""
        ),
        "metal_info": metal_info,
        "weight_info": weight_info,
        "sku_code": sku.sku_code if sku else "",
    }


def _photo_to_dict(photo: ProductTagPhoto) -> dict:
    return {
        "id": photo.id,
        "image_url": photo.image_url,
        "uploaded_at": photo.system_created_at.isoformat() if photo.system_created_at else None,
    }


def _photos_by_tag_ids(tag_ids: list[int]) -> dict[int, list[dict]]:
    if not tag_ids:
        return {}
    rows = (
        ProductTagPhoto.objects.filter(product_tag_id__in=tag_ids)
        .order_by("sort_order", "id")
    )
    out: dict[int, list[dict]] = {}
    for photo in rows:
        out.setdefault(photo.product_tag_id, []).append(_photo_to_dict(photo))
    return out


def _tag_to_dict(tag, photos=None):
    if photos is None and hasattr(tag, "_prefetched_photos"):
        photos = [_photo_to_dict(p) for p in tag._prefetched_photos]
    row = {
        "id": tag.id,
        "product_item_id": tag.product_item_id,
        "product_code": (
            tag.product_item.sku.product_code
            if tag.product_item_id and tag.product_item.sku_id
            else ""
        ),
        "tag_type": tag.tag_type,
        "tag_value": tag.tag_value,
        "is_active": tag.is_active,
        "branch_id": tag.branch_id,
        "branch_name": tag.branch_name or (tag.branch.name if tag.branch_id else ""),
        "display_name": tag.display_name or "",
        "metal_info": tag.metal_info or "",
        "gross_weight": tag.gross_weight or "",
        "net_weight": tag.net_weight or "",
        "less_weight": tag.less_weight or "",
        "weight_info": tag.weight_info or "",
        "price_info": tag.price_info or "",
        "price_type": tag.price_type or "",
        "sku_code": tag.sku_code or "",
        "remark": tag.remark or "",
        "huid": tag.huid or "",
        "mapping_status": getattr(tag, "mapping_status", "pending") or "pending",
        "attributes_mapped_at": (
            tag.attributes_mapped_at.isoformat() if getattr(tag, "attributes_mapped_at", None) else None
        ),
        "printed_at": tag.printed_at.isoformat() if tag.printed_at else None,
        "printed_by": tag.printed_by_id,
        "created_at": tag.system_created_at.isoformat() if tag.system_created_at else "",
        "photos": photos if photos is not None else [],
        "tag_metals": [
            {
                "id": tm.id,
                "metal_id": tm.metal_id,
                "purity_id": tm.purity_id,
                "metal_name": tm.metal.metal_name if tm.metal_id and tm.metal else "",
                "purity_name": tm.purity.purity_name if tm.purity_id and tm.purity else "",
                "weight": _dec(tm.weight),
            }
            for tm in (
                tag.tag_metals.select_related("metal", "purity").all()
                if hasattr(tag, "tag_metals")
                else []
            )
        ],
    }
    if tag.product_item_id and getattr(tag, "product_item", None):
        item = tag.product_item
        row.update(serialize_product_item_size_for_api(item))
        grn_total = item.gross_weight if item.gross_weight is not None else Decimal("0")
        stock = int(item.qty or 0)
        if stock > 1 and grn_total > 0:
            grn_pp = (grn_total / Decimal(stock)).quantize(
                WEIGHT_DISPLAY_QUANT, rounding=ROUND_HALF_UP
            )
        else:
            grn_pp = grn_total
        row["grn_per_piece_gross"] = _dec(grn_pp) if grn_pp else ""
    return row


# ---------------------------------------------------------------------------
# Bag list — for the barcode generator landing page
# ---------------------------------------------------------------------------

@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_VIEW")
def barcode_bag_list(request):
    """
    GET /master/barcode/bags/?q=&page=1&page_size=50&open_only=true
    List bags ready for tagging, with bag-wise stock + tag counts.
    When open_only=true (default), fully tagged bags are excluded.
    """
    open_only = str(request.GET.get("open_only", "true")).lower() in ("1", "true", "yes")

    # Tags + stock are bag-scoped (multiple bags can share one product_item).
    active_tag_count_sq = (
        ProductTag.objects.filter(
            grn_bag_id=OuterRef("pk"),
            is_active=True,
        )
        .values("grn_bag_id")
        .annotate(c=Count("id"))
        .values("c")[:1]
    )
    bag_in_qty_sq = (
        StockTransaction.objects.filter(
            bag_id=OuterRef("pk"),
            txn_type="bag_in",
        )
        .values("bag_id")
        .annotate(s=Sum("quantity"))
        .values("s")[:1]
    )

    qs = (
        GrnBag.objects.select_related(
            "lot",
            "lot__batch",
            "product_item",
            "product_item__sku",
            "product_item__sku__product_group",
        )
        .prefetch_related(
            "product_item__bom_items__metal",
            "product_item__bom_items__purity",
        )
        .annotate(
            _tag_count=Coalesce(
                Subquery(active_tag_count_sq, output_field=IntegerField()),
                Value(0),
                output_field=IntegerField(),
            ),
            _stock_qty=Coalesce(
                F("quantity"),
                Subquery(bag_in_qty_sq, output_field=IntegerField()),
                Value(0),
                output_field=IntegerField(),
            ),
        )
        .order_by("-system_created_at")
    )

    if open_only:
        qs = qs.filter(Q(_stock_qty=0) | Q(_tag_count__lt=F("_stock_qty")))

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(bag_no__icontains=q)
            | Q(lot__lot_no__icontains=q)
            | Q(lot__product_code__icontains=q)
            | Q(lot__pattern_code__icontains=q)
            | Q(product_item__sku__product_code__icontains=q)
            | Q(product_item__sku__sku_code__icontains=q)
            | Q(product_item__sku__pattern_code__icontains=q)
        )

    page = max(int(request.GET.get("page", 1)), 1)
    page_size = min(int(request.GET.get("page_size", 50)), 200)
    start = (page - 1) * page_size
    total = qs.count()
    bags = list(qs[start : start + page_size])

    results = []
    for bag in bags:
        item = bag.product_item
        sku = item.sku if item else None
        pg = sku.product_group if sku else None

        metal = ""
        purity = ""
        if item:
            for b in item.bom_items.all():
                if b.material_type == "METAL" and b.metal_id:
                    metal = b.metal.metal_name if b.metal else ""
                    purity = b.purity.purity_name if b.purity_id and b.purity else ""
                    break

        stock = int(getattr(bag, "_stock_qty", None) or _bag_received_qty(bag))
        tags = int(getattr(bag, "_tag_count", None) or 0)
        bag_pcs = bag.pcs if bag.pcs is not None else (bag.quantity if bag.quantity is not None else stock)
        bag_g = bag.g_wt if bag.g_wt is not None else None
        bag_net = bag.net_wt if bag.net_wt is not None else None

        row = {
            "id": bag.id,
            "bag_no": bag.bag_no or "",
            "lot_no": bag.lot.lot_no if bag.lot else "",
            "batch_doc_no": (
                bag.lot.batch.doc_no if bag.lot and bag.lot.batch else ""
            ) or (bag.lot.batch_doc_no if bag.lot else ""),
            "product_item_id": item.id if item else None,
            "product_code": item.sku.product_code if item and item.sku_id else "",
            "sku_code": sku.sku_code if sku else "",
            "style_name": (pg.style_name if pg else "") or "",
            "metal": metal,
            "purity": purity,
            "pcs": str(bag_pcs if bag_pcs is not None else ""),
            "g_wt": _dec(bag_g) if bag_g is not None else "",
            "net_wt": _dec(bag_net) if bag_net is not None else "",
            "stock_qty": stock,
            "tags_created": tags,
            "tags_remaining": max(stock - tags, 0),
            "status": "Tagged" if tags >= stock and stock > 0 else "Open",
        }
        if item:
            row.update(serialize_product_item_size_for_api(item))
        results.append(row)

    return Response({
        "results": results,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


# ---------------------------------------------------------------------------
# Bag detail — full BOM, op charges, stock, existing tags
# ---------------------------------------------------------------------------

@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_VIEW")
def barcode_bag_detail(request, bag_id):
    """
    GET /master/barcode/bag/<bag_id>/
    """
    try:
        bag = (
            GrnBag.objects.select_related(
                "lot",
                "lot__batch",
                "product_item",
                "product_item__sku",
                "product_item__sku__product_group",
                "product_item__sku__color",
                "product_item__sku__hsn",
            )
            .get(pk=bag_id)
        )
    except GrnBag.DoesNotExist:
        return Response({"detail": "Bag not found."}, status=status.HTTP_404_NOT_FOUND)

    item = bag.product_item
    if not item:
        return Response(
            {"detail": "This bag has no product item linked yet."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    sku = item.sku
    pg = sku.product_group if sku else None

    # ── BOM rows ──
    bom_qs = (
        ProductBOM.objects.filter(product=item)
        .select_related(
            "metal",
            "purity",
            "stone",
        )
        .order_by("id")
    )
    bom_rows = []
    metal_total_wt = 0
    stone_total_wt = 0
    stone_total_pcs = 0
    for b in bom_qs:
        if b.material_type == "METAL":
            m = b.metal.metal_name if b.metal else ""
            p = b.purity.purity_name if b.purity else ""
            variant = f"{m} · {p}" if m and p else (m or p or "Metal")
            metal_total_wt += float(b.weight or 0)
            bom_rows.append({
                "id": b.id,
                "material_type": "METAL",
                "variant_name": variant,
                "pcs": b.quantity or 1,
                "weight": _dec(b.weight),
                "metal_id": b.metal_id,
                "purity_id": b.purity_id,
                "metal_name": m,
                "purity_name": p,
            })
        else:  # STONE — master stone only
            s = (b.stone.stone_name or "") if b.stone_id and b.stone else ""
            spec = _stone_master_spec_readonly(b.stone) if b.stone_id and b.stone else ""
            variant = f"{s} · {spec}" if (s and spec) else (s or spec or "Stone")
            stone_total_wt += float(b.weight or 0)
            stone_total_pcs += int(b.quantity or 0)
            row = {
                "id": b.id,
                "material_type": "STONE",
                "variant_name": variant,
                "pcs": b.quantity or 1,
                "weight": _dec(b.weight),
            }
            if s or spec:
                row["stone_name"] = s
                row["stone_variant_name"] = ""
                row["stone_master_spec"] = spec
            bom_rows.append(row)

    # Same shape as Make Bag catalog: charge_type + charge_value; "other"-style
    # component rows merged into one "Other" line with summed numeric values.
    op_rows = _serialize_operation_charges_for_make_bag(
        ProductOperationCharge.objects.filter(product=item).order_by("id")
    )

    # Bag-wise stock (not shared product_item.qty across sibling bags).
    stock_total = _bag_received_qty(bag)

    # Tags generated for THIS bag only.
    tags = list(
        ProductTag.objects.filter(grn_bag_id=bag.id, is_active=True)
        .order_by("-system_created_at", "-id")
        .values(
            "id", "tag_value", "printed_at",
            "display_name", "metal_info",
            "gross_weight", "net_weight", "less_weight",
            "weight_info", "price_info", "price_type", "huid",
            "branch_name", "remark", "mapping_status", "attributes_mapped_at",
        )
    )
    tag_ids = [t["id"] for t in tags]
    photos_map = _photos_by_tag_ids(tag_ids)

    weight_summary = _build_weight_summary(
        item=item,
        stock_total=stock_total,
        tags=tags,
        metal_total_wt=metal_total_wt,
        stone_total_wt=stone_total_wt,
        stone_total_pcs=stone_total_pcs,
        bag=bag,
    )

    bag_gross = _bag_gross_weight(bag, item)
    bag_net = _bag_net_weight(bag, item)
    if bag.net_wt is None and bag.g_wt is not None and bag_net <= 0:
        bag_net = bag_gross

    return Response({
        "bag": {
            "id": bag.id,
            "bag_no": bag.bag_no or "",
            "lot_no": bag.lot.lot_no if bag.lot else "",
            "batch_doc_no": (
                bag.lot.batch.doc_no if bag.lot and bag.lot.batch else ""
            ) or (bag.lot.batch_doc_no if bag.lot else ""),
            "remark": bag.remark or "",
            "quantity": stock_total,
            "pcs": bag.pcs if bag.pcs is not None else stock_total,
            "g_wt": _dec(bag.g_wt) if bag.g_wt is not None else "",
            "stone_wt": _dec(bag.stone_wt) if bag.stone_wt is not None else "",
            "net_wt": _dec(bag.net_wt) if bag.net_wt is not None else "",
        },
        "item": {
            "id": item.id,
            "product_code": item.sku.product_code if item.sku_id else "",
            "store_variant_name": item.store_variant_name or "",
            "sku_code": sku.sku_code if sku else "",
            "sku_id": sku.id if sku else None,
            "style_name": (pg.style_name if pg else "") or "",
            "color": sku.color.label if sku and sku.color_id else "",
            "hsn_code": sku.hsn.hsn_code if sku and sku.hsn_id else "",
            "vendor_name": primary_vendor_name_for_item(
                item, ProductItemLinkedVendor=ProductItemLinkedVendor
            ),
            # Prefer bag snapshot so the detail form / stock bar stay bag-wise.
            "gross_weight": _dec(bag_gross) if bag_gross else _dec(item.gross_weight),
            "net_weight": _dec(bag_net) if bag_net else _dec(item.net_weight),
            **serialize_product_item_size_for_api(item),
        },
        "bom_rows": bom_rows,
        "operational_charges": op_rows,
        "summary": {
            "metal_total_wt": round(metal_total_wt, 4),
            "stone_total_wt": round(stone_total_wt, 4),
            "stone_total_pcs": stone_total_pcs,
        },
        "stock": {
            "total": stock_total,
            "tags_created": len(tags),
            "remaining": max(stock_total - len(tags), 0),
        },
        "weight_summary": weight_summary,
        "tags": [
            {
                "id": t["id"],
                "tag_value": t["tag_value"],
                "printed_at": t["printed_at"].isoformat() if t["printed_at"] else None,
                "display_name": t["display_name"] or "",
                "metal_info": t["metal_info"] or "",
                "gross_weight": t["gross_weight"] or "",
                "net_weight": t["net_weight"] or "",
                "less_weight": t["less_weight"] or "",
                "weight_info": t["weight_info"] or "",
                "price_info": t["price_info"] or "",
                "price_type": t["price_type"] or "",
                "branch_name": t["branch_name"] or "",
                "remark": t["remark"] or "",
                "huid": t.get("huid") or "",
                "mapping_status": t.get("mapping_status") or "pending",
                "attributes_mapped_at": (
                    t["attributes_mapped_at"].isoformat()
                    if t.get("attributes_mapped_at")
                    else None
                ),
                "photos": photos_map.get(t["id"], []),
            }
            for t in tags
        ],
    })


# ---------------------------------------------------------------------------
# Generate one or many barcodes (was tag_generate / tag_bulk_generate)
# ---------------------------------------------------------------------------

def _save_tag_metal_lines(tag, lines, admin):
    """Persist extra metal + purity lines under a tag (not the primary gross)."""
    ProductTagMetal.objects.filter(product_tag=tag).delete()
    if not isinstance(lines, list):
        return
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        try:
            metal_id = int(line.get("metal_id"))
            purity_id = int(line.get("purity_id"))
        except (TypeError, ValueError):
            continue
        errs = {}
        wt = parse_optional_weight_decimal(line.get("weight"), "weight", errs) or Decimal("0")
        if not MetalMasterRule.objects.filter(pk=purity_id, metal_id=metal_id).exists():
            continue
        ProductTagMetal.objects.create(
            product_tag=tag,
            metal_id=metal_id,
            purity_id=purity_id,
            weight=wt,
            sort_order=i,
            created_by=admin,
            updated_by=admin,
        )


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_CREATE")
def barcode_generate(request):
    """
    Generate barcode(s) for a product item.
    POST /master/barcode/generate/
    Body: { product_item_id, quantity?, branch_id?, tag_type?, price_info? }
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    data = request.data or {}
    errors = {}

    item = None
    try:
        item = ProductItem.objects.select_related("sku", "sku__product_group").get(
            pk=int(data.get("product_item_id", 0))
        )
    except (ProductItem.DoesNotExist, TypeError, ValueError):
        errors["product_item_id"] = ["Invalid or missing."]

    branch = None
    branch_id = data.get("branch_id")
    if branch_id not in (None, "", 0):
        try:
            branch = Branch.objects.get(pk=int(branch_id))
        except (Branch.DoesNotExist, TypeError, ValueError):
            errors["branch_id"] = ["Invalid."]

    try:
        quantity = int(data.get("quantity", 1))
    except (TypeError, ValueError):
        quantity = 1
    if quantity < 1:
        quantity = 1

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    tag_type = data.get("tag_type", "barcode")
    if tag_type not in ("barcode", "qr", "rfid"):
        tag_type = "barcode"

    # Per-piece label data from the request (operator enters these on the barcode form)
    gross_wt = (data.get("gross_weight") or "").strip()
    net_wt = (data.get("net_weight") or "").strip()
    less_wt = (data.get("less_weight") or "").strip()
    tag_remark = (data.get("remark") or "").strip()

    weight_errors = {}
    gross_dec, less_dec, net_dec = _parse_and_validate_tag_weights(
        gross_wt, less_wt, item, weight_errors
    )
    if weight_errors:
        return Response({"errors": weight_errors}, status=status.HTTP_400_BAD_REQUEST)

    gross_wt = _dec(gross_dec)
    less_wt = _dec(less_dec)
    net_wt = _dec(net_dec)

    snapshot = _build_tag_snapshot(item)
    branch_display = branch.name if branch else ""

    tag_prefix = _tag_prefix_for_item(item)

    bag = None
    bag_id = data.get("grn_bag_id")
    if bag_id not in (None, "", 0):
        try:
            bag = GrnBag.objects.get(pk=int(bag_id))
        except (GrnBag.DoesNotExist, TypeError, ValueError):
            errors["grn_bag_id"] = ["Invalid."]

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    # Cap generate count by remaining capacity (bag-wise when bag is provided).
    if bag is not None:
        bag_stock = _bag_received_qty(bag)
        existing_tags = _bag_tag_count(bag.id)
        remaining = max(bag_stock - existing_tags, 0)
        if bag_stock > 0 and quantity > remaining:
            return Response(
                {
                    "errors": {
                        "quantity": [
                            f"Only {remaining} tag(s) remaining for this bag "
                            f"(stock {bag_stock}, already tagged {existing_tags})."
                        ]
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        existing_tags = ProductTag.objects.filter(
            product_item=item, is_active=True
        ).count()
        item_stock = int(item.qty or 0)
        remaining = max(item_stock - existing_tags, 0)
        if item_stock > 0 and quantity > remaining:
            return Response(
                {
                    "errors": {
                        "quantity": [
                            f"Only {remaining} tag(s) remaining for this item "
                            f"(stock {item_stock}, already tagged {existing_tags})."
                        ]
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    created = []
    with transaction.atomic():
        tag_values = allocate_tag_values(
            tag_prefix, quantity, admin_user=admin
        )
        for next_tag_value in tag_values:
            tag = ProductTag.objects.create(
                product_item=item,
                tag_type=tag_type,
                tag_value=next_tag_value,
                branch=branch,
                grn_bag=bag,
                mapping_status="pending",
                display_name=snapshot["display_name"],
                metal_info=snapshot["metal_info"],
                gross_weight=gross_wt,
                net_weight=net_wt,
                less_weight=less_wt,
                weight_info=snapshot["weight_info"],
                price_info="",
                sku_code=snapshot["sku_code"],
                branch_name=branch_display,
                remark=tag_remark,
                created_by=admin,
                updated_by=admin,
            )
            _save_tag_metal_lines(tag, data.get("metal_lines"), admin)
            created.append(_tag_to_dict(tag))

    return Response({"results": created}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "PUT"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_UPDATE", "CRM_INVENTORY_BARCODED_UPDATE")
def barcode_update_tag(request, tag_id):
    """
    PATCH /master/barcode/<tag_id>/update/
    Update per-piece weights and label fields on an existing tag (before/after print).
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    tag = _get_active_tag(tag_id)
    if not tag:
        return Response({"detail": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)

    item = tag.product_item
    data = request.data or {}

    gross_raw = data.get("gross_weight", tag.gross_weight)
    less_raw = data.get("less_weight", tag.less_weight)

    weight_errors = {}
    gross_dec, less_dec, net_dec = _parse_and_validate_tag_weights(
        gross_raw, less_raw, item, weight_errors
    )
    if weight_errors:
        return Response({"errors": weight_errors}, status=status.HTTP_400_BAD_REQUEST)

    tag.gross_weight = _dec(gross_dec)
    tag.less_weight = _dec(less_dec)
    tag.net_weight = _dec(net_dec)

    if "remark" in data:
        tag.remark = (data.get("remark") or "").strip()

    tag.updated_by = admin
    tag.save()
    if "metal_lines" in data:
        _save_tag_metal_lines(tag, data.get("metal_lines"), admin)

    return Response(_tag_to_dict(tag))


# ---------------------------------------------------------------------------
# List / mark-printed / deactivate
# ---------------------------------------------------------------------------

@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_VIEW")
def barcode_list(request):
    """
    GET /master/barcode/tags/
    Filters: ?product_item_id=&branch_id=&tag_type=&is_active=true&q=&page=1&page_size=50
    """
    qs = ProductTag.objects.select_related(
        "product_item",
        "product_item__sku",
        "branch",
    ).order_by("-system_created_at")

    item_id = request.GET.get("product_item_id")
    if item_id:
        qs = qs.filter(product_item_id=int(item_id))

    branch_id = request.GET.get("branch_id")
    if branch_id:
        qs = qs.filter(branch_id=int(branch_id))

    tag_type = request.GET.get("tag_type")
    if tag_type:
        qs = qs.filter(tag_type=tag_type)

    is_active = request.GET.get("is_active")
    if is_active is not None:
        qs = qs.filter(is_active=is_active.lower() in ("true", "1"))

    q = (request.GET.get("q") or "").strip()
    if q:
        matching = ProductItem.objects.filter(product_item_search_q(q))
        qs = qs.filter(
            tag_scan_lookup_q(q)
            | Q(product_item__sku__product_code__icontains=q)
            | Q(product_item__in=matching)
        )

    page = max(int(request.GET.get("page", 1)), 1)
    page_size = min(int(request.GET.get("page_size", 50)), 200)
    start = (page - 1) * page_size
    total = qs.count()
    items = list(qs[start : start + page_size])
    photos_map = _photos_by_tag_ids([t.id for t in items])

    return Response({
        "results": [_tag_to_dict(t, photos=photos_map.get(t.id, [])) for t in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_UPDATE")
def barcode_mark_printed(request):
    """
    POST /master/barcode/mark-printed/
    Body: { tag_ids: [1, 2, 3] }
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    tag_ids = (request.data or {}).get("tag_ids", [])
    if not isinstance(tag_ids, list) or not tag_ids:
        return Response(
            {"errors": {"tag_ids": ["Provide a non-empty list."]}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    now = timezone.now()
    updated = ProductTag.objects.filter(
        pk__in=tag_ids, printed_at__isnull=True
    ).update(
        printed_at=now,
        printed_by=admin,
        updated_by=admin,
    )
    return Response({"updated": updated, "printed_at": now.isoformat()})


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_DELETE")
def barcode_deactivate(request, tag_id):
    """
    POST /master/barcode/<tag_id>/deactivate/
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        tag = ProductTag.objects.get(pk=tag_id)
    except ProductTag.DoesNotExist:
        return Response({"detail": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)

    tag.is_active = False
    tag.updated_by = admin
    tag.save()
    return Response({"id": tag.id, "is_active": False, "tag_value": tag.tag_value})


# ---------------------------------------------------------------------------
# Tag photos (physical label images)
# ---------------------------------------------------------------------------


def _get_active_tag(tag_id: int) -> ProductTag | None:
    try:
        return ProductTag.objects.get(pk=tag_id, is_active=True)
    except ProductTag.DoesNotExist:
        return None


@api_view(["GET", "POST"])
@admin_auth()
def barcode_tag_photos(request, tag_id):
    """
    GET  /master/barcode/<tag_id>/photos/  — list photos for a tag.
    POST /master/barcode/<tag_id>/photos/  — upload one or more images (multipart field `images`).
    """
    tag = _get_active_tag(tag_id)
    if not tag:
        return Response({"detail": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_BARCODE_VIEW")
        if denied:
            return denied
        photos = ProductTagPhoto.objects.filter(product_tag=tag).order_by("sort_order", "id")
        return Response({
            "tag_id": tag.id,
            "photos": [_photo_to_dict(p) for p in photos],
        })

    denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_BARCODE_CREATE")
    if denied:
        return denied

    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    uploaded_files = list(request.FILES.getlist("images"))
    if not uploaded_files and request.FILES.get("image"):
        uploaded_files = [request.FILES["image"]]
    if not uploaded_files:
        return Response(
            {"errors": {"images": ["Provide at least one image file (field: images or image)."]}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    existing_count = ProductTagPhoto.objects.filter(product_tag=tag).count()
    if existing_count + len(uploaded_files) > MAX_TAG_PHOTOS_PER_TAG:
        return Response(
            {
                "errors": {
                    "images": [
                        f"Maximum {MAX_TAG_PHOTOS_PER_TAG} photos per tag. "
                        f"Currently {existing_count}, tried to add {len(uploaded_files)}."
                    ]
                }
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    from shared.services.s3_service import upload_file_to_s3, build_public_object_url

    created = []
    next_sort = existing_count
    for file_obj in uploaded_files:
        content_type = (getattr(file_obj, "content_type", None) or "").lower()
        if content_type and content_type not in ALLOWED_TAG_PHOTO_CONTENT_TYPES:
            return Response(
                {"errors": {"images": [f"Unsupported file type: {content_type}"]}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        safe_name = (file_obj.name or "photo").replace(" ", "_")
        object_name = f"Taranya/tags/tag_{tag_id}/{timestamp}_{safe_name}"
        if not upload_file_to_s3(file_obj, object_name):
            return Response(
                {"errors": {"images": [f"Failed to upload: {file_obj.name}"]}},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        image_url = build_public_object_url(object_name)
        photo = ProductTagPhoto.objects.create(
            product_tag=tag,
            image_url=image_url,
            sort_order=next_sort,
            created_by=admin,
            updated_by=admin,
        )
        next_sort += 1
        created.append(_photo_to_dict(photo))

    return Response(
        {
            "tag_id": tag.id,
            "photos": created,
            "message": f"{len(created)} photo(s) uploaded",
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["DELETE"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_DELETE")
def barcode_tag_photo_delete(request, photo_id):
    """
    DELETE /master/barcode/photos/<photo_id>/
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        photo = ProductTagPhoto.objects.select_related("product_tag").get(pk=photo_id)
    except ProductTagPhoto.DoesNotExist:
        return Response({"detail": "Photo not found."}, status=status.HTTP_404_NOT_FOUND)

    if not photo.product_tag.is_active:
        return Response({"detail": "Tag is not active."}, status=status.HTTP_400_BAD_REQUEST)

    photo.delete()
    return Response({"id": photo_id, "deleted": True})


# ---------------------------------------------------------------------------
# Finished Goods inventory dashboard aggregates
# ---------------------------------------------------------------------------

def _parse_weight(raw) -> float:
    if raw in (None, ""):
        return 0.0
    try:
        s = str(raw).strip().lower().replace("g", "").replace(",", "").strip()
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_price(raw) -> float:
    if raw in (None, ""):
        return 0.0
    try:
        s = (
            str(raw)
            .strip()
            .upper()
            .replace("₹", "")
            .replace("RS.", "")
            .replace("RS", "")
            .replace(",", "")
            .strip()
        )
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_purity(metal_info: str) -> str:
    text = (metal_info or "").strip()
    if not text:
        return "(unspecified)"
    upper = text.upper()
    for token in ("22K", "22KT", "18K", "18KT", "14K", "14KT", "24K", "24KT", "PT950", "PT900", "PLATINUM"):
        if token in upper:
            if token.startswith("PT") or token == "PLATINUM":
                return "Platinum"
            return token.replace("KT", "K") if token.endswith("KT") else token
    # "Gold · 22K" / "22 Kt" style
    import re
    m = re.search(r"(\d{2})\s*K(?:T)?", upper)
    if m:
        return f"{m.group(1)}K"
    if "PLAT" in upper:
        return "Platinum"
    return text.split("·")[-1].strip() or text


def _bucket_incr(buckets: dict, key: str, *, pcs=1, g_wt=0.0, n_wt=0.0, value=0.0):
    name = (key or "").strip() or "(unassigned)"
    row = buckets.get(name)
    if not row:
        row = {
            "key": name,
            "label": name,
            "records": 0,
            "pcs": 0,
            "gross_weight": 0.0,
            "net_weight": 0.0,
            "value": 0.0,
        }
        buckets[name] = row
    row["records"] += 1
    row["pcs"] += pcs
    row["gross_weight"] += g_wt
    row["net_weight"] += n_wt
    row["value"] += value


def _buckets_to_sorted_list(buckets: dict, *, limit=None):
    rows = sorted(
        buckets.values(),
        key=lambda r: (-r["records"], r["label"]),
    )
    if limit:
        rows = rows[:limit]
    for r in rows:
        r["gross_weight"] = round(r["gross_weight"], 3)
        r["net_weight"] = round(r["net_weight"], 3)
        r["value"] = round(r["value"], 2)
    return rows


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_BARCODE_VIEW")
def barcode_fg_dashboard(request):
    """
    GET /master/barcode/fg-dashboard/
    Aggregated Finished Goods analytics from active (default) barcode tags.
    Optional: ?is_active=true|false|all&q=&branch_id=
    """
    qs = ProductTag.objects.select_related(
        "product_item",
        "product_item__sku",
        "product_item__sku__product_group",
        "product_item__sku__product_group__category",
        "product_item__sku__product_group__subcategory",
        "branch",
    )

    is_active = request.GET.get("is_active", "true")
    if is_active is not None and str(is_active).lower() not in ("all", ""):
        qs = qs.filter(is_active=str(is_active).lower() in ("true", "1", "yes"))

    branch_id = request.GET.get("branch_id")
    if branch_id:
        try:
            qs = qs.filter(branch_id=int(branch_id))
        except (TypeError, ValueError):
            pass

    q = (request.GET.get("q") or "").strip()
    if q:
        matching = ProductItem.objects.filter(product_item_search_q(q))
        qs = qs.filter(
            tag_scan_lookup_q(q)
            | Q(product_item__sku__product_code__icontains=q)
            | Q(product_item__sku__pattern_code__icontains=q)
            | Q(product_item__sku__sku_code__icontains=q)
            | Q(product_item__in=matching)
        )

    # Cap for safety on very large DBs; dashboard is summary-first.
    tags = list(qs.order_by("-system_created_at")[:20000])

    by_item_group = {}
    by_product_code = {}
    by_pattern_code = {}
    by_sku = {}
    by_purity = {}
    by_category = {}
    by_collection = {}
    by_design_type = {}
    by_qc = {}
    by_barcode_status = {}
    by_location = {}

    total_records = 0
    total_pcs = 0
    total_g_wt = 0.0
    total_n_wt = 0.0
    total_value = 0.0
    barcoded = 0
    non_barcoded = 0  # inactive / missing tag value treated as non-barcoded for chart
    printed = 0
    unprinted = 0
    trend_daily = {}

    for tag in tags:
        sku = tag.product_item.sku if tag.product_item_id and tag.product_item and tag.product_item.sku_id else None
        pg = sku.product_group if sku and sku.product_group_id else None
        item_group = (pg.category.name if pg and pg.category_id and pg.category else "") or "(unassigned)"
        category = item_group
        product_code = ((sku.product_code if sku else "") or "").strip() or "(unassigned)"
        pattern_code = ((sku.pattern_code if sku else "") or "").strip() or "(unassigned)"
        sku_code = ((tag.sku_code or (sku.sku_code if sku else "")) or "").strip() or "(unassigned)"
        purity = _extract_purity(tag.metal_info or "")
        location = (tag.branch_name or (tag.branch.name if tag.branch_id else "") or "").strip() or "(unassigned)"
        # QC not modelled yet — bucket as pending
        qc = "Pending"
        collection = "(unassigned)"
        design_type = (pg.subcategory.name if pg and pg.subcategory_id and pg.subcategory else "") or "(unassigned)"

        g_wt = _parse_weight(tag.gross_weight)
        n_wt = _parse_weight(tag.net_weight)
        value = _parse_price(tag.price_info)
        pcs = 1

        total_records += 1
        total_pcs += pcs
        total_g_wt += g_wt
        total_n_wt += n_wt
        total_value += value

        if tag.is_active and (tag.tag_value or "").strip():
            barcoded += 1
            barcode_status = "Barcoded"
        else:
            non_barcoded += 1
            barcode_status = "Non-Barcoded"

        if tag.printed_at:
            printed += 1
        else:
            unprinted += 1

        created = tag.system_created_at
        if created:
            day = created.date().isoformat()
            trend_daily[day] = trend_daily.get(day, 0) + 1

        _bucket_incr(by_item_group, item_group, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_product_code, product_code, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_pattern_code, pattern_code, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_sku, sku_code, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_purity, purity, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_category, category, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_collection, collection, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_design_type, design_type, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_qc, qc, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_barcode_status, barcode_status, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)
        _bucket_incr(by_location, location, pcs=pcs, g_wt=g_wt, n_wt=n_wt, value=value)

    trend = [
        {"date": d, "count": trend_daily[d]}
        for d in sorted(trend_daily.keys())[-30:]
    ]

    cards = [
        {"id": "item_group", "label": "Item Group", "groups": _buckets_to_sorted_list(by_item_group)},
        {"id": "product_code", "label": "Product Code", "groups": _buckets_to_sorted_list(by_product_code)},
        {"id": "pattern_code", "label": "Pattern Code", "groups": _buckets_to_sorted_list(by_pattern_code)},
        {"id": "sku", "label": "SKU", "groups": _buckets_to_sorted_list(by_sku)},
        {"id": "purity", "label": "Metal Purity", "groups": _buckets_to_sorted_list(by_purity)},
        {"id": "category", "label": "Category", "groups": _buckets_to_sorted_list(by_category)},
        {"id": "collection", "label": "Collection", "groups": _buckets_to_sorted_list(by_collection)},
        {"id": "design_type", "label": "Design Type", "groups": _buckets_to_sorted_list(by_design_type)},
        {"id": "qc_status", "label": "QC Status", "groups": _buckets_to_sorted_list(by_qc)},
        {"id": "barcode_status", "label": "Barcode Status", "groups": _buckets_to_sorted_list(by_barcode_status)},
    ]

    # Card strip summary = rollup of each dimension's unique keys + totals
    card_summaries = []
    for card in cards:
        groups = card["groups"]
        card_summaries.append(
            {
                "id": card["id"],
                "label": card["label"],
                "group_count": len(groups),
                "records": sum(g["records"] for g in groups),
                "pcs": sum(g["pcs"] for g in groups),
                "gross_weight": round(sum(g["gross_weight"] for g in groups), 3),
                "net_weight": round(sum(g["net_weight"] for g in groups), 3),
                "value": round(sum(g["value"] for g in groups), 2),
            }
        )

    return Response(
        {
            "metrics": {
                "total_records": total_records,
                "total_pcs": total_pcs,
                "total_gross_weight": round(total_g_wt, 3),
                "total_net_weight": round(total_n_wt, 3),
                "total_metal_weight": round(total_n_wt, 3),
                "total_diamond_weight": 0.0,
                "total_stone_weight": 0.0,
                "total_value": round(total_value, 2),
                "printed": printed,
                "unprinted": unprinted,
                "barcoded": barcoded,
                "non_barcoded": non_barcoded,
            },
            "cards": card_summaries,
            "groupings": {c["id"]: c["groups"] for c in cards},
            "charts": {
                "distribution_by_item_group": _buckets_to_sorted_list(by_item_group, limit=8),
                "distribution_by_category": _buckets_to_sorted_list(by_category, limit=8),
                "distribution_by_product_code": _buckets_to_sorted_list(by_product_code, limit=8),
                "distribution_by_purity": _buckets_to_sorted_list(by_purity, limit=8),
                "qc_status": _buckets_to_sorted_list(by_qc),
                "barcode_status": [
                    {"key": "Barcoded", "label": "Barcoded", "records": barcoded, "pcs": barcoded},
                    {"key": "Non-Barcoded", "label": "Non-Barcoded", "records": non_barcoded, "pcs": non_barcoded},
                ],
                "purity": _buckets_to_sorted_list(by_purity, limit=8),
            },
            "top": {
                "product_codes": _buckets_to_sorted_list(by_product_code, limit=10),
                "skus": _buckets_to_sorted_list(by_sku, limit=10),
                "patterns": _buckets_to_sorted_list(by_pattern_code, limit=10),
            },
            "trend": trend,
            "locations": _buckets_to_sorted_list(by_location, limit=20),
            "notes": {
                "qc_status": "QC status is not stored on tags yet — shown as Pending.",
                "collection": "Collection mapping is not stored on FG tags yet.",
                "value": "Inventory value uses tag price_info when present.",
            },
        }
    )
