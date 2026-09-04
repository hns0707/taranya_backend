"""
Purchase Order CRUD — header + lines with virtual product linkage.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from master.views.product_views import get_admin_user_from_request
from shared.grn_weight_parse import parse_optional_weight_decimal
from shared.models import (
    ProductItem,
    ProductItemLinkedVendor,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from shared.product_item_size import format_product_item_size_display, serialize_product_item_size_for_api
from shared.services.product_item_vendors import (
    primary_vendor_id_for_item,
    vendor_variant_name_for_item,
)


def _dec_str(v):
    if v is None:
        return ""
    s = format(v, "f").rstrip("0").rstrip(".")
    return s if s else "0"


def _metal_purity_from_item(item):
    metal_name = ""
    purity_name = ""
    for bom in item.bom_items.all():
        if bom.material_type == "METAL" and bom.metal_id:
            if bom.metal:
                metal_name = bom.metal.metal_name or ""
            if bom.purity:
                purity_name = (bom.purity.purity_name or bom.purity.type or "").strip()
            break
    return metal_name, purity_name


def product_item_snapshot(item, vendor_id=None):
    """Build PO line prefill from a published ProductItem."""
    sku = item.sku
    pg = sku.product_group if sku else None
    metal_name, purity_name = _metal_purity_from_item(item)
    colour = ""
    if sku and sku.color_id and sku.color:
        colour = sku.color.label or ""
    item_group = ""
    item_type = ""
    category_id = None
    subcategory_id = None
    if pg:
        if pg.category_id and pg.category:
            item_group = pg.category.name or ""
            category_id = pg.category_id
        if pg.subcategory_id and pg.subcategory:
            item_type = pg.subcategory.name or ""
            subcategory_id = pg.subcategory_id
    size_display = format_product_item_size_display(item)
    return {
        "product_item_id": item.id,
        "product_sku_id": sku.id if sku else None,
        "product_code": (sku.product_code if sku else "") or "",
        "sku_code": (sku.sku_code if sku else "") or "",
        "style_name": (pg.style_name if pg else "") or "",
        "item_group": item_group,
        "item_type": item_type,
        "category_id": category_id,
        "subcategory_id": subcategory_id,
        "vendor_variant_name": vendor_variant_name_for_item(
            item,
            vendor_id=vendor_id or primary_vendor_id_for_item(
                item, ProductItemLinkedVendor=ProductItemLinkedVendor
            ),
            ProductItemLinkedVendor=ProductItemLinkedVendor,
        ),
        "metal": metal_name,
        "purity": purity_name,
        "colour": colour,
        "stone_name": "",
        "size_display": size_display,
        "ordered_g_wt": _dec_str(item.gross_weight) if item.gross_weight else "",
        "ordered_net_wt": _dec_str(item.net_weight) if item.net_weight else "",
    }


def line_to_dict(line):
    return {
        "id": line.id,
        "line_no": line.line_no,
        "product_item_id": line.product_item_id,
        "product_sku_id": line.product_sku_id,
        "product_code": line.product_code or "",
        "sku_code": line.sku_code or "",
        "style_name": line.style_name or "",
        "item_group": line.item_group or "",
        "item_type": line.item_type or "",
        "category_id": line.category_id,
        "subcategory_id": line.subcategory_id,
        "vendor_variant_name": line.vendor_variant_name or "",
        "metal": line.metal or "",
        "purity": line.purity or "",
        "colour": line.colour or "",
        "stone_name": line.stone_name or "",
        "size_display": line.size_display or "",
        "ordered_pcs": line.ordered_pcs,
        "ordered_g_wt": _dec_str(line.ordered_g_wt),
        "ordered_net_wt": _dec_str(line.ordered_net_wt),
        "making_rate": _dec_str(line.making_rate),
        "stone_purchase_rate": _dec_str(line.stone_purchase_rate),
        "line_amount": _dec_str(line.line_amount),
        "expected_delivery_date": line.expected_delivery_date.isoformat() if line.expected_delivery_date else "",
        "remarks": line.remarks or "",
        "line_status": line.line_status or "Open",
        "received_pcs": line.received_pcs,
        "received_g_wt": _dec_str(line.received_g_wt),
    }


def po_to_dict(po, include_lines=True):
    out = {
        "id": po.id,
        "po_no": po.po_no or "",
        "po_date": po.po_date.isoformat() if po.po_date else "",
        "po_category": po.po_category or "",
        "item_type": po.item_type or "",
        "vendor_id": po.vendor_id,
        "vendor_name": po.vendor_name or (po.vendor.vendor_name if po.vendor_id else ""),
        "currency": po.currency or "INR",
        "terms": po.terms or "",
        "remarks": po.remarks or "",
        "require_date": po.require_date.isoformat() if po.require_date else "",
        "contact_person": po.contact_person or "",
        "validity_date": po.validity_date.isoformat() if po.validity_date else "",
        "validity_days": po.validity_days or "",
        "expected_delivery_date": po.expected_delivery_date.isoformat() if po.expected_delivery_date else "",
        "buyer_name": po.buyer_name or "",
        "ship_to_address": po.ship_to_address or "",
        "internal_notes": po.internal_notes or "",
        "status": po.status or "Draft",
        "total_ordered_pcs": po.total_ordered_pcs or 0,
        "total_ordered_g_wt": _dec_str(po.total_ordered_g_wt),
        "system_created_at": po.system_created_at.isoformat() if po.system_created_at else "",
    }
    if include_lines:
        lines = po.lines.all().order_by("line_no")
        out["lines"] = [line_to_dict(ln) for ln in lines]
    return out


def _parse_date_opt(raw, field, errors):
    if raw is None or str(raw).strip() == "":
        return None
    d = parse_date(str(raw).strip())
    if d is None:
        errors.setdefault(field, []).append("Enter a valid date.")
    return d


def _parse_line_payload(raw, idx, errors):
    prefix = f"lines[{idx}]"
    pcs_raw = raw.get("ordered_pcs", 1)
    try:
        ordered_pcs = int(pcs_raw) if pcs_raw not in (None, "") else 1
        if ordered_pcs < 1:
            errors.setdefault(f"{prefix}.ordered_pcs", []).append("Must be at least 1.")
    except (TypeError, ValueError):
        errors.setdefault(f"{prefix}.ordered_pcs", []).append("Invalid integer.")
        ordered_pcs = 1

    line_errors = {}
    ordered_g_wt = parse_optional_weight_decimal(raw.get("ordered_g_wt"), "ordered_g_wt", line_errors)
    ordered_net_wt = parse_optional_weight_decimal(raw.get("ordered_net_wt"), "ordered_net_wt", line_errors)
    making_rate = parse_optional_weight_decimal(raw.get("making_rate"), "making_rate", line_errors)
    stone_purchase_rate = parse_optional_weight_decimal(
        raw.get("stone_purchase_rate"), "stone_purchase_rate", line_errors
    )
    line_amount = parse_optional_weight_decimal(raw.get("line_amount"), "line_amount", line_errors)
    for k, msgs in line_errors.items():
        errors.setdefault(f"{prefix}.{k}", []).extend(msgs)

    product_item_id = raw.get("product_item_id")
    try:
        product_item_id = int(product_item_id) if product_item_id not in (None, "") else None
    except (TypeError, ValueError):
        errors.setdefault(f"{prefix}.product_item_id", []).append("Invalid product.")
        product_item_id = None

    if not product_item_id:
        errors.setdefault(f"{prefix}.product_item_id", []).append("Select a product from catalog search.")

    product_sku_id = raw.get("product_sku_id")
    try:
        product_sku_id = int(product_sku_id) if product_sku_id not in (None, "") else None
    except (TypeError, ValueError):
        product_sku_id = None

    category_id = raw.get("category_id")
    subcategory_id = raw.get("subcategory_id")
    try:
        category_id = int(category_id) if category_id not in (None, "") else None
    except (TypeError, ValueError):
        category_id = None
    try:
        subcategory_id = int(subcategory_id) if subcategory_id not in (None, "") else None
    except (TypeError, ValueError):
        subcategory_id = None

    return {
        "line_no": raw.get("line_no"),
        "product_item_id": product_item_id,
        "product_sku_id": product_sku_id,
        "product_code": str(raw.get("product_code") or "").strip()[:100],
        "sku_code": str(raw.get("sku_code") or "").strip()[:128],
        "style_name": str(raw.get("style_name") or "").strip()[:255],
        "item_group": str(raw.get("item_group") or "").strip()[:150],
        "item_type": str(raw.get("item_type") or "").strip()[:150],
        "category_id": category_id,
        "subcategory_id": subcategory_id,
        "vendor_variant_name": str(raw.get("vendor_variant_name") or "").strip()[:255],
        "metal": str(raw.get("metal") or "").strip()[:64],
        "purity": str(raw.get("purity") or "").strip()[:64],
        "colour": str(raw.get("colour") or "").strip()[:64],
        "stone_name": str(raw.get("stone_name") or "").strip()[:255],
        "size_display": str(raw.get("size_display") or "").strip()[:64],
        "ordered_pcs": ordered_pcs,
        "ordered_g_wt": ordered_g_wt,
        "ordered_net_wt": ordered_net_wt,
        "making_rate": making_rate,
        "stone_purchase_rate": stone_purchase_rate,
        "line_amount": line_amount,
        "expected_delivery_date": _parse_date_opt(raw.get("expected_delivery_date"), "expected_delivery_date", errors),
        "remarks": str(raw.get("remarks") or "").strip(),
        "line_status": str(raw.get("line_status") or "Open").strip()[:16] or "Open",
    }


def _apply_header(data, errors, partial=False):
    out = {}
    if not partial or "po_no" in data:
        po_no = str(data.get("po_no") or "").strip()
        out["po_no"] = po_no
    if not partial or "po_date" in data:
        d = _parse_date_opt(data.get("po_date"), "po_date", errors)
        if d is None and not partial:
            errors.setdefault("po_date", []).append("This field is required.")
        out["po_date"] = d
    for key in (
        "po_category",
        "item_type",
        "currency",
        "terms",
        "contact_person",
        "validity_days",
        "buyer_name",
        "status",
    ):
        if not partial or key in data:
            out[key] = str(data.get(key) or "").strip()
    if not partial or "remarks" in data:
        out["remarks"] = str(data.get("remarks") or "").strip()
    if not partial or "ship_to_address" in data:
        out["ship_to_address"] = str(data.get("ship_to_address") or "").strip()
    if not partial or "internal_notes" in data:
        out["internal_notes"] = str(data.get("internal_notes") or "").strip()
    for date_key in ("require_date", "validity_date", "expected_delivery_date"):
        if not partial or date_key in data:
            out[date_key] = _parse_date_opt(data.get(date_key), date_key, errors)
    if not partial or "vendor_id" in data:
        vid = data.get("vendor_id")
        try:
            vid = int(vid) if vid not in (None, "") else None
        except (TypeError, ValueError):
            errors.setdefault("vendor_id", []).append("Invalid vendor.")
            vid = None
        if vid is None and not partial:
            errors.setdefault("vendor_id", []).append("Vendor is required.")
        out["vendor_id"] = vid
        if vid:
            try:
                v = Vendor.objects.get(pk=vid, is_active=True)
                out["vendor_name"] = v.vendor_name or ""
                if not out.get("contact_person") and v.contact_person:
                    out["contact_person"] = v.contact_person
            except Vendor.DoesNotExist:
                errors.setdefault("vendor_id", []).append("Vendor not found.")
    elif not partial or "vendor_name" in data:
        out["vendor_name"] = str(data.get("vendor_name") or "").strip()[:256]
    return out


def _next_po_no():
    today = timezone.localdate()
    prefix = f"PO-{today.strftime('%Y%m')}-"
    last = (
        PurchaseOrder.objects.filter(po_no__istartswith=prefix)
        .order_by("-po_no")
        .values_list("po_no", flat=True)
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(str(last).split("-")[-1]) + 1
        except (ValueError, IndexError):
            seq = PurchaseOrder.objects.count() + 1
    return f"{prefix}{seq:04d}"


def _save_lines(po, lines_data, admin):
    PurchaseOrderLine.objects.filter(purchase_order=po).delete()
    total_pcs = 0
    total_g = Decimal("0")
    for idx, raw in enumerate(lines_data or []):
        parsed = _parse_line_payload(raw, idx, {})
        line_no = parsed.get("line_no") or (idx + 1)
        try:
            line_no = int(line_no)
        except (TypeError, ValueError):
            line_no = idx + 1
        PurchaseOrderLine.objects.create(
            purchase_order=po,
            line_no=line_no,
            product_item_id=parsed["product_item_id"],
            product_sku_id=parsed["product_sku_id"],
            product_code=parsed["product_code"],
            sku_code=parsed["sku_code"],
            style_name=parsed["style_name"],
            item_group=parsed["item_group"],
            item_type=parsed["item_type"],
            category_id=parsed["category_id"],
            subcategory_id=parsed["subcategory_id"],
            vendor_variant_name=parsed["vendor_variant_name"],
            metal=parsed["metal"],
            purity=parsed["purity"],
            colour=parsed["colour"],
            stone_name=parsed["stone_name"],
            size_display=parsed["size_display"],
            ordered_pcs=parsed["ordered_pcs"],
            ordered_g_wt=parsed["ordered_g_wt"],
            ordered_net_wt=parsed["ordered_net_wt"],
            making_rate=parsed["making_rate"],
            stone_purchase_rate=parsed["stone_purchase_rate"],
            line_amount=parsed["line_amount"],
            expected_delivery_date=parsed["expected_delivery_date"],
            remarks=parsed["remarks"],
            line_status=parsed["line_status"],
            created_by=admin,
            updated_by=admin,
        )
        total_pcs += parsed["ordered_pcs"]
        if parsed["ordered_g_wt"]:
            total_g += parsed["ordered_g_wt"]
    po.total_ordered_pcs = total_pcs
    po.total_ordered_g_wt = total_g if total_g > 0 else None
    po.save(update_fields=["total_ordered_pcs", "total_ordered_g_wt", "system_updated_at"])


@api_view(["GET"])
@admin_auth()
def purchase_order_product_prefill(request, product_item_id):
    """GET /master/purchase-orders/product-prefill/<product_item_id>/"""
    item = (
        ProductItem.objects.select_related(
            "sku",
            "sku__product_group",
            "sku__product_group__category",
            "sku__product_group__subcategory",
            "sku__color",
        )
        .prefetch_related("bom_items__metal", "bom_items__purity")
        .filter(pk=product_item_id)
        .first()
    )
    if not item:
        return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
    vendor_id = request.GET.get("vendor_id")
    data = product_item_snapshot(item, vendor_id=vendor_id)
    data.update(serialize_product_item_size_for_api(item))
    return Response(data)


@api_view(["GET", "POST"])
@admin_auth()
def purchase_order_list_create(request):
    admin = get_admin_user_from_request(request)

    if request.method == "GET":
        qs = PurchaseOrder.objects.select_related("vendor").order_by("-system_created_at", "-id")
        status_filter = (request.GET.get("status") or "").strip()
        if status_filter:
            qs = qs.filter(status=status_filter)
        q = (request.GET.get("search") or "").strip()
        if q:
            qs = qs.filter(
                Q(po_no__icontains=q)
                | Q(vendor_name__icontains=q)
                | Q(remarks__icontains=q)
            )
        try:
            page_size = min(int(request.GET.get("page_size", 100)), 200)
        except (TypeError, ValueError):
            page_size = 100
        rows = [po_to_dict(po, include_lines=False) for po in qs[:page_size]]
        return Response({"results": rows, "total": qs.count()})

    data = request.data or {}
    errors = {}
    header = _apply_header(data, errors, partial=False)
    lines_in = data.get("lines")
    if not isinstance(lines_in, list) or len(lines_in) == 0:
        errors.setdefault("lines", []).append("Add at least one order line.")

    parsed_lines = []
    for idx, raw in enumerate(lines_in or []):
        if isinstance(raw, dict):
            parsed_lines.append(_parse_line_payload(raw, idx, errors))

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    po_no = header.get("po_no") or _next_po_no()
    if PurchaseOrder.objects.filter(po_no=po_no).exists():
        return Response({"errors": {"po_no": ["PO number already exists."]}}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        po = PurchaseOrder.objects.create(
            po_no=po_no,
            po_date=header["po_date"],
            po_category=header.get("po_category", ""),
            item_type=header.get("item_type", ""),
            vendor_id=header.get("vendor_id"),
            vendor_name=header.get("vendor_name", ""),
            currency=header.get("currency") or "INR",
            terms=header.get("terms", ""),
            remarks=header.get("remarks", ""),
            require_date=header.get("require_date"),
            contact_person=header.get("contact_person", ""),
            validity_date=header.get("validity_date"),
            validity_days=header.get("validity_days", ""),
            expected_delivery_date=header.get("expected_delivery_date"),
            buyer_name=header.get("buyer_name", ""),
            ship_to_address=header.get("ship_to_address", ""),
            internal_notes=header.get("internal_notes", ""),
            status=header.get("status") or "Draft",
            created_by=admin,
            updated_by=admin,
        )
        _save_lines(po, lines_in, admin)

    po = PurchaseOrder.objects.select_related("vendor").prefetch_related("lines").get(pk=po.pk)
    return Response(po_to_dict(po), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@admin_auth()
def purchase_order_detail(request, pk):
    admin = get_admin_user_from_request(request)
    try:
        po = PurchaseOrder.objects.select_related("vendor").prefetch_related("lines").get(pk=pk)
    except PurchaseOrder.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(po_to_dict(po))

    if request.method == "DELETE":
        if po.status not in ("Draft", "Cancelled"):
            return Response(
                {"detail": "Only draft or cancelled POs can be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        po.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    data = request.data or {}
    errors = {}
    partial = request.method == "PATCH"
    header = _apply_header(data, errors, partial=partial)

    lines_in = data.get("lines") if "lines" in data else None
    parsed_lines = []
    if lines_in is not None:
        if not isinstance(lines_in, list) or len(lines_in) == 0:
            errors.setdefault("lines", []).append("Add at least one order line.")
        for idx, raw in enumerate(lines_in or []):
            if isinstance(raw, dict):
                parsed_lines.append(_parse_line_payload(raw, idx, errors))

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        for key, val in header.items():
            if key == "po_no" and not val:
                continue
            setattr(po, key, val)
        po.updated_by = admin
        po.save()
        if lines_in is not None:
            _save_lines(po, lines_in, admin)

    po = PurchaseOrder.objects.select_related("vendor").prefetch_related("lines").get(pk=pk)
    return Response(po_to_dict(po))
