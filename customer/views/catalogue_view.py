"""
Storefront / assisted-selling catalogue APIs (public read models).

Plain Django views + JsonResponse — no DRF serializers.
Reusable for e-commerce and admin POS catalogue.
"""

import math
from decimal import Decimal, InvalidOperation

from django.db.models import (
    DecimalField,
    ExpressionWrapper,
    F,
    Max,
    Min,
    Prefetch,
    Q,
    Value,
)
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.views.decorators.http import require_GET

from shared.models import (
    Category,
    ProductAttribute,
    ProductBOM,
    ProductImage,
    ProductItem,
    ProductItemLinkedVendor,
    ProductOperationCharge,
    ProductStone,
    ProductTag,
    ProductTagPhoto,
)
from shared.product_item_size import format_product_item_size_display
from shared.services.catalogue_availability_service import (
    availability_for_item,
    availability_from_counts,
    resolve_exclude_quote_id,
    reserved_qty_by_product_item,
    reserved_tag_ids,
)
from shared.services.metal_rate_service import get_metal_rate_by_date
from shared.services.pricing_calculation import (
    compute_making_charges_breakdown_for_item,
    compute_operation_charges_breakdown_for_item,
    primary_metal_rate_for_item,
)
from shared.services.product_item_vendors import vendor_variant_name_for_item

TAG_TYPE_BARCODE = "barcode"


def _decimal_to_number(d: Decimal | None) -> float:
    if d is None:
        return 0.0
    return float(d)


def _parse_optional_int(raw):
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _parse_catalogue_path_id(raw) -> int | None:
    """
    Catalogue PDP URLs may use a plain int or a prefixed id (e.g. 'p42' from storefront routes).
    Always resolves to ProductItem primary key when valid.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if len(s) > 1 and s[0].lower() == "p" and s[1:].isdigit():
        return int(s[1:])
    try:
        return int(s)
    except ValueError:
        return None


def _parse_weight_char(raw: str | None) -> Decimal | None:
    """Parse ProductTag snapshot weight fields (CharField) to Decimal."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _barcode_tag_base_qs():
    """
    Physical tagged piece eligible for barcode catalogue mode.

    Includes any active generated barcode tag (Print is optional for shelf labels;
    stock is already on ProductItem after Make Bag). Attribute mapping PENDING
    does not block catalogue visibility.
    """
    return ProductTag.objects.filter(
        is_active=True,
        tag_type=TAG_TYPE_BARCODE,
        product_item_id__isnull=False,
        product_item__sku__product_group__category__is_active=True,
        product_item__sku__product_group__subcategory__is_active=True,
    )


# Back-compat alias (older call sites / comments referred to "printed").
_printed_barcode_tag_base_qs = _barcode_tag_base_qs


def _catalogue_product_items_base_qs():
    """
    Sellable rows used for storefront filters: active category + subcategory.
    """
    return ProductItem.objects.filter(
        sku__isnull=False,
        sku__product_group__category__is_active=True,
        sku__product_group__subcategory__is_active=True,
    )


def _catalogue_virtual_items_qs():
    """ProductItem rows with no active barcode tag (virtual / pre-tag inventory)."""
    tagged_item_ids = _barcode_tag_base_qs().values_list("product_item_id", flat=True).distinct()
    return _catalogue_product_items_base_qs().exclude(id__in=tagged_item_ids)


def _catalogue_items_for_mode(mode: str):
    """ProductItem queryset for filters/listing, depending on catalogue mode."""
    mode = (mode or "virtual").strip().lower()
    if mode not in ("virtual", "all", "barcode"):
        mode = "virtual"
    if mode == "virtual":
        return _catalogue_virtual_items_qs()
    if mode == "barcode":
        return _catalogue_product_items_base_qs().filter(
            id__in=_barcode_tag_base_qs().values_list("product_item_id", flat=True).distinct()
        )
    # all — any sellable line (virtual + tagged parents)
    return _catalogue_product_items_base_qs()


def _category_public_dict(cat: Category) -> dict:
    """Stable storefront shape; extend with image_url when Category gains media."""
    return {
        "id": cat.id,
        "name": cat.name,
        "slug": cat.slug,
        "description": (cat.description or "").strip() or None,
        "image": None,
        "sort_order": cat.sort_order,
    }


def _latest_category_image_map(category_ids: list[int]) -> dict[int, str]:
    """
    Fallback category image from latest sellable ProductItem in each category.
    Preference: primary image first, then newest image.
    """
    if not category_ids:
        return {}
    items = (
        ProductItem.objects.filter(
            sku__isnull=False,
            sku__product_group__category_id__in=category_ids,
            sku__product_group__category__is_active=True,
            sku__product_group__subcategory__is_active=True,
        )
        .select_related("sku__product_group__category")
        .prefetch_related(
            Prefetch(
                "images",
                queryset=ProductImage.objects.order_by("-is_primary", "-id"),
            )
        )
        .order_by("sku__product_group__category_id", "-system_created_at", "-id")
    )

    out: dict[int, str] = {}
    for it in items:
        cat_id = it.sku.product_group.category_id
        if cat_id in out:
            continue
        for img in it.images.all():
            u = (img.image_url or "").strip()
            if u:
                out[cat_id] = u
                break
    return out


@csrf_exempt
@require_GET
def catalogue_categories(request):
    """
    Active product categories for catalogue home, mega-nav, and filters.

    GET /customer/catalogue/categories/

    Response:
        { "categories": [ { "id", "name", "slug", "description", "image", "sort_order" }, ... ] }
    """
    qs = list(Category.objects.filter(is_active=True).order_by("sort_order", "name"))
    image_map = _latest_category_image_map([c.id for c in qs])
    payload = []
    for c in qs:
        row = _category_public_dict(c)
        row["image"] = image_map.get(c.id)
        payload.append(row)
    return JsonResponse({"categories": payload}, status=200)


# Placeholder list price for filters + grid until a real MRP / pricing service exists.
_CATALOGUE_INDICATIVE_RATE_PER_GM = Decimal("6500")
_CATALOGUE_INDICATIVE_BASE_INR = Decimal("1999")

_DEFAULT_PRICE_MIN = 0
_DEFAULT_PRICE_MAX = 2_500_000
CATALOGUE_DEFAULT_LIST_LIMIT = 100


