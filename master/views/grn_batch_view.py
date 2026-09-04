"""
GRN batch CRUD — function views, aligned with GrnBatchManagement.tsx BatchRow.
"""
import re
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth, ensure_admin_permission
from shared.models import GrnBatch, GrnBag, GrnLot
from shared.grn_weight_parse import parse_optional_weight_decimal

_ORDER_MAP = {
    "date": "date",
    "-date": "-date",
    "system_created_at": "system_created_at",
    "-system_created_at": "-system_created_at",
    "system_updated_at": "system_updated_at",
    "-system_updated_at": "-system_updated_at",
    "doc_no": "doc_no",
    "-doc_no": "-doc_no",
    "id": "id",
    "-id": "-id",
}


def _dec_str(v):
    if v is None:
        return ""
    s = format(v, "f").rstrip("0").rstrip(".")
    return s if s else "0"


def batch_to_dict(obj):
    return {
        "id": obj.id,
        "doc_no": obj.doc_no or "",
        "date": obj.date.isoformat() if obj.date else "",
        "category": obj.category or "",
        "product_type": obj.product_type or "",
        "vendor": obj.vendor or "",
        "terms": obj.terms or "",
        "remarks": obj.remarks or "",
        "contact_person": obj.contact_person or "",
        "validity": obj.validity.isoformat() if obj.validity else "",
        "reorder_deliver_days": obj.reorder_deliver_days or "",
        "metal": obj.metal or "",
        "quantity": _dec_str(obj.quantity),
        "pcs": str(obj.pcs) if obj.pcs is not None else "",
        "g_wt": _dec_str(obj.g_wt),
        "stone_wt": _dec_str(obj.stone_wt),
        "stone_wt_unit": getattr(obj, "stone_wt_unit", None) or "grams",
        "stone_rate_basis": getattr(obj, "stone_rate_basis", None) or "per_gram",
        "stone_purchase_rate": _dec_str(obj.stone_purchase_rate),
        "stone_sell_rate": _dec_str(obj.stone_sell_rate),
        "stone_exchange_rate": _dec_str(obj.stone_exchange_rate),
        "quality_check": obj.quality_check or "",
        "bom": obj.bom or "",
        "status": obj.status or "",
    }


def _opt_decimal(raw, field_name, errors):
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        errors.setdefault(field_name, []).append(f"{field_name} must be a valid number.")
        return None


def _opt_int(raw, field_name, errors):
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        errors.setdefault(field_name, []).append(f"{field_name} must be a valid integer.")
        return None


def _parse_reorder_days(raw, errors):
    if raw is None or str(raw).strip() == "":
        return ""
    s = str(raw).strip()
    if not re.fullmatch(r"-?\d+", s):
        errors.setdefault("reorder_deliver_days", []).append("Must be a whole number of days.")
        return None
    return s


def _parse_date_required(raw, field_name, errors):
    if raw is None or str(raw).strip() == "":
        errors.setdefault(field_name, []).append("This field is required.")
        return None
    d = parse_date(str(raw).strip())
    if d is None:
        errors.setdefault(field_name, []).append("Enter a valid date.")
        return None
    return d


def _parse_date_optional(raw, field_name, errors):
    if raw is None or str(raw).strip() == "":
        return None
    d = parse_date(str(raw).strip())
    if d is None:
        errors.setdefault(field_name, []).append("Enter a valid date.")
        return None
    return d


_QC_ALLOWED = {"Approve", "Pending", "Reject"}
_BOM_ALLOWED = {"Yes", "No"}


