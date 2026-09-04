"""
GRN lot list/create — function views, aligned with grn-lot-types.ts LotListingRow.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth, ensure_admin_permission
from shared.grn_weight_parse import parse_optional_weight_decimal
from shared.models import (
    GrnBag,
    GrnBatch,
    GrnLot,
    ProductItem,
    ProductItemLinkedVendor,
    Subcategory,
    Vendor,
)
from shared.services.product_item_vendors import resolve_lot_linked_vendor_terms

# Align with GrnBatch.g_wt decimal_places=4
_G_WT_QUANT = Decimal("0.0001")


def _quantize_g_wt(value):
    """Normalize gross weight for compare/subtract (avoids float/binary noise)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value).strip())
    return d.quantize(_G_WT_QUANT, rounding=ROUND_HALF_UP)


def _parse_batch_id(data):
    raw = data.get("batch_id")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_ORDER_MAP = {
    "lot_no": "lot_no",
    "-lot_no": "-lot_no",
    "batch_doc_no": "batch_doc_no",
    "-batch_doc_no": "-batch_doc_no",
    "system_created_at": "system_created_at",
    "-system_created_at": "-system_created_at",
    "system_updated_at": "system_updated_at",
    "-system_updated_at": "-system_updated_at",
    "id": "id",
    "-id": "-id",
}


def _dec_str(v):
    if v is None:
        return ""
    s = format(v, "f").rstrip("0").rstrip(".")
    return s if s else "0"