def _cached_metal_rate_per_gm(bom, cache: dict, branch_id=None) -> Decimal:
    """Per-request cache — list view prices one metal rate lookup per metal/purity/branch."""
    metal_id = getattr(bom, "metal_id", None)
    if not metal_id:
        return Decimal("0")
    purity = _bom_purity_label(bom.purity) if getattr(bom, "purity", None) else "24K"
    if not purity:
        purity = "24K"
    bkey = int(branch_id) if branch_id else 0
    rdate = timezone.localdate()
    key = (int(metal_id), purity.upper(), bkey, rdate)
    if key in cache:
        return cache[key]
    row = get_metal_rate_by_date(metal_id, rdate, purity_name=purity, branch_id=branch_id)
    rv = None
    if row:
        rv = getattr(row, "sell_price", None) or getattr(row, "rate_value", None)
    rate = Decimal(str(rv)) if rv else Decimal("0")
    cache[key] = rate
    return rate


def _primary_metal_rate_cached(item, cache: dict, branch_id=None) -> Decimal:
    for bom in item.bom_items.all():
        if (getattr(bom, "material_type", None) or "").strip().upper() != "METAL":
            continue
        rate = _cached_metal_rate_per_gm(bom, cache, branch_id)
        if rate > 0:
            return rate
    return Decimal("0")


def _filter_slider_min(value: float) -> int:
    """Inclusive lower bound for catalogue sliders (floor)."""
    return max(0, math.floor(value))


def _filter_slider_max(value: float) -> int:
    """Inclusive upper bound for catalogue sliders (ceil so edge tags are not dropped)."""
    return max(0, math.ceil(value))


def _annotate_indicative_list_price(qs):
    return qs.annotate(
        _list_price=ExpressionWrapper(
            F("net_weight") * Value(_CATALOGUE_INDICATIVE_RATE_PER_GM)
            + Value(_CATALOGUE_INDICATIVE_BASE_INR),
            output_field=DecimalField(max_digits=16, decimal_places=2),
        )
    )


def _bom_purity_label(purity_obj) -> str:
    if not purity_obj:
        return ""
    return (purity_obj.purity_name or purity_obj.type or "").strip()


def _item_display_metal_purity(item: ProductItem):
    boms = [b for b in item.bom_items.all() if b.material_type == "METAL" and b.metal_id]
    boms.sort(key=lambda b: b.id)
    if not boms:
        return "", ""
    b = boms[0]
    metal = (b.metal.metal_name or "").strip() if b.metal else ""
    purity = _bom_purity_label(b.purity)
    return metal, purity


def _item_primary_stone_label(item: ProductItem) -> str:
    stones = list(item.stones.all())
    stones.sort(key=lambda s: s.id)
    for s in stones:
        if s.stone_id and s.stone and (s.stone.stone_name or "").strip():
            return (s.stone.stone_name or "").strip()
    boms = [b for b in item.bom_items.all() if b.material_type == "STONE"]
    boms.sort(key=lambda b: b.id)
    for b in boms:
        if b.stone_id and b.stone and (b.stone.stone_name or "").strip():
            return (b.stone.stone_name or "").strip()
    return "None"


def _item_primary_image_url(item: ProductItem) -> str:
    imgs = list(item.images.all())
    if not imgs:
        return ""
    imgs.sort(key=lambda i: (not i.is_primary, i.id))
    return (imgs[0].image_url or "").strip()


def _tag_primary_image_url(tag: ProductTag, item: ProductItem) -> str:
    """Barcode shelf piece — prefer photos uploaded on the printed tag."""
    photos = list(tag.photos.all())
    photos.sort(key=lambda p: (p.sort_order, p.id))
    for photo in photos:
        url = (photo.image_url or "").strip()
        if url:
            return url
    return _item_primary_image_url(item)


def _images_for_item_and_tag(item: ProductItem, tag: ProductTag | None = None) -> list[str]:
    images: list[str] = []
    if tag is not None:
        photos = sorted(tag.photos.all(), key=lambda p: (p.sort_order, p.id))
        for photo in photos:
            url = (photo.image_url or "").strip()
            if url:
                images.append(url)
    if images:
        return images
    for img in sorted(item.images.all(), key=lambda x: (not x.is_primary, x.id)):
        url = (img.image_url or "").strip()
        if url:
            images.append(url)
    return images


def _item_sku_product_code(item: ProductItem) -> str:
    """Product code is stored on ProductSKU (not on ProductItem)."""
    sku = getattr(item, "sku", None)
    if sku is None:
        return ""
    code = (getattr(sku, "product_code", None) or "").strip()
    if code:
        return code
    return (getattr(sku, "sku_code", None) or "").strip()


def _item_sku_pattern_code(item: ProductItem) -> str:
    sku = getattr(item, "sku", None)
    if sku is None:
        return ""
    return (getattr(sku, "pattern_code", None) or "").strip()


def _compute_catalogue_final_price(
    item: ProductItem,
    tag: ProductTag | None = None,
    *,
    net_weight_override: Decimal | None = None,
    gross_weight_override: Decimal | None = None,
    rate_cache: dict | None = None,
) -> float:
    """Same final price as product detail (gold rate + making + ops + GST)."""
    if tag is not None:
        net_dec = _parse_weight_char(tag.net_weight) or item.net_weight
        gross_dec = _parse_weight_char(tag.gross_weight) or item.gross_weight
    else:
        net_dec = net_weight_override if net_weight_override is not None else item.net_weight
        gross_dec = (
            gross_weight_override
            if gross_weight_override is not None
            else item.gross_weight
        )

    net_weight = max(0.0, _decimal_to_number(net_dec))
    branch_id = getattr(tag, "branch_id", None) if tag is not None else None
    if rate_cache is not None:
        resolved_gold_rate = _primary_metal_rate_cached(item, rate_cache, branch_id=branch_id)
    else:
        resolved_gold_rate = primary_metal_rate_for_item(item, branch_id=branch_id)
    gold_rate = float(resolved_gold_rate) if resolved_gold_rate > 0 else float(_CATALOGUE_INDICATIVE_RATE_PER_GM)
    has_stone = _item_primary_stone_label(item) != "None"
    stone_price = 3500.0 if has_stone else 0.0
    making_breakdown = compute_making_charges_breakdown_for_item(
        item,
        net_weight_override=net_dec,
        gross_weight_override=gross_dec,
        branch_id=branch_id,
        gold_rate_override=Decimal(str(gold_rate)),
    )
    making = float(making_breakdown["total"])
    operation_breakdown = compute_operation_charges_breakdown_for_item(
        item,
        net_weight_override=net_dec,
        gross_weight_override=gross_dec,
    )
    operation_charges = float(operation_breakdown["total"])
    subtotal = round(gold_rate * net_weight + stone_price + making + operation_charges, 2)
    gst_percent = 3.0
    gst_amount = round(subtotal * (gst_percent / 100.0), 2)
    return round(subtotal + gst_amount, 2)