def _values_from_data_full(data, errors):
    """
    Create or PUT: Batch fields aligned with GRN Batch doc.
    Required: batch no (doc_no), date, category, vendor, gross weight (g_wt).
    Batch no is mandatory but NOT unique (same number may repeat).
    """
    values = {}

    # Batch number — mandatory free text; duplicates allowed.
    dn = data.get("doc_no")
    dn = "" if dn is None else str(dn).strip()
    if not dn:
        errors.setdefault("doc_no", []).append("Batch number is required.")
    else:
        values["doc_no"] = dn

    d = _parse_date_required(data.get("date"), "date", errors)
    if d is not None:
        values["date"] = d

    cat = data.get("category")
    if cat is None or not str(cat).strip():
        errors.setdefault("category", []).append("This field is required.")
    else:
        values["category"] = str(cat).strip()

    vendor = data.get("vendor")
    if vendor is None or not str(vendor).strip():
        errors.setdefault("vendor", []).append("Vendor is required.")
    else:
        values["vendor"] = str(vendor).strip()

    for f in (
        "product_type",
        "terms",
        "remarks",
        "contact_person",
        "metal",
    ):
        v = data.get(f)
        values[f] = "" if v is None else str(v).strip()

    qc = "" if data.get("quality_check") is None else str(data.get("quality_check")).strip()
    if qc and qc not in _QC_ALLOWED:
        errors.setdefault("quality_check", []).append(
            "QC must be Approve, Pending, or Reject."
        )
    else:
        values["quality_check"] = qc

    bom = "" if data.get("bom") is None else str(data.get("bom")).strip()
    if bom and bom not in _BOM_ALLOWED:
        errors.setdefault("bom", []).append("BOM must be Yes or No.")
    else:
        values["bom"] = bom

    rd = _parse_reorder_days(data.get("reorder_deliver_days"), errors)
    if "reorder_deliver_days" not in errors:
        values["reorder_deliver_days"] = rd if rd is not None else ""

    v = data.get("validity")
    if v is None or str(v).strip() == "":
        values["validity"] = None
    else:
        vd = _parse_date_optional(v, "validity", errors)
        if vd is not None:
            values["validity"] = vd

    for k in ("quantity", "stone_purchase_rate", "stone_sell_rate", "stone_exchange_rate"):
        err_before = len(errors.get(k, []))
        parsed = _opt_decimal(data.get(k, ""), k, errors)
        if len(errors.get(k, [])) == err_before:
            values[k] = parsed

    for k in ("g_wt", "stone_wt"):
        err_before = len(errors.get(k, []))
        parsed = parse_optional_weight_decimal(data.get(k, ""), k, errors)
        if len(errors.get(k, [])) == err_before:
            values[k] = parsed

    # Gross weight is mandatory (doc: Gross Weight — numerical, up to 4 decimals).
    if values.get("g_wt") is None:
        errors.setdefault("g_wt", []).append("Gross weight is required.")
    elif values["g_wt"] <= 0:
        errors.setdefault("g_wt", []).append("Gross weight must be greater than 0.")
    # stone_wt stores Less weight from the GRN Batch doc (deduction / stone).

    for k in ("stone_wt_unit", "stone_rate_basis"):
        v = data.get(k)
        if v is not None and str(v).strip():
            values[k] = str(v).strip()[:16]

    err_before = len(errors.get("pcs", []))
    parsed = _opt_int(data.get("pcs", ""), "pcs", errors)
    if len(errors.get("pcs", [])) == err_before:
        values["pcs"] = parsed

    st = (data.get("status") or "").strip()
    values["status"] = st or "Open"

    return values