def lot_to_dict(obj):
    """
    `item_group` = category name (UI: Item group); `item_type` = subcategory name (UI: Item type).
    Lot stores pattern_code; metal / stone are handled on Make Bag.
    Type-1 fields: QC, less_wt, marketing flags, LOB/brand/occasion.
    """
    category_display = ""
    if getattr(obj, "category_id", None) and getattr(obj, "category", None):
        category_display = obj.category.name or ""
    subcategory_display = ""
    if getattr(obj, "subcategory_id", None) and getattr(obj, "subcategory", None):
        subcategory_display = obj.subcategory.name or ""

    updated_by_name = ""
    updated_by = getattr(obj, "updated_by", None)
    if updated_by is not None:
        updated_by_name = (
            getattr(updated_by, "full_name", None)
            or getattr(updated_by, "username", None)
            or getattr(updated_by, "email", None)
            or str(updated_by)
        )

    batch = getattr(obj, "batch", None)
    return {
        "id": obj.id,
        "lot_no": obj.lot_no or "",
        "batch_id": getattr(obj, "batch_id", None),
        "batch_doc_no": obj.batch_doc_no or "",
        "batch_vendor": (batch.vendor or "") if batch is not None else "",
        "batch_metal": (batch.metal or "") if batch is not None else "",
        "item_group": category_display,
        "item_type": subcategory_display,
        "category_id": getattr(obj, "category_id", None),
        "subcategory_id": getattr(obj, "subcategory_id", None),
        "product_code": getattr(obj, "product_code", "") or "",
        "pattern_code": getattr(obj, "pattern_code", "") or "",
        "quantity": _dec_str(obj.quantity),
        "pcs": str(obj.pcs) if obj.pcs is not None else "",
        "g_wt": _dec_str(obj.g_wt),
        "less_wt": _dec_str(getattr(obj, "less_wt", None)),
        "quality_check": getattr(obj, "quality_check", "") or "",
        "is_exclusive": bool(getattr(obj, "is_exclusive", False)),
        "is_rare_find": bool(getattr(obj, "is_rare_find", False)),
        "is_limited": bool(getattr(obj, "is_limited", False)),
        "is_bestseller": bool(getattr(obj, "is_bestseller", False)),
        "line_of_business": getattr(obj, "line_of_business", "") or "",
        "sub_line_of_business": getattr(obj, "sub_line_of_business", "") or "",
        "brand": getattr(obj, "brand", "") or "",
        "occasion": getattr(obj, "occasion", "") or "",
        "bom": obj.bom or "",
        "status": obj.status or "",
        "system_updated_at": (
            obj.system_updated_at.isoformat()
            if getattr(obj, "system_updated_at", None)
            else ""
        ),
        "updated_by_name": updated_by_name or "",
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


def _next_lot_no():
    n = GrnLot.objects.count() + 1
    return f"LOT-{n:05d}"


def _batch_fully_depleted(batch):
    """True when qty/PCS/G-wt balances are exhausted. Stone stays on batch for Make Bag."""
    checks = []
    if batch.g_wt is not None:
        checks.append(batch.g_wt <= 0)
    if batch.quantity is not None:
        checks.append(batch.quantity <= 0)
    if batch.pcs is not None:
        checks.append(batch.pcs <= 0)
    return bool(checks) and all(checks)


def _reopen_batch_if_needed(batch, update_fields):
    st = (batch.status or "").strip().lower()
    if st in ("closed", "completed") and not _batch_fully_depleted(batch):
        batch.status = "Open"
        update_fields.append("status")


def _restore_lot_allocations_to_batch(batch, lot_qty, lot_pcs, lot_g_wt):
    """Reverse lot create/update deduction — return batch to pre-lot balance."""
    update_fields = []
    if batch.quantity is not None and lot_qty is not None:
        batch.quantity = (batch.quantity or Decimal("0")) + lot_qty
        update_fields.append("quantity")
    if batch.pcs is not None and lot_pcs is not None:
        batch.pcs = (batch.pcs or 0) + int(lot_pcs)
        update_fields.append("pcs")
    if batch.g_wt is not None and lot_g_wt is not None and lot_g_wt > 0:
        batch.g_wt = _quantize_g_wt((batch.g_wt or Decimal("0")) + lot_g_wt)
        update_fields.append("g_wt")
    _reopen_batch_if_needed(batch, update_fields)
    return update_fields


def _deduct_lot_allocations_from_batch(batch, lot_qty, lot_pcs, lot_g_wt):
    """
    Subtract lot allocations from batch balances (qty / PCS / G-wt only).
    Returns (batch_update_fields, error_response).

    Rule: if this lot consumes ALL remaining PCS (or ALL remaining quantity),
    it must also consume ALL remaining gross weight — otherwise leftover weight
    with zero pieces is an invalid entry.
    """
    batch_update_fields = []

    rem_pcs = batch.pcs if batch.pcs is not None else None
    rem_qty = batch.quantity if batch.quantity is not None else None
    rem_g = _quantize_g_wt(batch.g_wt) if batch.g_wt is not None else None
    lot_g = _quantize_g_wt(lot_g_wt) if lot_g_wt is not None else None

    consuming_all_pcs = (
        rem_pcs is not None
        and lot_pcs is not None
        and rem_pcs > 0
        and int(lot_pcs) == int(rem_pcs)
    )
    consuming_all_qty = (
        rem_qty is not None
        and lot_qty is not None
        and rem_qty > 0
        and lot_qty == rem_qty
    )

    if (consuming_all_pcs or consuming_all_qty) and rem_g is not None and rem_g > 0:
        if lot_g is None or lot_g <= 0:
            which = "PCS" if consuming_all_pcs else "quantity"
            return batch_update_fields, Response(
                {
                    "g_wt": [
                        f"When using all remaining batch {which} ({rem_pcs if consuming_all_pcs else rem_qty}), "
                        f"lot G-wt must equal remaining batch weight ({rem_g})."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if lot_g != rem_g:
            which = "PCS" if consuming_all_pcs else "quantity"
            return batch_update_fields, Response(
                {
                    "g_wt": [
                        f"When using all remaining batch {which}, lot G-wt must be {rem_g} "
                        f"(full remaining weight), not {lot_g}."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    if batch.g_wt is not None and lot_g_wt is not None and lot_g_wt > 0:
        remaining = _quantize_g_wt(batch.g_wt)
        if remaining <= 0:
            return batch_update_fields, Response(
                {
                    "g_wt": [
                        "Lot gross weight exceeds remaining batch gross weight (0)."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if lot_g_wt > remaining:
            return batch_update_fields, Response(
                {
                    "g_wt": [
                        f"Lot gross weight ({lot_g_wt}) exceeds remaining batch "
                        f"gross weight ({remaining})."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        new_rem = remaining - lot_g_wt
        batch.g_wt = (
            _quantize_g_wt(new_rem)
            if new_rem > 0
            else Decimal("0").quantize(_G_WT_QUANT, rounding=ROUND_HALF_UP)
        )
        batch_update_fields.append("g_wt")

    if batch.quantity is not None and lot_qty is not None:
        if lot_qty < 0:
            return batch_update_fields, Response(
                {"quantity": ["Quantity cannot be negative."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        br = batch.quantity
        if lot_qty > br:
            return batch_update_fields, Response(
                {
                    "quantity": [
                        f"Lot quantity ({lot_qty}) exceeds remaining batch quantity ({br})."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        batch.quantity = br - lot_qty
        if batch.quantity < 0:
            batch.quantity = Decimal("0")
        batch_update_fields.append("quantity")

    if batch.pcs is not None and lot_pcs is not None:
        if lot_pcs < 0:
            return batch_update_fields, Response(
                {"pcs": ["PCS cannot be negative."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if lot_pcs > batch.pcs:
            return batch_update_fields, Response(
                {
                    "pcs": [
                        f"Lot PCS ({lot_pcs}) exceeds remaining batch PCS ({batch.pcs})."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        batch.pcs = batch.pcs - lot_pcs
        if batch.pcs < 0:
            batch.pcs = 0
        batch_update_fields.append("pcs")

    if _batch_fully_depleted(batch):
        batch.status = "Closed"
        batch_update_fields.append("status")

    return batch_update_fields, None


def _lot_has_positive_allocation(lot_qty, lot_pcs, lot_g_wt):
    if lot_qty is not None and lot_qty > 0:
        return True
    if lot_pcs is not None and lot_pcs > 0:
        return True
    if lot_g_wt is not None and lot_g_wt > 0:
        return True
    return False


def _allocation_changed(old_qty, old_pcs, old_g_wt, new_qty, new_pcs, new_g_wt):
    def _eq(a, b):
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return a == b

    return not (
        _eq(old_qty, new_qty)
        and _eq(old_pcs, new_pcs)
        and _eq(old_g_wt, new_g_wt)
    )


def _parse_opt_pk(raw, field_name, errors):
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        errors.setdefault(field_name, []).append("Must be a valid integer.")
        return None


def _opt_decimal_silent(raw):
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


_QC_ALLOWED = {"Approve", "Pending", "Reject", ""}


def _parse_bool(raw):
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_lot_create(data, errors):
    values = {}
    bdn = data.get("batch_doc_no")
    if bdn is None or not str(bdn).strip():
        errors.setdefault("batch_doc_no", []).append("This field is required.")
    else:
        values["batch_doc_no"] = str(bdn).strip()

    lot_no = (data.get("lot_no") or "").strip()
    values["lot_no"] = lot_no if lot_no else _next_lot_no()

    cat_pk = _parse_opt_pk(data.get("category_id"), "category_id", errors)
    sub_pk = _parse_opt_pk(data.get("subcategory_id"), "subcategory_id", errors)
    if cat_pk is None and not errors.get("category_id"):
        errors.setdefault("category_id", []).append("This field is required.")
    if sub_pk is None and not errors.get("subcategory_id"):
        errors.setdefault("subcategory_id", []).append("This field is required.")

    if cat_pk is not None and sub_pk is not None:
        sub = (
            Subcategory.objects.select_related("category")
            .filter(pk=sub_pk, is_active=True)
            .first()
        )
        if not sub:
            errors.setdefault("subcategory_id", []).append(
                "Invalid or inactive item type (subcategory)."
            )
        elif sub.category_id != cat_pk:
            errors.setdefault("subcategory_id", []).append(
                "Item type must belong to the selected item group (category)."
            )
        else:
            values["category_id"] = cat_pk
            values["subcategory_id"] = sub_pk

    prod = data.get("product_code")
    values["product_code"] = "" if prod is None else str(prod).strip()
    if not values["product_code"] and values.get("category_id") and values.get("subcategory_id"):
        from shared.services.product_code_prefix import resolve_prefix_for_group

        resolved = resolve_prefix_for_group(
            values["category_id"],
            values["subcategory_id"],
        )
        if resolved:
            values["product_code"] = resolved

    pc = data.get("pattern_code")
    values["pattern_code"] = "" if pc is None else str(pc).strip().upper().replace(" ", "")

    bom_v = data.get("bom")
    values["bom"] = "" if bom_v is None else str(bom_v).strip()

    qc = "" if data.get("quality_check") is None else str(data.get("quality_check")).strip()
    if qc not in _QC_ALLOWED:
        errors.setdefault("quality_check", []).append(
            "QC must be Approve, Pending, or Reject."
        )
    else:
        values["quality_check"] = qc

    values["is_exclusive"] = _parse_bool(data.get("is_exclusive"))
    values["is_rare_find"] = _parse_bool(data.get("is_rare_find"))
    values["is_limited"] = _parse_bool(data.get("is_limited"))
    values["is_bestseller"] = _parse_bool(data.get("is_bestseller"))
    if values["quality_check"] != "Approve":
        # Flags only apply when Approved (Type-1).
        values["is_exclusive"] = False
        values["is_rare_find"] = False
        values["is_limited"] = False
        values["is_bestseller"] = False

    for f in (
        "line_of_business",
        "sub_line_of_business",
        "brand",
        "occasion",
    ):
        v = data.get(f)
        values[f] = "" if v is None else str(v).strip()[:128]

    err_before = len(errors.get("quantity", []))
    parsed = _opt_decimal(data.get("quantity", ""), "quantity", errors)
    if len(errors.get("quantity", [])) == err_before:
        values["quantity"] = parsed

    err_before = len(errors.get("g_wt", []))
    parsed = parse_optional_weight_decimal(data.get("g_wt", ""), "g_wt", errors)
    if len(errors.get("g_wt", [])) == err_before:
        values["g_wt"] = parsed

    err_before = len(errors.get("less_wt", []))
    parsed = parse_optional_weight_decimal(data.get("less_wt", ""), "less_wt", errors)
    if len(errors.get("less_wt", [])) == err_before:
        values["less_wt"] = parsed

    if (
        values.get("g_wt") is not None
        and values.get("less_wt") is not None
        and values["less_wt"] > values["g_wt"]
    ):
        errors.setdefault("less_wt", []).append(
            "Less weight cannot exceed gross weight."
        )

    err_before = len(errors.get("pcs", []))
    parsed = _opt_int(data.get("pcs", ""), "pcs", errors)
    if len(errors.get("pcs", [])) == err_before:
        values["pcs"] = parsed

    st = (data.get("status") or "").strip()
    values["status"] = st or "Open"
    return values


@api_view(["GET", "POST"])
@admin_auth()
def grn_lot_list_create(request):
    if request.method == "GET":
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_LOT_VIEW")
        if denied:
            return denied
    else:
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_LOT_CREATE")
        if denied:
            return denied

    if request.method == "GET":
        qs = GrnLot.objects.select_related(
            "category", "subcategory", "batch", "updated_by"
        ).all()
        search = request.GET.get("search")
        if search:
            term = search.strip()
            qs = qs.filter(
                Q(lot_no__icontains=term)
                | Q(batch_doc_no__icontains=term)
                | Q(category__name__icontains=term)
                | Q(subcategory__name__icontains=term)
                | Q(pattern_code__icontains=term)
                | Q(product_code__icontains=term)
                | Q(vendor_variant_name__icontains=term)
                | Q(bom__icontains=term)
                | Q(status__icontains=term)
            )
        for param in ("batch_doc_no", "pattern_code", "product_code", "status"):
            v = request.GET.get(param)
            if v:
                qs = qs.filter(**{f"{param}__iexact": v})
        # Legacy query param still accepted but ignored (metal removed from lots).
        ordering = request.GET.get("ordering", "-system_created_at")
        order_expr = _ORDER_MAP.get(ordering.strip(), "-system_created_at")
        qs = qs.order_by(order_expr)
        return Response({"results": [lot_to_dict(o) for o in qs]})

    data = request.data if isinstance(request.data, dict) else {}
    errors = {}
    values = _parse_lot_create(data, errors)
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)

    batch_doc = str(values["batch_doc_no"]).strip()
    batch_id = _parse_batch_id(data)

    if values.get("g_wt") is not None:
        values["g_wt"] = _quantize_g_wt(values["g_wt"])
    if values.get("less_wt") is not None:
        values["less_wt"] = _quantize_g_wt(values["less_wt"])
    lot_g_wt = values.get("g_wt")
    lot_qty = values.get("quantity")
    lot_pcs = values.get("pcs")
    is_draft = (values.get("status") or "").strip().lower() == "draft"

    if not is_draft and not _lot_has_positive_allocation(lot_qty, lot_pcs, lot_g_wt):
        return Response(
            {
                "detail": [
                    "Enter quantity, PCS, or gross weight for this lot."
                ]
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    from master.views.product_views import get_admin_user_from_request

    admin_user = get_admin_user_from_request(request)

    with transaction.atomic():
        if batch_id is not None:
            batch = (
                GrnBatch.objects.select_for_update()
                .filter(pk=batch_id)
                .first()
            )
            if (
                batch
                and str(batch.doc_no).strip().casefold() != batch_doc.casefold()
            ):
                return Response(
                    {
                        "batch_doc_no": [
                            "batch_id does not match this document number. "
                            "Refresh the batch list and try again."
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            batch = (
                GrnBatch.objects.select_for_update()
                .filter(doc_no__iexact=batch_doc)
                .first()
            )

        if not batch:
            return Response(
                {"batch_doc_no": ["No batch found with this document number."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        st = (batch.status or "").strip().lower()
        if st in ("closed", "completed"):
            return Response(
                {"detail": ["This batch is completed; no new lots can be created."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch_update_fields = []

        if not is_draft and _lot_has_positive_allocation(lot_qty, lot_pcs, lot_g_wt):
            batch_update_fields, err = _deduct_lot_allocations_from_batch(
                batch, lot_qty, lot_pcs, lot_g_wt
            )
            if err is not None:
                return err

            if batch_update_fields:
                batch_update_fields = list(dict.fromkeys(batch_update_fields))
                if "system_updated_at" not in batch_update_fields:
                    batch_update_fields.append("system_updated_at")
                batch.save(update_fields=batch_update_fields)

        # Persist both the real FK and the legacy doc string. Reads can keep
        # using batch_doc_no for now; new code should prefer lot.batch.
        create_kwargs = dict(values)
        if admin_user is not None:
            create_kwargs["created_by"] = admin_user
            create_kwargs["updated_by"] = admin_user
        obj = GrnLot.objects.create(batch=batch, **create_kwargs)

    obj = GrnLot.objects.select_related(
        "category", "subcategory", "batch", "updated_by"
    ).get(pk=obj.pk)
    return Response(lot_to_dict(obj), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "PATCH"])
@admin_auth()
def grn_lot_detail(request, pk):
    """
    GET/PUT/PATCH /master/grn-lots/<pk>/
    Update lot metadata always; adjust batch balances when allocations change
    and the lot has no bags yet.
    """
    lot = (
        GrnLot.objects.select_related(
            "category", "subcategory", "batch", "updated_by"
        )
        .filter(pk=pk)
        .first()
    )
    if not lot:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_LOT_VIEW")
        if denied:
            return denied
        return Response(lot_to_dict(lot))

    denied = ensure_admin_permission(request, "CRM_MASTERS_GRN_LOT_UPDATE")
    if denied:
        return denied

    data = request.data if isinstance(request.data, dict) else {}
    errors = {}
    values = _parse_lot_create(data, errors)
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)

    if values.get("g_wt") is not None:
        values["g_wt"] = _quantize_g_wt(values["g_wt"])
    if values.get("less_wt") is not None:
        values["less_wt"] = _quantize_g_wt(values["less_wt"])
    new_qty = values.get("quantity")
    new_pcs = values.get("pcs")
    new_g_wt = values.get("g_wt")
    is_draft = (values.get("status") or "").strip().lower() == "draft"

    if not is_draft and not _lot_has_positive_allocation(new_qty, new_pcs, new_g_wt):
        return Response(
            {
                "detail": [
                    "Enter quantity, PCS, or gross weight for this lot."
                ]
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    bag_count = GrnBag.objects.filter(lot_id=lot.pk).count()
    alloc_changed = _allocation_changed(
        lot.quantity,
        lot.pcs,
        lot.g_wt,
        new_qty,
        new_pcs,
        new_g_wt,
    )
    if bag_count > 0 and alloc_changed:
        return Response(
            {
                "detail": [
                    "Cannot change lot quantity, PCS, or weights after bags have been "
                    "created from this lot. Edit other fields only."
                ]
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    batch = lot.batch
    if batch is None and lot.batch_doc_no:
        batch = GrnBatch.objects.filter(
            doc_no__iexact=str(lot.batch_doc_no).strip()
        ).first()

    from master.views.product_views import get_admin_user_from_request

    admin_user = get_admin_user_from_request(request)

    with transaction.atomic():
        had_allocation = _lot_has_positive_allocation(
            lot.quantity, lot.pcs, lot.g_wt
        )
        will_allocate = (not is_draft) and _lot_has_positive_allocation(
            new_qty, new_pcs, new_g_wt
        )
        if batch is not None and bag_count == 0 and (alloc_changed or (had_allocation != will_allocate)):
            batch = GrnBatch.objects.select_for_update().get(pk=batch.pk)
            restore_fields = []
            if had_allocation:
                restore_fields = _restore_lot_allocations_to_batch(
                    batch, lot.quantity, lot.pcs, lot.g_wt
                )
            deduct_fields = []
            if will_allocate:
                deduct_fields, err = _deduct_lot_allocations_from_batch(
                    batch, new_qty, new_pcs, new_g_wt
                )
                if err is not None:
                    transaction.set_rollback(True)
                    return err
            all_batch_fields = list(
                dict.fromkeys(restore_fields + deduct_fields + ["system_updated_at"])
            )
            if all_batch_fields:
                batch.save(update_fields=all_batch_fields)

        lot_no = (values.get("lot_no") or lot.lot_no or "").strip()
        lot.lot_no = lot_no or lot.lot_no
        if values.get("category_id") is not None:
            lot.category_id = values["category_id"]
        if values.get("subcategory_id") is not None:
            lot.subcategory_id = values["subcategory_id"]
        lot.product_code = values.get("product_code", lot.product_code or "")
        lot.pattern_code = values.get("pattern_code", lot.pattern_code or "")
        lot.quantity = new_qty
        lot.pcs = new_pcs
        lot.g_wt = new_g_wt
        lot.less_wt = values.get("less_wt")
        lot.quality_check = values.get("quality_check", "") or ""
        lot.is_exclusive = bool(values.get("is_exclusive"))
        lot.is_rare_find = bool(values.get("is_rare_find"))
        lot.is_limited = bool(values.get("is_limited"))
        lot.is_bestseller = bool(values.get("is_bestseller"))
        lot.line_of_business = values.get("line_of_business", "") or ""
        lot.sub_line_of_business = values.get("sub_line_of_business", "") or ""
        lot.brand = values.get("brand", "") or ""
        lot.occasion = values.get("occasion", "") or ""
        lot.bom = values.get("bom", lot.bom or "")
        st = (values.get("status") or lot.status or "").strip()
        lot.status = st or lot.status or "Open"
        if batch is not None:
            lot.batch = batch
            lot.batch_doc_no = batch.doc_no or lot.batch_doc_no
        if admin_user is not None:
            lot.updated_by = admin_user
        lot.save()

    lot = GrnLot.objects.select_related(
        "category", "subcategory", "batch", "updated_by"
    ).get(pk=lot.pk)
    return Response(lot_to_dict(lot))


def _opt_int_param(raw):
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def grn_lot_vendor_terms(request):
    """
    Display-only vendor terms for lot creation: match batch vendor + pattern/product
    to ProductItemLinkedVendor (vendor_variant_name, delivery_days, validity).
    """
    vendor_name = (request.GET.get("vendor_name") or "").strip()
    batch_id = _opt_int_param(request.GET.get("batch_id"))
    batch_doc_no = (request.GET.get("batch_doc_no") or "").strip()

    if not vendor_name and (batch_id or batch_doc_no):
        batch = None
        if batch_id:
            batch = GrnBatch.objects.filter(pk=batch_id).first()
        elif batch_doc_no:
            batch = GrnBatch.objects.filter(doc_no__iexact=batch_doc_no).first()
        if batch:
            vendor_name = (batch.vendor or "").strip()

    payload = resolve_lot_linked_vendor_terms(
        vendor_name=vendor_name,
        pattern_code=(request.GET.get("pattern_code") or "").strip(),
        product_code=(request.GET.get("product_code") or "").strip(),
        category_id=_opt_int_param(request.GET.get("category_id")),
        subcategory_id=_opt_int_param(request.GET.get("subcategory_id")),
        product_item_id=_opt_int_param(request.GET.get("product_item_id")),
        ProductItem=ProductItem,
        ProductItemLinkedVendor=ProductItemLinkedVendor,
        Vendor=Vendor,
    )
    return Response(payload)
