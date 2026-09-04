"""ProductItem ↔ Vendor M2M mapping (works for parent and child product items)."""

from django.utils import timezone
from django.utils.dateparse import parse_datetime


def _parse_optional_days(value):
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > 32767:
        return None
    return n


def _parse_optional_datetime(value):
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        dt = parse_datetime(s)
        if dt is None and len(s) == 10:
            dt = parse_datetime(f"{s}T23:59:59")
        if dt is None:
            return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _format_validity_for_api(dt):
    if not dt:
        return None
    return dt.isoformat()


def _clean_vendor_entry(row):
    if not isinstance(row, dict):
        return None
    try:
        vendor_id = int(row.get("vendor_id"))
    except (TypeError, ValueError):
        return None
    if vendor_id <= 0:
        return None
    return {
        "id": str(row.get("id") or "").strip() or None,
        "vendor_id": vendor_id,
        "vendor_name": str(row.get("vendor_name") or "").strip(),
        "vendor_code": str(row.get("vendor_code") or "").strip(),
        "vendor_variant_name": str(row.get("vendor_variant_name") or "").strip()[:255],
        "delivery_days": _parse_optional_days(row.get("delivery_days")),
        "validity": _parse_optional_datetime(row.get("validity")),
        "vendor_gst_number": str(row.get("vendor_gst_number") or "").strip(),
        "vendor_contact_person": str(row.get("vendor_contact_person") or "").strip(),
        "vendor_phone": str(row.get("vendor_phone") or "").strip(),
        "vendor_email": str(row.get("vendor_email") or "").strip(),
        "vendor_address1": str(row.get("vendor_address1") or "").strip(),
        "vendor_address2": str(row.get("vendor_address2") or "").strip(),
        "vendor_city": str(row.get("vendor_city") or "").strip(),
        "vendor_state": str(row.get("vendor_state") or "").strip(),
        "vendor_postal_code": str(row.get("vendor_postal_code") or "").strip(),
        "vendor_country": str(row.get("vendor_country") or "").strip() or "India",
    }


def normalize_vendor_entries(step_vendor):
    if not isinstance(step_vendor, dict):
        return []
    rows = []
    seen_ids = set()
    for row in step_vendor.get("vendor_entries") or []:
        cleaned = _clean_vendor_entry(row)
        if not cleaned or cleaned["vendor_id"] in seen_ids:
            continue
        seen_ids.add(cleaned["vendor_id"])
        rows.append(cleaned)
    if rows:
        return rows
    legacy = _clean_vendor_entry(step_vendor)
    return [legacy] if legacy else []


def primary_vendor_id(step_vendor):
    entries = normalize_vendor_entries(step_vendor)
    return entries[0]["vendor_id"] if entries else None


def primary_vendor_id_for_item(product_item, *, ProductItemLinkedVendor):
    """First linked vendor id for a product item (sort_order, then id)."""
    if not product_item or not getattr(product_item, "id", None):
        return None
    return (
        ProductItemLinkedVendor.objects.filter(product_item_id=product_item.id)
        .order_by("sort_order", "id")
        .values_list("vendor_id", flat=True)
        .first()
    )


def primary_vendor_name_for_item(product_item, *, ProductItemLinkedVendor):
    """Display name of the first linked vendor, if any."""
    if not product_item or not getattr(product_item, "id", None):
        return ""
    link = (
        ProductItemLinkedVendor.objects.filter(product_item_id=product_item.id)
        .select_related("vendor")
        .order_by("sort_order", "id")
        .first()
    )
    if not link or not link.vendor_id:
        return ""
    return (link.vendor.vendor_name or "").strip()


def primary_vendor_variant_name(step_vendor, step1_flat=None):
    """Resolve vendor variant label from vendor step (legacy step1 fallback for old drafts)."""
    entries = normalize_vendor_entries(step_vendor)
    for row in entries:
        name = (row.get("vendor_variant_name") or "").strip()
        if name:
            return name[:255]
    if isinstance(step1_flat, dict):
        legacy = str(step1_flat.get("vendor_variant_name") or "").strip()
        if legacy:
            return legacy[:255]
    return ""