def _item_to_product_dict(
    item: ProductItem,
    *,
    row_type: str = "virtual",
    catalogue_id: int | None = None,
    product_tag_id: int | None = None,
    net_weight_override: Decimal | None = None,
    gross_weight_override: Decimal | None = None,
    design_code_override: str | None = None,
    name_override: str | None = None,
    rate_cache: dict | None = None,
) -> dict:
    group = item.sku.product_group
    category = group.category
    metal, purity = _item_display_metal_purity(item)
    net_dec = net_weight_override if net_weight_override is not None else item.net_weight
    gross_dec = gross_weight_override if gross_weight_override is not None else item.gross_weight
    starting = int(round(_compute_catalogue_final_price(
        item,
        net_weight_override=net_weight_override,
        gross_weight_override=gross_weight_override,
        rate_cache=rate_cache,
    )))
    name = (
        name_override
        if name_override is not None
        else (item.store_variant_name or "").strip() or (group.style_name or "").strip() or _item_sku_product_code(item)
    )
    design = (
        design_code_override
        if design_code_override is not None
        else _item_sku_product_code(item)
    )
    cid = catalogue_id if catalogue_id is not None else item.id
    out = {
        "id": cid,
        "rowType": row_type,
        "productItemId": item.id,
        "name": name,
        "designCode": design,
        "categoryId": category.id,
        "categoryName": category.name or "",
        "metal": metal,
        "purity": purity,
        "weightGm": round(_decimal_to_number(net_dec), 3),
        "grossWeightGm": round(_decimal_to_number(gross_dec), 3),
        "stoneType": _item_primary_stone_label(item),
        "patternCode": _item_sku_pattern_code(item) or None,
        "startingPrice": max(0, starting),
        "image": _item_primary_image_url(item),
        "systemUpdatedAt": item.system_updated_at.isoformat()
        if getattr(item, "system_updated_at", None)
        else None,
    }
    if product_tag_id is not None:
        out["productTagId"] = product_tag_id
    return out


def _attach_catalogue_availability(
    products: list[dict],
    *,
    exclude_quote_id: int | None,
) -> None:
    """Mutate listing rows in place with stock / reservation fields."""
    if not products:
        return
    item_ids: list[int] = []
    tag_ids: list[int] = []
    for row in products:
        iid = row.get("productItemId")
        if iid is not None:
            try:
                item_ids.append(int(iid))
            except (TypeError, ValueError):
                pass
        tid = row.get("productTagId")
        if tid is not None:
            try:
                tag_ids.append(int(tid))
            except (TypeError, ValueError):
                pass
    if not item_ids:
        return
    reserved_map = reserved_qty_by_product_item(item_ids, exclude_quote_id=exclude_quote_id)
    tag_reserved = reserved_tag_ids(tag_ids, exclude_quote_id=exclude_quote_id) if tag_ids else set()
    qty_map = dict(
        ProductItem.objects.filter(id__in=item_ids).values_list("id", "qty")
    )
    for row in products:
        try:
            iid = int(row.get("productItemId"))
        except (TypeError, ValueError):
            continue
        stock_qty = max(0, int(qty_map.get(iid) or 0))
        reserved_qty = reserved_map.get(iid, 0)
        tag_id = row.get("productTagId")
        tid = int(tag_id) if tag_id is not None else None
        row.update(
            availability_from_counts(
                stock_qty,
                reserved_qty,
                tag_id=tid,
                tag_reserved=tag_reserved,
            )
        )


def _variant_options_for_item(item: ProductItem) -> dict:
    metals = []
    purities = []
    stone_types = []
    sizes = []

    for b in sorted(item.bom_items.all(), key=lambda x: x.id):
        if b.material_type == "METAL":
            if b.metal and (b.metal.metal_name or "").strip():
                metals.append((b.metal.metal_name or "").strip())
            p = _bom_purity_label(b.purity)
            if p:
                purities.append(p)
        elif b.material_type == "STONE":
            if b.stone and (b.stone.stone_name or "").strip():
                stone_types.append((b.stone.stone_name or "").strip())

    for s in sorted(item.stones.all(), key=lambda x: x.id):
        if s.stone and (s.stone.stone_name or "").strip():
            stone_types.append((s.stone.stone_name or "").strip())

    sz = (format_product_item_size_display(item) or "").strip()
    if sz and sz != "—":
        sizes.append(sz)

    def _uniq(values, fallback):
        seen = set()
        out = []
        for v in values:
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        if not out and fallback:
            out = [fallback]
        return [{"value": x, "label": x} for x in out]

    metal_fallback, purity_fallback = _item_display_metal_purity(item)
    stone_fallback = _item_primary_stone_label(item)
    return {
        "metal": _uniq(metals, metal_fallback),
        "purity": _uniq(purities, purity_fallback),
        "stoneType": _uniq(stone_types, stone_fallback or "None"),
        "size": _uniq(sizes, "Standard"),
    }