def _patch_values(data, errors):
    """PATCH: only keys present in data."""
    values = {}
    if "date" in data:
        d = _parse_date_required(data.get("date"), "date", errors)
        if d is not None:
            values["date"] = d
    if "category" in data:
        cat = data.get("category")
        if cat is None or not str(cat).strip():
            errors.setdefault("category", []).append("This field is required.")
        else:
            values["category"] = str(cat).strip()
    for f in (
        "doc_no",
        "product_type",
        "terms",
        "remarks",
        "contact_person",
        "metal",
        "status",
    ):
        if f in data:
            v = data.get(f)
            values[f] = "" if v is None else str(v).strip()
    if "doc_no" in values and not values["doc_no"]:
        errors.setdefault("doc_no", []).append("Batch number is required.")
    if "vendor" in data:
        vendor = "" if data.get("vendor") is None else str(data.get("vendor")).strip()
        if not vendor:
            errors.setdefault("vendor", []).append("Vendor is required.")
        else:
            values["vendor"] = vendor
    if "quality_check" in data:
        qc = "" if data.get("quality_check") is None else str(data.get("quality_check")).strip()
        if qc and qc not in _QC_ALLOWED:
            errors.setdefault("quality_check", []).append(
                "QC must be Approve, Pending, or Reject."
            )
        else:
            values["quality_check"] = qc
    if "bom" in data:
        bom = "" if data.get("bom") is None else str(data.get("bom")).strip()
        if bom and bom not in _BOM_ALLOWED:
            errors.setdefault("bom", []).append("BOM must be Yes or No.")
        else:
            values["bom"] = bom
    if "reorder_deliver_days" in data:
        rd = _parse_reorder_days(data.get("reorder_deliver_days"), errors)
        if "reorder_deliver_days" not in errors:
            values["reorder_deliver_days"] = rd if rd is not None else ""
    if "validity" in data:
        v = data.get("validity")
        if v is None or str(v).strip() == "":
            values["validity"] = None
        else:
            vd = _parse_date_optional(v, "validity", errors)
            if vd is not None:
                values["validity"] = vd
    for k in ("quantity", "stone_purchase_rate", "stone_sell_rate", "stone_exchange_rate"):
        if k in data:
            err_before = len(errors.get(k, []))
            parsed = _opt_decimal(data.get(k), k, errors)
            if len(errors.get(k, [])) == err_before:
                values[k] = parsed
    for k in ("g_wt", "stone_wt"):
        if k in data:
            err_before = len(errors.get(k, []))
            parsed = parse_optional_weight_decimal(data.get(k), k, errors)
            if len(errors.get(k, [])) == err_before:
                values[k] = parsed

    # Do not allow clearing / zeroing batch gross weight on update.
    if "g_wt" in values:
        if values["g_wt"] is None:
            errors.setdefault("g_wt", []).append("Gross weight is required.")
        elif values["g_wt"] <= 0:
            errors.setdefault("g_wt", []).append("Gross weight must be greater than 0.")
    if "pcs" in data:
        err_before = len(errors.get("pcs", []))
        parsed = _opt_int(data.get("pcs"), "pcs", errors)
        if len(errors.get("pcs", [])) == err_before:
            values["pcs"] = parsed
    for k in ("stone_wt_unit", "stone_rate_basis"):
        if k in data and str(data.get(k) or "").strip():
            values[k] = str(data.get(k)).strip()[:16]
    return values


def _sum_lot_decimals(lots, field_name):
    total = Decimal("0")
    found = False
    for lot in lots:
        val = getattr(lot, field_name, None)
        if val is not None:
            found = True
            total += val
    return total if found else None


def _add_dec_str(a, b):
    if a is None and b is None:
        return ""
    return _dec_str((a or Decimal("0")) + (b or Decimal("0")))


def batch_detail_payload(obj):
    """Batch row plus lot allocations and finished goods (bags) for reconciliation."""
    from master.views.grn_lot_view import lot_to_dict

    base = batch_to_dict(obj)
    lots = list(
        GrnLot.objects.filter(batch_id=obj.id)
        .select_related("category", "subcategory", "batch", "updated_by")
        .order_by("id")
    )
    allocated_qty = _sum_lot_decimals(lots, "quantity")
    allocated_pcs = _sum_lot_decimals(lots, "pcs")
    allocated_g = _sum_lot_decimals(lots, "g_wt")
    # Stone stays on batch / Make Bag — lots no longer allocate stone_wt.
    allocated_st = None

    remaining = {
        "quantity": base["quantity"],
        "pcs": base["pcs"],
        "g_wt": base["g_wt"],
        "stone_wt": base["stone_wt"],
    }
    allocated = {
        "quantity": _dec_str(allocated_qty),
        "pcs": str(int(allocated_pcs)) if allocated_pcs is not None else "",
        "g_wt": _dec_str(allocated_g),
        "stone_wt": _dec_str(allocated_st),
    }
    received = {
        "quantity": _add_dec_str(obj.quantity, allocated_qty),
        "pcs": (
            str(int((obj.pcs or 0) + int(allocated_pcs or 0)))
            if obj.pcs is not None or allocated_pcs is not None
            else ""
        ),
        "g_wt": _add_dec_str(obj.g_wt, allocated_g),
        "stone_wt": _add_dec_str(obj.stone_wt, allocated_st),
    }

    bags = (
        GrnBag.objects.filter(lot__batch_id=obj.id)
        .select_related("lot", "product_item", "product_item__sku")
        .order_by("-system_created_at", "-id")
    )
    finished_goods = []
    for bag in bags[:100]:
        item = bag.product_item
        sku_code = ""
        store_name = ""
        if item and item.sku_id:
            sku_code = item.sku.sku_code or ""
            store_name = item.store_variant_name or ""
        finished_goods.append(
            {
                "id": bag.id,
                "bag_no": bag.bag_no or "",
                "lot_no": bag.lot.lot_no if bag.lot_id else "",
                "lot_id": bag.lot_id,
                "sku_code": sku_code,
                "store_variant_name": store_name,
                "product_item_id": item.id if item else None,
            }
        )

    base["summary"] = {
        "received": received,
        "allocated_to_lots": allocated,
        "remaining": remaining,
    }
    base["lots"] = [lot_to_dict(l) for l in lots]
    base["finished_goods"] = finished_goods
    return base