def vendor_variant_name_for_item(product_item, vendor_id=None, *, ProductItemLinkedVendor):
    """Read vendor variant name from linked-vendor rows (per-supplier naming)."""
    if not product_item or not getattr(product_item, "id", None):
        return ""
    qs = ProductItemLinkedVendor.objects.filter(product_item_id=product_item.id).order_by(
        "sort_order", "id"
    )
    if vendor_id not in (None, ""):
        try:
            vid = int(vendor_id)
        except (TypeError, ValueError):
            vid = None
        if vid:
            link = qs.filter(vendor_id=vid).first()
            if link:
                name = (link.vendor_variant_name or "").strip()
                if name:
                    return name[:255]
    for link in qs:
        name = (link.vendor_variant_name or "").strip()
        if name:
            return name[:255]
    return ""


def filter_items_by_vendor_variant_name(qs, name):
    """Filter ProductItem queryset by linked vendor variant name."""
    needle = (name or "").strip()
    if not needle:
        return qs
    return qs.filter(linked_vendors__vendor_variant_name__icontains=needle).distinct()


def resolve_vendor_id_by_name(vendor_name, *, Vendor):
    """Resolve Vendor.id from batch/vendor display name (exact, then contains)."""
    needle = (vendor_name or "").strip()
    if not needle:
        return None
    vid = (
        Vendor.objects.filter(vendor_name__iexact=needle, is_active=True)
        .values_list("id", flat=True)
        .first()
    )
    if vid:
        return vid
    return (
        Vendor.objects.filter(vendor_name__icontains=needle, is_active=True)
        .values_list("id", flat=True)
        .first()
    )


def filter_items_by_vendor_name(qs, vendor_name, *, Vendor):
    """Filter ProductItem queryset to SKUs linked to the given vendor."""
    vid = resolve_vendor_id_by_name(vendor_name, Vendor=Vendor)
    if not vid:
        return qs.none()
    return qs.filter(linked_vendors__vendor_id=vid).distinct()


def resolve_lot_linked_vendor_terms(
    *,
    vendor_name,
    pattern_code="",
    product_code="",
    category_id=None,
    subcategory_id=None,
    product_item_id=None,
    ProductItem,
    ProductItemLinkedVendor,
    Vendor,
):
    """
    Match batch vendor + lot/catalog scope to ProductItemLinkedVendor.
    Returns display-only vendor terms (not persisted on GrnLot).
    """
    empty = {
        "matched": False,
        "vendor_name": (vendor_name or "").strip(),
        "vendor_variant_name": "",
        "delivery_days": None,
        "validity": None,
        "validity_expired": False,
    }
    vid = resolve_vendor_id_by_name(vendor_name, Vendor=Vendor)
    if not vid:
        return empty

    vendor = Vendor.objects.filter(pk=vid).first()
    vendor_display = (vendor.vendor_name or "").strip() if vendor else empty["vendor_name"]

    link = None
    if product_item_id not in (None, ""):
        try:
            pi_id = int(product_item_id)
        except (TypeError, ValueError):
            pi_id = None
        if pi_id:
            link = ProductItemLinkedVendor.objects.filter(
                product_item_id=pi_id, vendor_id=vid
            ).first()

    if link is None:
        qs = ProductItem.objects.filter(
            sku__isnull=False, linked_vendors__vendor_id=vid
        )
        if category_id not in (None, ""):
            try:
                qs = qs.filter(sku__product_group__category_id=int(category_id))
            except (TypeError, ValueError):
                pass
        if subcategory_id not in (None, ""):
            try:
                qs = qs.filter(sku__product_group__subcategory_id=int(subcategory_id))
            except (TypeError, ValueError):
                pass
        pc = (pattern_code or "").strip().upper().replace(" ", "")
        if pc:
            qs = qs.filter(sku__pattern_code__iexact=pc)
        prod = (product_code or "").strip()
        if prod:
            qs = qs.filter(sku__product_code__iexact=prod)
        item = qs.distinct().order_by("-system_created_at", "-id").first()
        if item:
            link = ProductItemLinkedVendor.objects.filter(
                product_item_id=item.id, vendor_id=vid
            ).first()

    if not link:
        return {**empty, "vendor_name": vendor_display}

    validity = link.validity
    expired = False
    if validity:
        now = timezone.now()
        if timezone.is_naive(validity):
            validity = timezone.make_aware(validity, timezone.get_current_timezone())
        expired = validity < now

    return {
        "matched": True,
        "vendor_name": vendor_display,
        "vendor_variant_name": (link.vendor_variant_name or "").strip(),
        "delivery_days": link.delivery_days,
        "validity": _format_validity_for_api(validity),
        "validity_expired": expired,
    }