def _item_to_product_detail_dict(
    item: ProductItem,
    tag: ProductTag | None = None,
    *,
    exclude_quote_id: int | None = None,
) -> dict:
    group = item.sku.product_group
    category = group.category
    images = _images_for_item_and_tag(item, tag)

    if tag:
        net_dec = _parse_weight_char(tag.net_weight) or item.net_weight
        gross_dec = _parse_weight_char(tag.gross_weight) or item.gross_weight
        design = (tag.tag_value or "").strip() or _item_sku_product_code(item)
        title = (tag.display_name or "").strip() or (item.store_variant_name or "").strip() or design
    else:
        net_dec = item.net_weight
        gross_dec = item.gross_weight
        design = _item_sku_product_code(item)
        title = (item.store_variant_name or "").strip() or (group.style_name or "").strip() or design

    net_weight = max(0.0, _decimal_to_number(net_dec))
    gross_weight = max(0.0, _decimal_to_number(gross_dec))
    final_price = _compute_catalogue_final_price(
        item,
        tag,
        net_weight_override=net_dec,
        gross_weight_override=gross_dec,
    )
    branch_id_val = getattr(tag, "branch_id", None) if tag is not None else None
    resolved_rate = primary_metal_rate_for_item(item, branch_id=branch_id_val)
    gold_rate = float(resolved_rate) if resolved_rate > 0 else float(_CATALOGUE_INDICATIVE_RATE_PER_GM)
    has_stone = _item_primary_stone_label(item) != "None"
    stone_price = 3500.0 if has_stone else 0.0
    making_breakdown = compute_making_charges_breakdown_for_item(
        item,
        net_weight_override=net_dec,
        gross_weight_override=gross_dec,
        branch_id=branch_id_val,
        gold_rate_override=Decimal(str(gold_rate)),
    )
    making = float(making_breakdown["total"])
    operation_breakdown = compute_operation_charges_breakdown_for_item(
        item,
        net_weight_override=net_dec,
        gross_weight_override=gross_dec,
    )
    operation_charges = float(operation_breakdown["total"])
    subtotal = round(gold_rate * net_weight + stone_price + making + operation_charges, 2)
    gst_percent = 3.0
    gst_amount = round(subtotal * (gst_percent / 100.0), 2)

    out = {
        "id": item.id,
        "name": title,
        "designCode": design,
        "categoryId": category.id,
        "categoryName": category.name or "",
        "description": (group.description or "").strip() or "Curated jewellery design from our catalogue.",
        "patternCode": _item_sku_pattern_code(item) or None,
        "images": images,
        "variantOptions": _variant_options_for_item(item),
        "defaultPricing": {
            "goldRatePerGm": gold_rate,
            "netGoldWeightGm": round(net_weight, 3),
            "grossGoldWeightGm": round(gross_weight, 3),
            "stonePrice": stone_price,
            "makingCharges": making,
            "operationCharges": operation_charges,
            "gstPercent": gst_percent,
            "subtotal": subtotal,
            "gstAmount": gst_amount,
            "finalPrice": final_price,
            "chargeApply": (getattr(item, "charge_apply", None) or "").strip() or "net_wt",
        },
        "pricingBreakdown": {
            "makingCharges": {
                "total": str(making_breakdown["total"]),
                "lines": making_breakdown["lines"],
            },
            "operationCharges": {
                "total": str(operation_breakdown["total"]),
                "lines": operation_breakdown["lines"],
            },
            "totals": {
                "goldValue": round(gold_rate * net_weight, 2),
                "stoneValue": round(stone_price, 2),
                "makingCharges": making,
                "operationCharges": operation_charges,
                "subtotal": subtotal,
                "gstPercent": gst_percent,
                "gstAmount": gst_amount,
                "finalPrice": final_price,
            },
        },
        "comboPricing": {},
    }
    if tag:
        out["rowType"] = "barcode"
        out["productTagId"] = tag.id
        out["productItemId"] = item.id
    else:
        out["rowType"] = "virtual"
        out["productItemId"] = item.id

    reserved_map = reserved_qty_by_product_item([item.id], exclude_quote_id=exclude_quote_id)
    tag_reserved = (
        reserved_tag_ids([tag.id], exclude_quote_id=exclude_quote_id) if tag else set()
    )
    out.update(
        availability_for_item(
            item,
            reserved_map,
            tag_id=tag.id if tag else None,
            tag_reserved=tag_reserved,
        )
    )
    return out


def _build_collections(limit_products_per_collection: int = 12):
    items = (
        _catalogue_virtual_items_qs()
        .select_related("sku__product_group__category")
        .order_by("-system_updated_at", "id")
    )
    product_ids_by_category = {}
    for it in items:
        cat = it.sku.product_group.category
        if not cat:
            continue
        key = cat.id
        if key not in product_ids_by_category:
            product_ids_by_category[key] = []
        if len(product_ids_by_category[key]) < limit_products_per_collection:
            product_ids_by_category[key].append(str(it.id))

    if not product_ids_by_category:
        return []

    categories = Category.objects.filter(id__in=product_ids_by_category.keys(), is_active=True).order_by(
        "sort_order", "name"
    )
    out = []
    for cat in categories:
        product_ids = product_ids_by_category.get(cat.id, [])
        if not product_ids:
            continue
        out.append(
            {
                "id": f"cat-{cat.id}",
                "name": f"{cat.name} Edit",
                "tagline": f"Top picks in {cat.name}",
                "productIds": product_ids,
            }
        )
    return out


