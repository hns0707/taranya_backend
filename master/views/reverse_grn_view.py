"""
Reverse GRN — return unbarcoded bag receive back to lot, then lot remainder to batch.

Flow: BAG NO → verify metal/purity → reverse unbarcoded qty
      → verify pattern/product on lot → reverse lot remainder to batch
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from master.views.grn_lot_view import (
    _G_WT_QUANT,
    _quantize_g_wt,
    _restore_lot_allocations_to_batch,
    lot_to_dict,
)
from master.views.make_bag_view import _bag_already_received
from master.views.product_views import get_admin_user_from_request
from master.views.barcode_view import (
    _bag_received_qty,
    _bag_tag_count,
    _build_weight_summary,
    _dec,
)
from shared.models import (
    GrnBag,
    GrnBatch,
    GrnLot,
    ProductBOM,
    ProductItem,
    ProductTag,
)
from shared.product_item_size import serialize_product_item_size_for_api
from shared.services.stock_service import adjust_product_item_qty


def _metal_purity_for_item(item):
    if not item:
        return "", ""
    for b in item.bom_items.all():
        if b.material_type == "METAL" and b.metal_id:
            metal = b.metal.metal_name if b.metal else ""
            purity = b.purity.purity_name if b.purity_id and b.purity else ""
            return metal, purity
    return "", ""


def _bag_reversible_amounts(bag):
    """
    Compute qty/pcs/g-wt that can be reversed (unbarcoded remainder only).
    """
    stock_total = _bag_received_qty(bag)
    tag_count = _bag_tag_count(bag.id)
    reversible_qty = max(stock_total - tag_count, 0)

    item = bag.product_item
    tags_qs = ProductTag.objects.filter(grn_bag_id=bag.id, is_active=True)
    tags = [
        {
            "gross_weight": t.gross_weight,
            "net_weight": t.net_weight,
        }
        for t in tags_qs
    ]

    metal_total_wt = 0.0
    stone_total_wt = 0.0
    stone_total_pcs = 0
    if item:
        for b in ProductBOM.objects.filter(product=item):
            if b.material_type == "METAL":
                metal_total_wt += float(b.weight or 0)
            elif b.material_type == "STONE":
                stone_total_wt += float(b.weight or 0)
                stone_total_pcs += int(b.pcs or 0)

    ws = _build_weight_summary(
        item=item,
        stock_total=stock_total,
        tags=tags,
        metal_total_wt=metal_total_wt,
        stone_total_wt=stone_total_wt,
        stone_total_pcs=stone_total_pcs,
        bag=bag,
    )

    bag_pcs = bag.pcs if bag.pcs is not None else stock_total
    reversible_pcs = reversible_qty
    if stock_total > 0 and bag_pcs is not None:
        reversible_pcs = max(
            0,
            int(
                round(
                    Decimal(bag_pcs) * Decimal(reversible_qty) / Decimal(stock_total)
                )
            ),
        )

    reversible_g_wt = Decimal(str(ws.get("remaining_gross") or "0"))
    if reversible_g_wt < 0:
        reversible_g_wt = Decimal("0")

    return {
        "stock_total": stock_total,
        "tags_created": tag_count,
        "reversible_qty": reversible_qty,
        "reversible_pcs": reversible_pcs,
        "reversible_g_wt": _dec(reversible_g_wt),
        "reversible_g_wt_dec": reversible_g_wt,
        "weight_summary": ws,
    }


def _serialize_bag_for_reverse(bag):
    item = bag.product_item
    lot = bag.lot
    sku = item.sku if item and item.sku_id else None
    pg = sku.product_group if sku else None
    metal, purity = _metal_purity_for_item(item)
    rev = _bag_reversible_amounts(bag)

    row = {
        "id": bag.id,
        "bag_no": bag.bag_no or "",
        "remark": bag.remark or "",
        "quantity": bag.quantity,
        "pcs": bag.pcs,
        "g_wt": _dec(bag.g_wt) if bag.g_wt is not None else "",
        "product_item_id": item.id if item else None,
        "product_code": (sku.product_code or "") if sku else "",
        "pattern_code": (sku.pattern_code or "") if sku else "",
        "sku_code": (sku.sku_code or "") if sku else "",
        "style_name": (pg.style_name if pg else "") or "",
        "metal": metal,
        "purity": purity,
        "lot": lot_to_dict(lot) if lot else None,
        "lot_id": lot.id if lot else None,
        "stock_total": rev["stock_total"],
        "tags_created": rev["tags_created"],
        "reversible_qty": rev["reversible_qty"],
        "reversible_pcs": rev["reversible_pcs"],
        "reversible_g_wt": rev["reversible_g_wt"],
        "weight_summary": rev["weight_summary"],
        "can_reverse": rev["reversible_qty"] > 0 and _bag_already_received(bag),
    }
    if item:
        row.update(serialize_product_item_size_for_api(item))
    return row


def _restore_bag_to_lot(lot, *, stock_qty_int, pcs_to_add, g_delta_opt):
    """Inverse of make_bag _save_lot_after_consuming_bag."""
    update_fields = []
    if lot.quantity is not None:
        lot.quantity = (lot.quantity or Decimal("0")) + Decimal(str(int(stock_qty_int)))
        update_fields.append("quantity")
    if lot.pcs is not None:
        lot.pcs = (lot.pcs or 0) + int(pcs_to_add)
        update_fields.append("pcs")
    if lot.g_wt is not None and g_delta_opt is not None and g_delta_opt > 0:
        lot.g_wt = _quantize_g_wt((lot.g_wt or Decimal("0")) + g_delta_opt)
        update_fields.append("g_wt")
    st = (lot.status or "").strip()
    if st in ("Closed", "Completed"):
        lot.status = "Open"
        update_fields.append("status")
    if update_fields:
        update_fields = list(dict.fromkeys(update_fields + ["system_updated_at"]))
        lot.save(update_fields=update_fields)


def _apply_bag_reverse_snapshot(bag, *, reverse_qty, reverse_pcs, reverse_g_wt, tag_count):
    """Shrink bag receive snapshot; keep tagged portion when tags exist."""
    stock_total = _bag_received_qty(bag)
    new_qty = max(0, stock_total - int(reverse_qty))
    new_pcs = None
    if bag.pcs is not None:
        new_pcs = max(0, int(bag.pcs) - int(reverse_pcs))
    elif new_qty > 0:
        new_pcs = new_qty

    new_g_wt = None
    if bag.g_wt is not None and reverse_g_wt is not None:
        rem = _quantize_g_wt(bag.g_wt) - _quantize_g_wt(reverse_g_wt)
        new_g_wt = rem if rem > 0 else Decimal("0").quantize(_G_WT_QUANT, rounding=ROUND_HALF_UP)

    if new_qty <= 0 and tag_count <= 0:
        return "delete"

    bag.quantity = new_qty if new_qty > 0 else tag_count
    update_fields = ["quantity", "system_updated_at"]
    if new_pcs is not None:
        bag.pcs = new_pcs if new_pcs > 0 else tag_count
        update_fields.append("pcs")
    if new_g_wt is not None:
        bag.g_wt = new_g_wt
        update_fields.append("g_wt")
    bag.save(update_fields=update_fields)
    return "updated"


def _parse_positive_int(raw, field, errors, *, required=True):
    if raw is None or raw == "":
        if required:
            errors.setdefault(field, []).append("Required.")
        return None
    try:
        n = int(str(raw).strip())
        if n <= 0:
            errors.setdefault(field, []).append("Must be greater than zero.")
            return None
        return n
    except (TypeError, ValueError):
        errors.setdefault(field, []).append("Invalid integer.")
        return None


def _parse_opt_g_wt(raw, field, errors):
    if raw is None or raw == "":
        return None
    try:
        n = _quantize_g_wt(raw)
        if n <= 0:
            errors.setdefault(field, []).append("Must be greater than zero.")
            return None
        return n
    except Exception:
        errors.setdefault(field, []).append("Invalid weight.")
        return None


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def reverse_grn_bag_lookup(request):
    """
    GET /master/reverse-grn/bag/?bag_no=BAG-00001
    GET /master/reverse-grn/bag/?bag_id=123
    """
    bag_id = request.GET.get("bag_id")
    bag_no = (request.GET.get("bag_no") or "").strip()

    qs = GrnBag.objects.select_related(
        "lot",
        "lot__batch",
        "product_item",
        "product_item__sku",
        "product_item__sku__product_group",
    ).prefetch_related(
        "product_item__bom_items__metal",
        "product_item__bom_items__purity",
    )

    bag = None
    if bag_id:
        try:
            bag = qs.filter(pk=int(bag_id)).first()
        except (TypeError, ValueError):
            return Response({"errors": {"bag_id": ["Invalid."]}}, status=status.HTTP_400_BAD_REQUEST)
    elif bag_no:
        bag = qs.filter(bag_no__iexact=bag_no).order_by("-system_created_at").first()
    else:
        return Response(
            {"errors": {"bag_no": ["Provide bag_no or bag_id."]}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not bag:
        return Response({"detail": "Bag not found."}, status=status.HTTP_404_NOT_FOUND)
    if not bag.product_item_id:
        return Response(
            {"detail": "This bag has no catalog item linked."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not _bag_already_received(bag):
        return Response(
            {"detail": "This bag was never received into stock."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(_serialize_bag_for_reverse(bag))


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_LOT_UPDATE")
def reverse_grn_bag(request):
    """
    POST /master/reverse-grn/bag/
    Reverse unbarcoded bag qty back to the parent lot.
    Body: grn_bag_id, qty (optional — defaults to full reversible),
          confirm_metal, confirm_purity (must match catalog BOM).
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

    data = request.data if isinstance(request.data, dict) else {}
    errors = {}

    try:
        bag_id = int(data.get("grn_bag_id"))
    except (TypeError, ValueError):
        errors.setdefault("grn_bag_id", []).append("Required.")
        bag_id = None

    confirm_metal = (data.get("confirm_metal") or "").strip()
    confirm_purity = (data.get("confirm_purity") or "").strip()
    if not confirm_metal:
        errors.setdefault("confirm_metal", []).append("Required — must match catalog metal.")
    if not confirm_purity:
        errors.setdefault("confirm_purity", []).append("Required — must match catalog purity.")

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    bag = (
        GrnBag.objects.select_related("lot", "product_item")
        .prefetch_related("product_item__bom_items__metal", "product_item__bom_items__purity")
        .filter(pk=bag_id)
        .first()
    )
    if not bag:
        return Response({"detail": "Bag not found."}, status=status.HTTP_404_NOT_FOUND)
    if not bag.product_item_id or not bag.lot_id:
        return Response(
            {"detail": "Bag must be linked to a lot and catalog item."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not _bag_already_received(bag):
        return Response(
            {"detail": "This bag was never received into stock."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    item = bag.product_item
    metal, purity = _metal_purity_for_item(item)
    if confirm_metal.lower() != (metal or "").lower():
        return Response(
            {"errors": {"confirm_metal": [f"Must match catalog metal ({metal or '—'})."]}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if confirm_purity.lower() != (purity or "").lower():
        return Response(
            {"errors": {"confirm_purity": [f"Must match catalog purity ({purity or '—'})."]}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    rev = _bag_reversible_amounts(bag)
    max_qty = rev["reversible_qty"]
    if max_qty <= 0:
        return Response(
            {"detail": "Nothing to reverse — all pieces on this bag already have barcodes."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reverse_qty = _parse_positive_int(data.get("qty"), "qty", errors, required=False)
    if reverse_qty is None:
        reverse_qty = max_qty
    elif reverse_qty > max_qty:
        errors.setdefault("qty", []).append(f"Cannot reverse more than {max_qty} unbarcoded piece(s).")

    reverse_pcs = _parse_positive_int(data.get("pcs"), "pcs", errors, required=False)
    if reverse_pcs is None:
        reverse_pcs = rev["reversible_pcs"]
        if reverse_qty < max_qty and max_qty > 0:
            stock_total = rev["stock_total"]
            bag_pcs = bag.pcs if bag.pcs is not None else stock_total
            reverse_pcs = max(
                1,
                int(round(Decimal(bag_pcs) * Decimal(reverse_qty) / Decimal(stock_total))),
            )

    reverse_g_wt = _parse_opt_g_wt(data.get("g_wt"), "g_wt", errors)
    if reverse_g_wt is None:
        if reverse_qty >= max_qty:
            reverse_g_wt = rev["reversible_g_wt_dec"]
        elif max_qty > 0:
            reverse_g_wt = _quantize_g_wt(
                rev["reversible_g_wt_dec"] * Decimal(reverse_qty) / Decimal(max_qty)
            )

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    tag_count = rev["tags_created"]
    lot_id = bag.lot_id
    item_id = bag.product_item_id
    bag_no = bag.bag_no

    with transaction.atomic():
        bag = GrnBag.objects.select_for_update().get(pk=bag_id)
        lot = GrnLot.objects.select_for_update().get(pk=lot_id)
        item = ProductItem.objects.select_for_update().get(pk=item_id)

        rev_check = _bag_reversible_amounts(bag)
        if reverse_qty > rev_check["reversible_qty"]:
            return Response(
                {"errors": {"qty": ["Concurrent change — refresh and retry."]}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        adjust_product_item_qty(
            product_item=item,
            delta=-int(reverse_qty),
            txn_type="bag_out",
            admin=admin,
            bag=bag,
            reference=bag_no or "",
            notes=f"Reverse GRN bag — returned {reverse_qty} pc(s) to lot {lot.lot_no}",
        )

        _restore_bag_to_lot(
            lot,
            stock_qty_int=reverse_qty,
            pcs_to_add=reverse_pcs,
            g_delta_opt=reverse_g_wt,
        )

        action = _apply_bag_reverse_snapshot(
            bag,
            reverse_qty=reverse_qty,
            reverse_pcs=reverse_pcs,
            reverse_g_wt=reverse_g_wt,
            tag_count=tag_count,
        )
        if action == "delete":
            bag.delete()

    remaining_bag = (
        GrnBag.objects.filter(pk=bag_id).first() if action != "delete" else None
    )
    lot = GrnLot.objects.select_related("category", "subcategory").get(pk=lot_id)

    return Response(
        {
            "ok": True,
            "bag_deleted": action == "delete",
            "reversed_qty": reverse_qty,
            "reversed_pcs": reverse_pcs,
            "reversed_g_wt": _dec(reverse_g_wt),
            "bag": _serialize_bag_for_reverse(remaining_bag) if remaining_bag else None,
            "lot": lot_to_dict(lot),
        }
    )


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_LOT_VIEW")
def reverse_grn_lot_lookup(request, lot_id):
    """
    GET /master/reverse-grn/lot/<lot_id>/
    Lot remainder available to return to batch (after bag reverses).
    """
    lot = (
        GrnLot.objects.select_related("category", "subcategory", "batch")
        .filter(pk=lot_id)
        .first()
    )
    if not lot:
        return Response({"detail": "Lot not found."}, status=status.HTTP_404_NOT_FOUND)

    batch = lot.batch
    if batch is None and lot.batch_doc_no:
        batch = GrnBatch.objects.filter(doc_no__iexact=str(lot.batch_doc_no).strip()).first()

    qty = int(lot.quantity) if lot.quantity is not None else 0
    pcs = int(lot.pcs) if lot.pcs is not None else 0
    g_wt = _dec(lot.g_wt) if lot.g_wt is not None else ""

    bag_ids = list(GrnBag.objects.filter(lot_id=lot.id).values_list("id", flat=True))
    bags_with_tags = 0
    if bag_ids:
        bags_with_tags = (
            ProductTag.objects.filter(grn_bag_id__in=bag_ids, is_active=True)
            .values("grn_bag_id")
            .distinct()
            .count()
        )

    return Response(
        {
            "lot": lot_to_dict(lot),
            "batch_doc_no": batch.doc_no if batch else lot.batch_doc_no,
            "batch_id": batch.id if batch else None,
            "reversible_qty": qty,
            "reversible_pcs": pcs,
            "reversible_g_wt": g_wt,
            "can_reverse": qty > 0 or pcs > 0 or (lot.g_wt is not None and lot.g_wt > 0),
            "bags_on_lot": len(bag_ids),
            "bags_with_barcodes": bags_with_tags,
        }
    )


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_LOT_UPDATE")
def reverse_grn_lot(request, lot_id):
    """
    POST /master/reverse-grn/lot/<lot_id>/
    Return lot remainder to parent batch.
    Body: confirm_pattern_code, confirm_product_code (must match lot),
          qty/pcs/g_wt optional (default = full lot remainder).
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

    lot = (
        GrnLot.objects.select_related("batch", "category", "subcategory")
        .filter(pk=lot_id)
        .first()
    )
    if not lot:
        return Response({"detail": "Lot not found."}, status=status.HTTP_404_NOT_FOUND)

    data = request.data if isinstance(request.data, dict) else {}
    errors = {}

    confirm_pattern = (data.get("confirm_pattern_code") or "").strip()
    confirm_product = (data.get("confirm_product_code") or "").strip()
    lot_pattern = (lot.pattern_code or "").strip()
    lot_product = (lot.product_code or "").strip()

    if not confirm_pattern:
        errors.setdefault("confirm_pattern_code", []).append("Required.")
    elif confirm_pattern.lower() != lot_pattern.lower():
        errors.setdefault("confirm_pattern_code", []).append(
            f"Must match lot pattern code ({lot_pattern or '—'})."
        )
    if not confirm_product:
        errors.setdefault("confirm_product_code", []).append("Required.")
    elif confirm_product.lower() != lot_product.lower():
        errors.setdefault("confirm_product_code", []).append(
            f"Must match lot product code ({lot_product or '—'})."
        )

    lot_qty = lot.quantity
    lot_pcs = lot.pcs
    lot_g_wt = lot.g_wt

    has_remainder = (
        (lot_qty is not None and lot_qty > 0)
        or (lot_pcs is not None and lot_pcs > 0)
        or (lot_g_wt is not None and lot_g_wt > 0)
    )
    if not has_remainder:
        return Response(
            {"detail": "This lot has no remaining quantity to reverse to the batch."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reverse_qty = lot_qty
    raw_qty = data.get("qty")
    if raw_qty not in (None, ""):
        reverse_qty = _parse_positive_int(raw_qty, "qty", errors, required=True)
        if reverse_qty is not None and lot_qty is not None and Decimal(str(reverse_qty)) > lot_qty:
            errors.setdefault("qty", []).append(f"Cannot exceed lot quantity ({lot_qty}).")

    reverse_pcs = lot_pcs
    raw_pcs = data.get("pcs")
    if raw_pcs not in (None, ""):
        reverse_pcs = _parse_positive_int(raw_pcs, "pcs", errors, required=True)
        if reverse_pcs is not None and lot_pcs is not None and reverse_pcs > lot_pcs:
            errors.setdefault("pcs", []).append(f"Cannot exceed lot PCS ({lot_pcs}).")

    reverse_g_wt = lot_g_wt
    raw_g = data.get("g_wt")
    if raw_g not in (None, ""):
        reverse_g_wt = _parse_opt_g_wt(raw_g, "g_wt", errors)
        if reverse_g_wt is not None and lot_g_wt is not None and reverse_g_wt > lot_g_wt:
            errors.setdefault("g_wt", []).append(f"Cannot exceed lot G-wt ({lot_g_wt}).")

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    batch = lot.batch
    if batch is None and lot.batch_doc_no:
        batch = GrnBatch.objects.filter(doc_no__iexact=str(lot.batch_doc_no).strip()).first()
    if batch is None:
        return Response(
            {"detail": "Parent batch not found for this lot."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        lot = GrnLot.objects.select_for_update().get(pk=lot_id)
        batch = GrnBatch.objects.select_for_update().get(pk=batch.pk)

        restore_fields = _restore_lot_allocations_to_batch(
            batch,
            reverse_qty if reverse_qty is not None else None,
            reverse_pcs if reverse_pcs is not None else None,
            reverse_g_wt if reverse_g_wt is not None else None,
        )
        if restore_fields:
            batch.save(update_fields=list(dict.fromkeys(restore_fields + ["system_updated_at"])))

        lot_update = []
        if lot.quantity is not None and reverse_qty is not None:
            lot.quantity = lot.quantity - Decimal(str(reverse_qty))
            if lot.quantity < 0:
                lot.quantity = Decimal("0")
            lot_update.append("quantity")
        if lot.pcs is not None and reverse_pcs is not None:
            lot.pcs = lot.pcs - int(reverse_pcs)
            if lot.pcs < 0:
                lot.pcs = 0
            lot_update.append("pcs")
        if lot.g_wt is not None and reverse_g_wt is not None:
            lot.g_wt = _quantize_g_wt(lot.g_wt - reverse_g_wt)
            if lot.g_wt < 0:
                lot.g_wt = Decimal("0").quantize(_G_WT_QUANT, rounding=ROUND_HALF_UP)
            lot_update.append("g_wt")

        depleted = True
        if lot.quantity is not None and lot.quantity > 0:
            depleted = False
        if lot.pcs is not None and lot.pcs > 0:
            depleted = False
        if lot.g_wt is not None and lot.g_wt > 0:
            depleted = False

        if depleted and not GrnBag.objects.filter(lot_id=lot.id).exists():
            lot.delete()
            lot_deleted = True
        else:
            if depleted:
                lot.status = "Closed"
                lot_update.append("status")
            lot.updated_by = admin
            lot_update.append("updated_by")
            lot_update.append("system_updated_at")
            lot.save(update_fields=list(dict.fromkeys(lot_update)))
            lot_deleted = False

    if lot_deleted:
        return Response({"ok": True, "lot_deleted": True, "batch_doc_no": batch.doc_no})
    lot = GrnLot.objects.select_related("category", "subcategory").get(pk=lot_id)
    return Response(
        {
            "ok": True,
            "lot_deleted": False,
            "lot": lot_to_dict(lot),
            "batch_doc_no": batch.doc_no,
        }
    )