def _vendor_address(vendor_id, *, VendorAddress):
    if not vendor_id:
        return None
    return VendorAddress.objects.filter(vendor_id=vendor_id).order_by("id").first()


def _vendor_entry_dict(
    vendor,
    addr=None,
    *,
    entry_id=None,
    vendor_variant_name="",
    delivery_days=None,
    validity=None,
):
    vid = vendor.id
    return {
        "id": entry_id or f"vendor-{vid}",
        "vendor_id": vid,
        "vendor_name": vendor.vendor_name or "",
        "vendor_code": vendor.vendor_code or "",
        "vendor_variant_name": (vendor_variant_name or "").strip()[:255],
        "delivery_days": delivery_days,
        "validity": _format_validity_for_api(validity),
        "vendor_gst_number": vendor.gst_number or "",
        "vendor_contact_person": vendor.contact_person or "",
        "vendor_phone": vendor.phone or "",
        "vendor_email": vendor.email or "",
        "vendor_address1": (addr.address_line1 if addr else "") or "",
        "vendor_address2": (addr.address_line2 if addr else "") or "",
        "vendor_city": (addr.city if addr else "") or "",
        "vendor_state": (addr.state if addr else "") or "",
        "vendor_postal_code": (addr.pincode if addr else "") or "",
        "vendor_country": (addr.country if addr else "") or "India",
    }


def linked_vendors_for_item(product_item, *, ProductItemLinkedVendor, VendorAddress):
    """Load vendor mappings for any product item (parent or child)."""
    if not product_item or not getattr(product_item, "id", None):
        return []
    links = (
        ProductItemLinkedVendor.objects.filter(product_item_id=product_item.id)
        .select_related("vendor")
        .order_by("sort_order", "id")
    )
    entries = []
    for link in links:
        v = link.vendor
        if not v:
            continue
        addr = _vendor_address(v.id, VendorAddress=VendorAddress)
        entries.append(
            _vendor_entry_dict(
                v,
                addr,
                entry_id=f"vendor-{v.id}",
                vendor_variant_name=getattr(link, "vendor_variant_name", "") or "",
                delivery_days=getattr(link, "delivery_days", None),
                validity=getattr(link, "validity", None),
            )
        )
    return entries


def save_linked_vendors_on_item(
    product_item,
    step_vendor,
    *,
    admin_user,
    ProductItemLinkedVendor,
):
    """Replace all vendor M2M rows for this product item."""
    entries = normalize_vendor_entries(step_vendor)
    ProductItemLinkedVendor.objects.filter(product_item=product_item).delete()
    for idx, row in enumerate(entries):
        ProductItemLinkedVendor.objects.create(
            product_item=product_item,
            vendor_id=row["vendor_id"],
            sort_order=idx,
            vendor_variant_name=row.get("vendor_variant_name") or "",
            delivery_days=row.get("delivery_days"),
            validity=row.get("validity"),
            created_by=admin_user,
            updated_by=admin_user,
        )
    return entries


def vendor_step_payload_for_item(product_item, *, ProductItemLinkedVendor, VendorAddress):
    entries = linked_vendors_for_item(
        product_item,
        ProductItemLinkedVendor=ProductItemLinkedVendor,
        VendorAddress=VendorAddress,
    )
    primary_id = entries[0]["vendor_id"] if entries else None
    return {
        "vendor_id": primary_id,
        "vendor_entries": entries,
    }