@csrf_exempt
@require_GET
def catalogue_filters(request):
    """
    Facets + slider bounds for catalogue product listing.

    GET /customer/catalogue/filters/

    Query:
        mode — virtual | barcode | all (same semantics as /catalogue/products/)

    Response:
        {
          "categories": [ { "id", "label" }, ... ],
          "metals": [ str, ... ],
          "purities": [ str, ... ],
          "stoneTypes": [ str, ... ],
          "weightRange": { "min": number, "max": number },
          "sizes": [ str, ... ],
          "patterns": [ str, ... ],
          "priceRange": { "min": number, "max": number }
        }

    priceRange is a wide default band; refine when product list returns comparable prices.
    """
    mode = (request.GET.get("mode") or "virtual").strip().lower()
    if mode not in ("virtual", "all", "barcode"):
        mode = "virtual"

    items_qs = _catalogue_items_for_mode(mode)
    item_ids = list(items_qs.values_list("id", flat=True))

    if not item_ids:
        return JsonResponse(
            {
                "categories": [],
                "metals": [],
                "purities": [],
                "stoneTypes": [],
                "stoneTypeOptions": [],
                "puritiesByMetal": {},
                "weightRange": {"min": 0, "max": 1},
                "sizes": [],
                "priceRange": {"min": _DEFAULT_PRICE_MIN, "max": _DEFAULT_PRICE_MAX},
                "patterns": [],
                "meta": {"mode": mode},
            },
            status=200,
        )

    cat_ids = items_qs.values_list("sku__product_group__category_id", flat=True).distinct()
    categories_qs = (
        Category.objects.filter(id__in=cat_ids, is_active=True)
        .order_by("sort_order", "name")
    )
    categories_payload = [{"id": c.id, "label": c.name} for c in categories_qs]

    metal_qs = ProductBOM.objects.filter(
        product_id__in=item_ids,
        material_type="METAL",
        metal_id__isnull=False,
    )
    metals_set = {
        str(n).strip()
        for n in metal_qs.values_list("metal__metal_name", flat=True).distinct()
        if n and str(n).strip()
    }

    purities_set = set()
    purities_by_metal: dict[str, set[str]] = {}
    for metal_name, purity_name, purity_type in (
        metal_qs.filter(purity_id__isnull=False)
        .values_list("metal__metal_name", "purity__purity_name", "purity__type")
        .distinct()
    ):
        metal_label = (metal_name or "").strip()
        purity_label = (purity_name or purity_type or "").strip() if (purity_name or purity_type) else ""
        if purity_label:
            purities_set.add(purity_label)
            if metal_label:
                purities_by_metal.setdefault(metal_label, set()).add(purity_label)

    stone_type_map: dict[str, str] = {}
    stone_name_qs = (
        ProductStone.objects.filter(product_id__in=item_ids, stone_id__isnull=False)
        .values_list("stone__stone_name", "stone__stone_type__name")
        .distinct()
    )
    for stone_name, stone_type_name in stone_name_qs:
        name = (stone_name or "").strip()
        if not name:
            continue
        type_label = (stone_type_name or "").strip() or "Other"
        stone_type_map[name] = type_label
    for stone_name, stone_type_name in (
        ProductBOM.objects.filter(
            product_id__in=item_ids,
            material_type="STONE",
            stone_id__isnull=False,
        )
        .values_list("stone__stone_name", "stone__stone_type__name")
        .distinct()
    ):
        name = (stone_name or "").strip()
        if not name:
            continue
        type_label = (stone_type_name or "").strip() or "Other"
        stone_type_map.setdefault(name, type_label)

    stone_types = sorted(stone_type_map.keys(), key=lambda s: (stone_type_map[s].lower(), s.lower()))
    stone_type_options = [
        {"value": name, "label": name, "type": stone_type_map[name]}
        for name in stone_types
    ]

    w_agg = items_qs.aggregate(wmin=Min("net_weight"), wmax=Max("net_weight"))
    wmin = _filter_slider_min(_decimal_to_number(w_agg["wmin"]))
    wmax = _filter_slider_max(_decimal_to_number(w_agg["wmax"]))
    if wmax <= wmin:
        wmax = wmin + 1

    if mode == "barcode":
        tag_nets: list[float] = []
        tag_prices: list[float] = []
        for tag in (
            _printed_barcode_tag_base_qs()
            .filter(product_item_id__in=item_ids)
            .select_related("product_item")
        ):
            parsed = _parse_weight_char(tag.net_weight)
            if parsed is not None:
                tag_nets.append(float(parsed))
            tag_prices.append(float(_tag_list_price_value(tag, tag.product_item)))
        if tag_nets:
            twmin = _filter_slider_min(min(tag_nets))
            twmax = _filter_slider_max(max(tag_nets))
            wmin = min(wmin, twmin)
            wmax = max(wmax, twmax)
            if wmax <= wmin:
                wmax = wmin + 1

    size_displays = []
    for it in items_qs.select_related("sku"):
        d = (format_product_item_size_display(it) or "").strip()
        if d and d != "—":
            size_displays.append(d)
    sizes = sorted(set(size_displays), key=lambda s: (len(s), s.lower()))

    pattern_codes = sorted(
        {
            (pc or "").strip()
            for pc in items_qs.values_list("sku__pattern_code", flat=True).distinct()
            if pc and str(pc).strip()
        },
        key=str.lower,
    )

    priced = _annotate_indicative_list_price(items_qs)
    p_agg = priced.aggregate(pmin=Min("_list_price"), pmax=Max("_list_price"))
    pr_min = (
        _filter_slider_min(_decimal_to_number(p_agg["pmin"]))
        if p_agg["pmin"] is not None
        else _DEFAULT_PRICE_MIN
    )
    pr_max = (
        _filter_slider_max(_decimal_to_number(p_agg["pmax"]))
        if p_agg["pmax"] is not None
        else _DEFAULT_PRICE_MAX
    )
    if pr_max <= pr_min:
        pr_max = pr_min + 1

    if mode == "barcode" and tag_prices:
        pr_min = _filter_slider_min(min(tag_prices))
        pr_max = _filter_slider_max(max(tag_prices))
        if pr_max <= pr_min:
            pr_max = pr_min + 1

    payload = {
        "categories": categories_payload,
        "metals": sorted(metals_set, key=str.lower),
        "purities": sorted(purities_set, key=str.lower),
        "puritiesByMetal": {
            metal: sorted(labels, key=str.lower)
            for metal, labels in purities_by_metal.items()
        },
        "stoneTypes": stone_types,
        "stoneTypeOptions": stone_type_options,
        "weightRange": {"min": wmin, "max": wmax},
        "sizes": sizes,
        "patterns": pattern_codes,
        "priceRange": {"min": pr_min, "max": pr_max},
        "meta": {"mode": mode},
    }
    return JsonResponse(payload, status=200)


def _item_catalogue_prefetch():
    return (
        Prefetch(
            "bom_items",
            queryset=ProductBOM.objects.select_related(
                "metal",
                "purity",
                "stone",
            )
            .prefetch_related(
                Prefetch(
                    "attributes",
                    queryset=ProductAttribute.objects.select_related("charge_type").order_by("id"),
                )
            )
            .order_by("id"),
        ),
        Prefetch(
            "stones",
            queryset=ProductStone.objects.select_related("stone").order_by("id"),
        ),
        Prefetch(
            "images",
            queryset=ProductImage.objects.order_by("-is_primary", "id"),
        ),
        Prefetch(
            "operation_charges",
            queryset=ProductOperationCharge.objects.order_by("id"),
        ),
    )


