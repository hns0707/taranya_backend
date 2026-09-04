"""
Post-tag attribute mapping APIs (Attrib Type → Attrib Value per ProductTag).
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from master.permissions.permission_checker import admin_auth
from master.views.barcode_view import get_admin_user_from_request
from shared.models import (
    LookupValue,
    ProductBOM,
    ProductImage,
    ProductItem,
    ProductItemLinkedVendor,
    ProductTag,
    ProductTagAttributeValue,
    ProductTagPhoto,
    TagAttributeDefinition,
)
from shared.product_item_size import serialize_product_item_size_for_api
from shared.services.product_item_vendors import vendor_variant_name_for_item


def _lookup_options(defn):
    if defn.data_type != "lookup" or not defn.lookup_id:
        return []
    return list(
        LookupValue.objects.filter(lookup_id=defn.lookup_id, is_active=True)
        .order_by("label")
        .values("id", "code", "label")
    )


def _definition_row(defn, value_row=None):
    opts = _lookup_options(defn) if defn.data_type == "lookup" else []
    out = {
        "id": defn.id,
        "code": defn.code,
        "label": defn.label,
        "data_type": defn.data_type,
        "lookup_code": defn.lookup.code if defn.lookup_id else None,
        "required": defn.required,
        "sort_order": defn.sort_order,
        "help_text": defn.help_text or "",
        "lookup_options": opts,
        "value_text": "",
        "value_number": None,
        "lookup_value_id": None,
        "lookup_value_label": "",
    }
    if value_row:
        out["value_text"] = value_row.value_text or ""
        out["value_number"] = (
            str(value_row.value_number) if value_row.value_number is not None else None
        )
        out["lookup_value_id"] = value_row.lookup_value_id
        if value_row.lookup_value_id and value_row.lookup_value:
            out["lookup_value_label"] = value_row.lookup_value.label or ""
    return out


def _item_images(item: ProductItem | None, tag: ProductTag | None = None) -> list[str]:
    """Tag photos first, then bulk-imported product gallery (primary first)."""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(url: str | None):
        u = (url or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    if tag and tag.pk:
        for photo in ProductTagPhoto.objects.filter(product_tag=tag).order_by(
            "sort_order", "id"
        ):
            _add(photo.image_url)

    if item and item.pk:
        for img in ProductImage.objects.filter(product_id=item.id).order_by(
            "-is_primary", "id"
        ):
            _add(img.image_url)

    return urls


def _item_summary(item: ProductItem, tag: ProductTag | None = None) -> dict:
    sku = item.sku
    pg = sku.product_group if sku and sku.product_group_id else None
    metal_name = ""
    purity_name = ""
    if item.pk:
        metal_bom = (
            ProductBOM.objects.filter(product=item, material_type="METAL")
            .select_related("metal", "purity")
            .order_by("id")
            .first()
        )
        if metal_bom:
            if metal_bom.metal_id and metal_bom.metal:
                metal_name = metal_bom.metal.metal_name or ""
            if metal_bom.purity_id and metal_bom.purity:
                pr = metal_bom.purity
                purity_name = (pr.purity_name or pr.type or "").strip()

    images = _item_images(item, tag)
    return {
        "product_item_id": item.id,
        "product_code": sku.product_code if sku else "",
        "sku_code": sku.sku_code if sku else "",
        "store_variant_name": item.store_variant_name or "",
        "style_name": pg.style_name if pg else "",
        "item_group": pg.category.name if pg and pg.category_id else "",
        "item_type": pg.subcategory.name if pg and pg.subcategory_id else "",
        "gender": pg.gender.label if pg and pg.gender_id else "",
        "metal": metal_name,
        "purity": purity_name,
        "color": sku.color.label if sku and sku.color_id else "",
        "hsn_code": sku.hsn.hsn_code if sku and sku.hsn_id else "",
        "vendor_variant_name": vendor_variant_name_for_item(
            item, ProductItemLinkedVendor=ProductItemLinkedVendor
        ),
        "customer_variant_name": item.customer_variant_name or "",
        "gross_weight": str(item.gross_weight) if item.gross_weight is not None else "",
        "net_weight": str(item.net_weight) if item.net_weight is not None else "",
        **serialize_product_item_size_for_api(item),
        "image_url": images[0] if images else "",
        "images": images,
    }


def _tag_summary(tag: ProductTag) -> dict:
    item = tag.product_item
    return {
        "id": tag.id,
        "tag_value": tag.tag_value,
        "mapping_status": tag.mapping_status,
        "attributes_mapped_at": (
            tag.attributes_mapped_at.isoformat() if tag.attributes_mapped_at else None
        ),
        "gross_weight": tag.gross_weight or "",
        "net_weight": tag.net_weight or "",
        "less_weight": tag.less_weight or "",
        "remark": tag.remark or "",
        "huid": tag.huid or "",
        "price_type": tag.price_type or "",
        "printed_at": tag.printed_at.isoformat() if tag.printed_at else None,
        "grn_bag_id": tag.grn_bag_id,
        "product_item_id": item.id if item else None,
    }


def _default_values_for_tag(tag: ProductTag) -> dict[int, dict]:
    """Suggest defaults from master item when no saved value exists."""
    item = tag.product_item
    if not item:
        return {}
    summary = _item_summary(item)
    code_map = {
        "SIZE": summary.get("size_display") or "",
        "SUB_CATEGORY": summary.get("item_type") or "",
        "GENDER": summary.get("gender") or "",
        "STYLE_KARAT": summary.get("purity") or "",
        "STYLE_COLOR": summary.get("color") or "",
        "HSN_SAC_CODE": summary.get("hsn_code") or "",
        "CUSTOMER_VARIANT": summary.get("customer_variant_name") or "",
    }
    out = {}
    for defn in TagAttributeDefinition.objects.filter(is_active=True, code__in=code_map.keys()):
        text = code_map.get(defn.code) or ""
        if text:
            out[defn.id] = {"value_text": text}
    return out


_TAG_SNAPSHOT_FIELD_BY_CODE = {
    "HUID": "huid",
    "PRICE_TYPE": "price_type",
}


def _attribute_payload_text(defn, payload):
    if _value_is_empty(defn, payload):
        return ""
    if defn.data_type == "lookup":
        try:
            lookup_value_id = int(payload.get("lookup_value_id"))
        except (TypeError, ValueError):
            return ""
        lv = LookupValue.objects.filter(
            pk=lookup_value_id, lookup_id=defn.lookup_id, is_active=True
        ).first()
        return (lv.label or lv.code or "").strip() if lv else ""
    if defn.data_type == "number":
        return str(payload.get("value_number") or "").strip()
    if defn.data_type == "boolean":
        return "true" if str(payload.get("value_text")).lower() in ("1", "true", "yes") else "false"
    return str(payload.get("value_text") or "").strip()


def _sync_tag_snapshot_fields(tag, definitions, by_def_id):
    """Copy selected mapped attributes onto ProductTag label snapshot fields."""
    update_fields = []
    for defn in definitions.values():
        field = _TAG_SNAPSHOT_FIELD_BY_CODE.get(defn.code)
        if not field:
            continue
        value = _attribute_payload_text(defn, by_def_id.get(defn.id, {}))
        if getattr(tag, field) != value:
            setattr(tag, field, value)
            update_fields.append(field)
    return update_fields


def _build_mapping_payload(tag: ProductTag):
    values_qs = ProductTagAttributeValue.objects.filter(product_tag=tag).select_related(
        "attribute_definition", "lookup_value"
    )
    values_by_def = {v.attribute_definition_id: v for v in values_qs}
    defaults = _default_values_for_tag(tag)

    definitions = []
    for defn in TagAttributeDefinition.objects.filter(is_active=True).select_related("lookup"):
        value_row = values_by_def.get(defn.id)
        row = _definition_row(defn, value_row)
        if not value_row and defn.id in defaults:
            row["value_text"] = defaults[defn.id].get("value_text", "")
        definitions.append(row)

    return {
        "tag": _tag_summary(tag),
        "item": _item_summary(tag.product_item, tag),
        "definitions": definitions,
    }


def _value_is_empty(defn, payload):
    if defn.data_type == "lookup":
        return not payload.get("lookup_value_id")
    if defn.data_type == "number":
        v = payload.get("value_number")
        return v in (None, "", "null")
    if defn.data_type == "boolean":
        return payload.get("value_text") in (None, "", "false", "False", "0")
    return not str(payload.get("value_text") or "").strip()


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_ATTRIBUTE_MAPPING_VIEW")
def tag_attribute_definitions_list(request):
    """GET /master/tag-attributes/definitions/"""
    rows = []
    for defn in TagAttributeDefinition.objects.filter(is_active=True).select_related("lookup"):
        rows.append(_definition_row(defn))
    return Response({"definitions": rows})


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_ATTRIBUTE_MAPPING_VIEW")
def tag_mapping_tag_list(request):
    """
    GET /master/tag-attributes/tags/
    ?mapping_status=pending|complete&category_id=&subcategory_id=&q=
    """
    qs = (
        ProductTag.objects.filter(is_active=True)
        .select_related(
            "product_item",
            "product_item__sku",
            "product_item__sku__product_group",
        )
        .order_by("-system_created_at", "-id")
    )

    mapping_status = (request.GET.get("mapping_status") or "").strip().lower()
    if mapping_status in ("pending", "complete"):
        qs = qs.filter(mapping_status=mapping_status)

    try:
        category_id = int(request.GET.get("category_id") or 0)
    except (TypeError, ValueError):
        category_id = 0
    if category_id > 0:
        qs = qs.filter(product_item__sku__product_group__category_id=category_id)

    try:
        subcategory_id = int(request.GET.get("subcategory_id") or 0)
    except (TypeError, ValueError):
        subcategory_id = 0
    if subcategory_id > 0:
        qs = qs.filter(product_item__sku__product_group__subcategory_id=subcategory_id)

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(tag_value__icontains=q)
            | Q(sku_code__icontains=q)
            | Q(product_item__sku__product_code__icontains=q)
            | Q(product_item__store_variant_name__icontains=q)
        )

    try:
        page_size = min(int(request.GET.get("page_size", 50)), 200)
    except (TypeError, ValueError):
        page_size = 50

    rows = []
    for tag in qs[:page_size]:
        item = tag.product_item
        sku = item.sku if item else None
        pg = sku.product_group if sku and sku.product_group_id else None
        images = _item_images(item, tag)
        rows.append(
            {
                "id": tag.id,
                "tag_value": tag.tag_value,
                "mapping_status": tag.mapping_status,
                "product_code": sku.product_code if sku else "",
                "sku_code": tag.sku_code or (sku.sku_code if sku else ""),
                "store_variant_name": item.store_variant_name if item else "",
                "item_group": pg.category.name if pg and pg.category_id else "",
                "item_type": pg.subcategory.name if pg and pg.subcategory_id else "",
                "created_at": tag.system_created_at.isoformat() if tag.system_created_at else "",
                "image_url": images[0] if images else "",
            }
        )

    return Response({"results": rows, "total": qs.count()})


@api_view(["GET"])
@admin_auth("CRM_MASTERS_GRN_ATTRIBUTE_MAPPING_VIEW")
def tag_mapping_detail(request, tag_id):
    """GET /master/tag-attributes/tags/<tag_id>/mapping/"""
    tag = (
        ProductTag.objects.filter(pk=tag_id, is_active=True)
        .select_related(
            "product_item",
            "product_item__sku",
            "product_item__sku__product_group",
            "product_item__sku__product_group__category",
            "product_item__sku__product_group__subcategory",
            "product_item__sku__product_group__gender",
            "product_item__sku__color",
            "product_item__sku__hsn",
        )
        .first()
    )
    if not tag:
        return Response({"detail": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(_build_mapping_payload(tag))


@api_view(["PUT", "PATCH"])
@admin_auth("CRM_MASTERS_GRN_ATTRIBUTE_MAPPING_UPDATE")
@transaction.atomic
def tag_mapping_save(request, tag_id):
    """PUT /master/tag-attributes/tags/<tag_id>/mapping/"""
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    tag = ProductTag.objects.filter(pk=tag_id, is_active=True).select_related("product_item").first()
    if not tag:
        return Response({"detail": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)

    data = request.data or {}
    values_in = data.get("values")
    if not isinstance(values_in, list):
        return Response(
            {"errors": {"values": ["Expected a list of attribute values."]}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    definitions = {
        d.id: d
        for d in TagAttributeDefinition.objects.filter(is_active=True).select_related("lookup")
    }
    by_def_id = {}
    for raw in values_in:
        if not isinstance(raw, dict):
            continue
        try:
            def_id = int(raw.get("definition_id"))
        except (TypeError, ValueError):
            continue
        if def_id in definitions:
            by_def_id[def_id] = raw

    errors = {}
    for def_id, defn in definitions.items():
        if not defn.required:
            continue
        if _value_is_empty(defn, by_def_id.get(def_id, {})):
            errors[defn.code] = [f"{defn.label} is required."]

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    for def_id, defn in definitions.items():
        payload = by_def_id.get(def_id, {})
        if _value_is_empty(defn, payload):
            ProductTagAttributeValue.objects.filter(
                product_tag=tag, attribute_definition_id=def_id
            ).delete()
            continue

        value_text = ""
        value_number = None
        lookup_value_id = None

        if defn.data_type == "lookup":
            try:
                lookup_value_id = int(payload.get("lookup_value_id"))
            except (TypeError, ValueError):
                lookup_value_id = None
            if not lookup_value_id or not LookupValue.objects.filter(
                pk=lookup_value_id, lookup_id=defn.lookup_id, is_active=True
            ).exists():
                errors.setdefault(defn.code, []).append("Invalid lookup value.")
                continue
        elif defn.data_type == "number":
            try:
                value_number = Decimal(str(payload.get("value_number")))
            except (InvalidOperation, TypeError, ValueError):
                errors.setdefault(defn.code, []).append("Invalid number.")
                continue
        elif defn.data_type == "boolean":
            value_text = "true" if str(payload.get("value_text")).lower() in ("1", "true", "yes") else "false"
        else:
            value_text = str(payload.get("value_text") or "").strip()

        ProductTagAttributeValue.objects.update_or_create(
            product_tag=tag,
            attribute_definition_id=def_id,
            defaults={
                "value_text": value_text,
                "value_number": value_number,
                "lookup_value_id": lookup_value_id,
                "updated_by": admin,
                "created_by": admin,
            },
        )

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    required_ids = [d.id for d in definitions.values() if d.required]
    all_required_filled = all(
        not _value_is_empty(
            definitions[did],
            by_def_id.get(did, {}),
        )
        for did in required_ids
    )
    tag.mapping_status = "complete" if all_required_filled else "pending"
    tag.attributes_mapped_at = timezone.now() if all_required_filled else None
    tag.updated_by = admin
    snapshot_fields = _sync_tag_snapshot_fields(tag, definitions, by_def_id)
    tag.save(
        update_fields=[
            "mapping_status",
            "attributes_mapped_at",
            "updated_by",
            "system_updated_at",
            *snapshot_fields,
        ]
    )

    return Response(_build_mapping_payload(tag))