@api_view(["GET", "POST"])
@admin_auth()
def grn_batch_list_create(request):
    if request.method == "GET":
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_BATCH_VIEW")
        if denied:
            return denied
    else:
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_BATCH_CREATE")
        if denied:
            return denied

    if request.method == "GET":
        # Open batches only: closed/completed or zero remaining gross weight are hidden
        # so users cannot create further lots from them in the batch-management list.
        qs = GrnBatch.objects.exclude(
            Q(status__iexact="Closed") | Q(status__iexact="Completed")
        ).filter(
            Q(g_wt__isnull=False, g_wt__gt=0)
            | Q(quantity__isnull=False, quantity__gt=0)
            | Q(pcs__isnull=False, pcs__gt=0)
            | Q(stone_wt__isnull=False, stone_wt__gt=0)
        )
        search = request.GET.get("search")
        if search:
            term = search.strip()
            qs = qs.filter(
                Q(doc_no__icontains=term)
                | Q(category__icontains=term)
                | Q(product_type__icontains=term)
                | Q(vendor__icontains=term)
                | Q(metal__icontains=term)
                | Q(remarks__icontains=term)
                | Q(terms__icontains=term)
                | Q(contact_person__icontains=term)
                | Q(quality_check__icontains=term)
                | Q(bom__icontains=term)
                | Q(status__icontains=term)
            )
        for param in ("category", "vendor", "metal", "status"):
            v = request.GET.get(param)
            if v:
                qs = qs.filter(**{f"{param}__iexact": v})
        ordering = request.GET.get("ordering", "-system_created_at")
        order_expr = _ORDER_MAP.get(ordering.strip(), "-system_created_at")
        qs = qs.order_by(order_expr)
        return Response({"results": [batch_to_dict(o) for o in qs]})

    data = request.data if isinstance(request.data, dict) else {}
    errors = {}
    values = _values_from_data_full(data, errors)
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    # doc_no is required and may be duplicated across batches (not unique).
    obj = GrnBatch.objects.create(**values)
    return Response(batch_to_dict(obj), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@admin_auth()
def grn_batch_detail(request, pk):
    try:
        obj = GrnBatch.objects.get(pk=pk)
    except GrnBatch.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_BATCH_VIEW")
        if denied:
            return denied
        return Response(batch_detail_payload(obj))

    if request.method == "DELETE":
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_BATCH_DELETE")
        if denied:
            return denied
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_BATCH_UPDATE")
    if denied:
        return denied

    data = request.data if isinstance(request.data, dict) else {}
    errors = {}
    if request.method == "PATCH":
        values = _patch_values(data, errors)
    else:
        values = _values_from_data_full(data, errors)
    # doc_no is now operator-editable free text — keep whatever the body holds.

    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)

    for k, v in values.items():
        setattr(obj, k, v)
    obj.save()
    return Response(batch_to_dict(obj))