def _apply_product_item_filters(request, qs, *, skip_text_search: bool = False):
    cid = request.GET.get("category_id") or request.GET.get("category")
    if cid:
        icid = _parse_optional_int(cid)
        if icid is not None:
            qs = qs.filter(sku__product_group__category_id=icid)

    pattern = (request.GET.get("pattern") or request.GET.get("pattern_code") or "").strip()
    if pattern:
        qs = qs.filter(sku__pattern_code__iexact=pattern)

    if not skip_text_search:
        q = (request.GET.get("q") or request.GET.get("search") or "").strip()
        if q:
            qs = qs.filter(
                Q(sku__product_code__icontains=q)
                | Q(sku__pattern_code__icontains=q)
                | Q(sku__sku_code__icontains=q)
                | Q(store_variant_name__icontains=q)
                | Q(sku__product_group__style_name__icontains=q)
            )

    metal = (request.GET.get("metal") or "").strip()
    if metal:
        qs = qs.filter(
            bom_items__material_type="METAL",
            bom_items__metal__metal_name__iexact=metal,
        ).distinct()

    purity = (request.GET.get("purity") or "").strip()
    if purity:
        qs = qs.filter(
            Q(bom_items__material_type="METAL", bom_items__purity__purity_name__iexact=purity)
            | Q(bom_items__material_type="METAL", bom_items__purity__type__iexact=purity)
        ).distinct()

    stone_type = (
        request.GET.get("stone_type") or request.GET.get("stoneType") or ""
    ).strip()
    if stone_type:
        qs = qs.filter(
            Q(stones__stone__stone_name__iexact=stone_type)
            | Q(
                bom_items__material_type="STONE",
                bom_items__stone__stone_name__iexact=stone_type,
            )
        ).distinct()

    min_w = _parse_optional_int(request.GET.get("min_weight") or request.GET.get("minWeight"))
    max_w = _parse_optional_int(request.GET.get("max_weight") or request.GET.get("maxWeight"))
    if min_w is not None:
        qs = qs.filter(net_weight__gte=min_w)
    if max_w is not None:
        qs = qs.filter(net_weight__lte=max_w)

    size = (request.GET.get("size") or "").strip()
    if size:
        low = size.lower()
        if low.startswith("size "):
            try:
                n = int(size.split()[1])
                qs = qs.filter(size_number=n)
            except (IndexError, ValueError):
                qs = qs.none()

    qs = _annotate_indicative_list_price(qs)

    min_p = _parse_optional_int(request.GET.get("min_price") or request.GET.get("minPrice"))
    max_p = _parse_optional_int(request.GET.get("max_price") or request.GET.get("maxPrice"))
    if min_p is not None:
        qs = qs.filter(_list_price__gte=min_p)
    if max_p is not None:
        qs = qs.filter(_list_price__lte=max_p)

    return qs


def _post_filter_items_by_size_display(items: list, size: str) -> list:
    s = (size or "").strip()
    if not s:
        return items
    out = []
    for it in items:
        if (format_product_item_size_display(it) or "").strip() == s:
            out.append(it)
    return out


def _tag_net_weight_dec(tag: ProductTag, item: ProductItem) -> Decimal:
    parsed = _parse_weight_char(tag.net_weight)
    if parsed is not None:
        return parsed
    return item.net_weight


def _tag_gross_weight_dec(tag: ProductTag, item: ProductItem) -> Decimal:
    parsed = _parse_weight_char(tag.gross_weight)
    if parsed is not None:
        return parsed
    return item.gross_weight


def _tag_list_price_value(tag: ProductTag, item: ProductItem, rate_cache: dict | None = None) -> Decimal:
    return Decimal(str(_compute_catalogue_final_price(item, tag, rate_cache=rate_cache)))


def _tag_matches_text_search(tag: ProductTag, item: ProductItem, q: str) -> bool:
    """Barcode catalogue: scanned tag id, tag_value, display_name, or item codes."""
    s = (q or "").strip()
    if not s:
        return True
    sl = s.lower()
    if s.isdigit() and tag.pk == int(s):
        return True
    if (tag.tag_value or "").lower().find(sl) >= 0:
        return True
    if (tag.display_name or "").lower().find(sl) >= 0:
        return True
    if (tag.sku_code or "").lower().find(sl) >= 0:
        return True
    pc = (_item_sku_product_code(item) or "").lower()
    if pc and pc.find(sl) >= 0:
        return True
    pattern = (_item_sku_pattern_code(item) or "").lower()
    if pattern and pattern.find(sl) >= 0:
        return True
    name = (item.store_variant_name or "").strip().lower()
    if name and name.find(sl) >= 0:
        return True
    style = ""
    if item.sku_id and item.sku.product_group_id:
        style = (item.sku.product_group.style_name or "").strip().lower()
    return bool(style and style.find(sl) >= 0)


def _filter_tags_by_text_search(tags: list, request) -> list:
    q = (request.GET.get("q") or request.GET.get("search") or "").strip()
    if not q:
        return tags
    return [t for t in tags if _tag_matches_text_search(t, t.product_item, q)]


def _filter_tag_rows_post(tags: list, request, rate_cache: dict | None = None) -> list:
    min_w = _parse_optional_int(request.GET.get("min_weight") or request.GET.get("minWeight"))
    max_w = _parse_optional_int(request.GET.get("max_weight") or request.GET.get("maxWeight"))
    min_p = _parse_optional_int(request.GET.get("min_price") or request.GET.get("minPrice"))
    max_p = _parse_optional_int(request.GET.get("max_price") or request.GET.get("maxPrice"))
    out = []
    for tag in tags:
        item = tag.product_item
        nw = float(_tag_net_weight_dec(tag, item))
        if min_w is not None and nw < min_w:
            continue
        if max_w is not None and nw > max_w:
            continue
        lp = float(_tag_list_price_value(tag, item, rate_cache=rate_cache))
        if min_p is not None and lp < min_p:
            continue
        if max_p is not None and lp > max_p:
            continue
        out.append(tag)
    return out


@csrf_exempt
@require_GET
def catalogue_products(request):
    """
    Catalogue product cards.

    GET /customer/catalogue/products/

    Query (e-commerce friendly; snake_case and camelCase aliases where noted):
        mode          — virtual | barcode | all
                        virtual: ProductItem rows with no active barcode tag (weights from product_items).
                        barcode: one row per generated ProductTag (weights from product_tags snapshot).
                        all: union of virtual rows + barcode rows.
        category_id   — or category
        q, search     — design / product / pattern code, item name, style name (barcode: also tag_value, display_name)
        pattern, pattern_code — exact pattern code (e.g. FMC, CFY)
        metal, purity, stone_type / stoneType
        min_weight, minWeight / max_weight, maxWeight  — net weight (grams); barcode uses tag snapshot with item fallback
        min_price, minPrice / max_price, maxPrice     — indicative list price
        size          — structured size display string (see /catalogue/filters sizes)
        limit         — max rows (1–200), optional

    Response rows include:
        rowType       — "virtual" | "barcode"
        productItemId — ProductItem pk (always)
        productTagId  — present when rowType is barcode (use with product detail ?tagId=)
        id            — virtual: productItemId; barcode: productTagId (unique per card)
        patternCode   — SKU pattern_code when set
        grossWeightGm — from product_items or product_tags depending on rowType

    startingPrice uses the same calculation as product detail (gold rate + making + ops + GST).
    """
    mode = (request.GET.get("mode") or "virtual").strip().lower()
    if mode not in ("virtual", "all", "barcode"):
        mode = "virtual"

    limit_raw = request.GET.get("limit")
    limit = CATALOGUE_DEFAULT_LIST_LIMIT
    if limit_raw is not None and str(limit_raw).strip() != "":
        try:
            limit = min(max(int(str(limit_raw).strip()), 1), 200)
        except ValueError:
            limit = CATALOGUE_DEFAULT_LIST_LIMIT

    rate_cache: dict = {}
    size_param = (request.GET.get("size") or "").strip()

    def _fetch_items(qs, eff_limit):
        q2 = (
            qs.select_related("sku__product_group__category")
            .prefetch_related(*_item_catalogue_prefetch())
            .order_by("-system_updated_at", "id")
        )
        if eff_limit is not None:
            q2 = q2[: eff_limit * 3 if size_param and not size_param.lower().startswith("size ") else eff_limit]
        rows = list(q2)
        if size_param and not size_param.lower().startswith("size "):
            rows = _post_filter_items_by_size_display(rows, size_param)
        if eff_limit is not None:
            rows = rows[:eff_limit]
        return rows

    products = []

    if mode in ("virtual", "all"):
        v_eff = (limit * 2) if (mode == "all" and limit) else limit
        vqs = _apply_product_item_filters(request, _catalogue_virtual_items_qs())
        vrows = _fetch_items(vqs, v_eff)
        for it in vrows:
            products.append(_item_to_product_dict(it, row_type="virtual", rate_cache=rate_cache))

    if mode in ("barcode", "all"):
        tagged_parent_qs = _catalogue_product_items_base_qs().filter(
            id__in=_printed_barcode_tag_base_qs().values_list("product_item_id", flat=True).distinct()
        )
        tagged_parent_qs = _apply_product_item_filters(request, tagged_parent_qs, skip_text_search=True)
        item_ids = list(tagged_parent_qs.values_list("id", flat=True))
        tqs = (
            _printed_barcode_tag_base_qs()
            .filter(product_item_id__in=item_ids)
            .select_related("product_item__sku__product_group__category")
            .prefetch_related(
                Prefetch(
                    "photos",
                    queryset=ProductTagPhoto.objects.order_by("sort_order", "id"),
                ),
                Prefetch(
                    "product_item__bom_items",
                    queryset=ProductBOM.objects.select_related(
                        "metal",
                        "purity",
                        "stone",
                    )
                    .prefetch_related(
                        Prefetch(
                            "attributes",
                            queryset=ProductAttribute.objects.select_related("charge_type").order_by("id"),
                        )
                    )
                    .order_by("id"),
                ),
                Prefetch(
                    "product_item__stones",
                    queryset=ProductStone.objects.select_related("stone").order_by("id"),
                ),
                Prefetch(
                    "product_item__operation_charges",
                    queryset=ProductOperationCharge.objects.order_by("id"),
                ),
                Prefetch(
                    "product_item__images",
                    queryset=ProductImage.objects.order_by("-is_primary", "id"),
                ),
            )
            .order_by("-system_updated_at", "id")
        )
        t_eff = (limit * 2) if (mode == "all" and limit) else limit
        if t_eff is not None:
            tqs = tqs[: t_eff * 4 if (size_param or request.GET.get("min_weight")) else t_eff]
        tags = list(tqs)
        tags = _filter_tags_by_text_search(tags, request)
        tags = _filter_tag_rows_post(tags, request, rate_cache=rate_cache)
        if size_param:
            tags = [
                t
                for t in tags
                if (format_product_item_size_display(t.product_item) or "").strip() == size_param
            ]
        if t_eff is not None:
            tags = tags[:t_eff]

        for tag in tags:
            it = tag.product_item
            net_dec = _tag_net_weight_dec(tag, it)
            gross_dec = _tag_gross_weight_dec(tag, it)
            name = (tag.display_name or "").strip() or (it.store_variant_name or "").strip() or (
                it.sku.product_group.style_name or ""
            ).strip() or (tag.tag_value or "")
            design = (tag.tag_value or "").strip() or _item_sku_product_code(it)
            list_px = _tag_list_price_value(tag, it, rate_cache=rate_cache)
            starting = int(round(float(list_px)))
            metal, purity = _item_display_metal_purity(it)
            group = it.sku.product_group
            category = group.category
            products.append(
                {
                    "id": tag.id,
                    "rowType": "barcode",
                    "productItemId": it.id,
                    "productTagId": tag.id,
                    "name": name,
                    "designCode": design,
                    "categoryId": category.id,
                    "categoryName": category.name or "",
                    "metal": metal,
                    "purity": purity,
                    "weightGm": round(_decimal_to_number(net_dec), 3),
                    "grossWeightGm": round(_decimal_to_number(gross_dec), 3),
                    "stoneType": _item_primary_stone_label(it),
                    "patternCode": _item_sku_pattern_code(it) or None,
                    "startingPrice": max(0, starting),
                    "image": _tag_primary_image_url(tag, it),
                    "systemUpdatedAt": tag.system_updated_at.isoformat()
                    if getattr(tag, "system_updated_at", None)
                    else None,
                }
            )

    if mode == "all":
        products.sort(key=lambda p: (p.get("systemUpdatedAt") or ""), reverse=True)
        if limit is not None:
            products = products[:limit]

    exclude_quote_id = resolve_exclude_quote_id(
        request.GET.get("excludeQuote") or request.GET.get("exclude_quote")
    )
    _attach_catalogue_availability(products, exclude_quote_id=exclude_quote_id)

    return JsonResponse(
        {"products": products, "meta": {"mode": mode, "count": len(products)}},
        status=200,
    )


@csrf_exempt
@require_GET
def catalogue_product_detail(request, product_id: str):
    """
    Product detail payload for PDP.

    GET /customer/catalogue/products/<product_id>/

    Path `product_id` is always the ProductItem id. For a printed barcode row, also pass:
        ?tagId=<product_tags.id>   (or tag_id=)
    The tag must belong to that item. Weights and design code/title then come from the ProductTag
    snapshot; images prefer ProductTagPhoto, else ProductItem images.
    """
    pk = _parse_catalogue_path_id(product_id)
    if pk is None:
        return JsonResponse({"error": "Invalid product id"}, status=400)
    tag_id = _parse_optional_int(request.GET.get("tag_id") or request.GET.get("tagId"))
    exclude_quote_id = resolve_exclude_quote_id(
        request.GET.get("excludeQuote") or request.GET.get("exclude_quote")
    )
    prefetches = (
        Prefetch(
            "bom_items",
            queryset=ProductBOM.objects.select_related(
                "metal", "purity", "stone"
            )
            .prefetch_related(
                Prefetch(
                    "attributes",
                    queryset=ProductAttribute.objects.select_related("charge_type").order_by("id"),
                )
            )
            .order_by("id"),
        ),
        Prefetch(
            "stones",
            queryset=ProductStone.objects.select_related("stone").order_by("id"),
        ),
        Prefetch(
            "images",
            queryset=ProductImage.objects.order_by("-is_primary", "id"),
        ),
    )

    if tag_id is not None:
        tag = (
            _printed_barcode_tag_base_qs()
            .filter(pk=tag_id, product_item_id=pk)
            .prefetch_related(
                Prefetch(
                    "photos",
                    queryset=ProductTagPhoto.objects.order_by("sort_order", "id"),
                )
            )
            .first()
        )
        if not tag:
            return JsonResponse({"error": "Tag not found for this product"}, status=404)
        item = (
            _catalogue_product_items_base_qs()
            .filter(pk=pk)
            .select_related("sku__product_group__category")
            .prefetch_related(*prefetches)
            .first()
        )
        if not item:
            return JsonResponse({"error": "Product not found"}, status=404)
        return JsonResponse(
            _item_to_product_detail_dict(item, tag=tag, exclude_quote_id=exclude_quote_id),
            status=200,
        )

    item = (
        _catalogue_product_items_base_qs()
        .filter(id=pk)
        .select_related("sku__product_group__category")
        .prefetch_related(*prefetches)
        .first()
    )
    if not item:
        return JsonResponse({"error": "Product not found"}, status=404)
    return JsonResponse(
        _item_to_product_detail_dict(item, exclude_quote_id=exclude_quote_id),
        status=200,
    )


@csrf_exempt
@require_GET
def catalogue_collections(request):
    """
    Collection cards for catalogue home.

    GET /customer/catalogue/collections/
    """
    return JsonResponse({"collections": _build_collections()}, status=200)


@csrf_exempt
@require_GET
def catalogue_collection_detail(request, collection_id: str):
    """
    Resolve collection id to product ids for listing filter.

    GET /customer/catalogue/collections/<collection_id>/
    """
    rows = _build_collections()
    found = next((c for c in rows if c["id"] == collection_id), None)
    if not found:
        return JsonResponse({"error": "Collection not found"}, status=404)
    return JsonResponse({"collection": found}, status=200)


@csrf_exempt
@require_GET
def catalogue_product_items(request, product_id: str):
    """
    Item-level rows under a selected catalogue product context.

    GET /customer/catalogue/product-items/<product_id>/
    """
    pk = _parse_catalogue_path_id(product_id)
    if pk is None:
        return JsonResponse({"error": "Invalid product id"}, status=400)
    base_item = (
        _catalogue_product_items_base_qs()
        .filter(id=pk)
        .select_related("sku")
        .first()
    )
    if not base_item:
        return JsonResponse({"items": []}, status=200)

    siblings = (
        _catalogue_product_items_base_qs()
        .filter(sku_id=base_item.sku_id)
        .order_by("id")
    )
    items = []
    for it in siblings:
        label = vendor_variant_name_for_item(
            it, ProductItemLinkedVendor=ProductItemLinkedVendor
        ) or (it.customer_variant_name or "").strip()
        if not label:
            label = (it.store_variant_name or "").strip() or _item_sku_product_code(it) or f"Item {it.id}"
        items.append({"id": str(it.id), "label": label})
    return JsonResponse({"items": items}, status=200)


@csrf_exempt
@require_GET
def catalogue_product_variants(request, product_item_id: str):
    """
    Variant facets for a product item (metal, purity, stoneType, size).

    GET /customer/catalogue/product-variants/<product_item_id>/
    """
    pk = _parse_catalogue_path_id(product_item_id)
    if pk is None:
        return JsonResponse({"error": "Invalid product item id"}, status=400)
    item = (
        _catalogue_product_items_base_qs()
        .filter(id=pk)
        .prefetch_related(
            Prefetch(
                "bom_items",
                queryset=ProductBOM.objects.select_related(
                    "metal", "purity", "stone"
                ).order_by("id"),
            ),
            Prefetch(
                "stones",
                queryset=ProductStone.objects.select_related("stone").order_by("id"),
            ),
        )
        .first()
    )
    if not item:
        return JsonResponse({"variants": []}, status=200)

    opts = _variant_options_for_item(item)
    payload = [
        {"name": "metal", "values": [x["value"] for x in opts["metal"]]},
        {"name": "purity", "values": [x["value"] for x in opts["purity"]]},
        {"name": "stoneType", "values": [x["value"] for x in opts["stoneType"]]},
        {"name": "size", "values": [x["value"] for x in opts["size"]]},
    ]
    return JsonResponse({"variants": payload}, status=200)
