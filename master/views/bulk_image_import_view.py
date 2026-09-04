"""
GRN Bulk Product Image Import — match filenames to product codes and link gallery images.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from master.views.barcode_view import get_admin_user_from_request
from shared.models import ProductImage, ProductItem, ProductSKU, ProductTag, ProductTagPhoto

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
# Role words only (hyphen or underscore): AJ-GR-000123-front, CODE_back
_ROLE_SUFFIX_RE = re.compile(
    r"[_-](front|back|side|model|video|main|primary|left|right|top|bottom)$",
    re.IGNORECASE,
)
# Sequence suffix: underscore + digits only — AJ-GR-000123_1, JH-1001_2
# Do NOT strip hyphen+digits (that would turn JH-1001 → JH / TST-01 → TST and mix products).
_SEQ_UNDERSCORE_RE = re.compile(r"_\d+$", re.IGNORECASE)


def _strip_image_suffix(stem: str) -> str:
    """Remove trailing photo role/sequence suffix; keep product-code tails like -1001."""
    s = (stem or "").strip()
    if not s:
        return ""
    prev = None
    while prev != s:
        prev = s
        s2 = _ROLE_SUFFIX_RE.sub("", s).strip()
        if s2 != s:
            s = s2
            continue
        s2 = _SEQ_UNDERSCORE_RE.sub("", s).strip()
        if s2 != s:
            s = s2
            continue
        break
    return s


def _stem_keys(filename: str) -> list[str]:
    """
    Derive lookup keys from a filename or relative path.

    Supports:
      JH-1001.jpg, JH-1001_1.jpg, JH-1001-front.jpg, JH-1001-1.jpg
      Folder import: JH-1001/photo1.jpg  → also tries folder name JH-1001
    Keys are returned longest-first so exact codes win over short prefixes.
    """
    raw = (filename or "").replace("\\", "/").strip()
    basename = os.path.basename(raw)
    stem = os.path.splitext(basename)[0].strip()
    keys: list[str] = []

    if stem:
        keys.append(stem)
        base = _strip_image_suffix(stem)
        if base and base.upper() != stem.upper():
            keys.append(base)

    # Parent folder name (webkitdirectory relative path), e.g. JH-1001/img.jpg
    parts = [p for p in raw.split("/") if p and p != basename]
    if parts:
        folder = parts[-1].strip()
        if folder and not folder.startswith("."):
            keys.append(folder)
            folder_base = _strip_image_suffix(folder)
            if folder_base and folder_base.upper() != folder.upper():
                keys.append(folder_base)

    # De-dupe case-insensitively; longest first (most specific product code wins)
    seen: set[str] = set()
    uniq: list[str] = []
    for k in keys:
        u = k.upper()
        if u and u not in seen:
            seen.add(u)
            uniq.append(k)
    uniq.sort(key=lambda k: len(k), reverse=True)
    return uniq


def _is_image_filename(name: str) -> bool:
    return os.path.splitext(os.path.basename(name or ""))[1].lower() in ALLOWED_IMAGE_EXT


def _resolve_item_for_keys(keys: list[str]) -> tuple[ProductItem | None, ProductTag | None, str, str]:
    """
    Match keys one-at-a-time (longest first).

    Barcode / tag_value is tried first so CL-1001, JH-1001-1009, etc. attach to the
    specific tagged piece — not the shared ProductItem used by every tag on that SKU.
    """
    if not keys:
        return None, None, "", ""

    for k in keys:
        tag = (
            ProductTag.objects.filter(tag_value__iexact=k, is_active=True)
            .select_related("product_item", "product_item__sku")
            .first()
        )
        if tag and tag.product_item_id:
            return tag.product_item, tag, (tag.tag_value or "").strip(), "tag_value"

    for k in keys:
        sku = (
            ProductSKU.objects.filter(product_code__iexact=k)
            .exclude(product_code__isnull=True)
            .exclude(product_code="")
            .select_related("product_group")
            .first()
        )
        if sku:
            item = _preferred_item_for_sku(sku.id)
            if item:
                return item, None, (sku.product_code or "").strip(), "product_code"

    for k in keys:
        sku = (
            ProductSKU.objects.filter(pattern_code__iexact=k)
            .exclude(pattern_code__isnull=True)
            .exclude(pattern_code="")
            .select_related("product_group")
            .first()
        )
        if sku:
            item = _preferred_item_for_sku(sku.id)
            if item:
                return item, None, (sku.pattern_code or "").strip(), "pattern_code"

    return None, None, "", ""


def _preferred_item_for_sku(sku_id: int) -> ProductItem | None:
    qs = ProductItem.objects.filter(sku_id=sku_id).order_by(
        "-is_parent_product", "-id"
    )
    return qs.first()


def _match_filename(filename: str) -> dict:
    keys = _stem_keys(filename)
    item, tag, matched_key, matched_via = _resolve_item_for_keys(keys)
    existing = 0
    if tag:
        existing = ProductTagPhoto.objects.filter(product_tag_id=tag.id).count()
    elif item:
        existing = ProductImage.objects.filter(product_id=item.id).count()
    sku = item.sku if item and item.sku_id else None
    return {
        "filename": os.path.basename(filename.replace("\\", "/")),
        "keys": keys,
        "matched": item is not None,
        "matched_key": matched_key,
        "matched_via": matched_via,
        "product_item_id": item.id if item else None,
        "product_tag_id": tag.id if tag else None,
        "tag_value": (tag.tag_value or "") if tag else "",
        "product_code": (sku.product_code or "") if sku else "",
        "pattern_code": (sku.pattern_code or "") if sku else "",
        "sku_code": (sku.sku_code or "") if sku else "",
        "store_variant_name": (item.store_variant_name or "") if item else "",
        "existing_image_count": existing,
    }


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_BULK_IMAGE_IMPORT_VIEW")
def bulk_image_match(request):
    """
    POST /master/grn/bulk-images/match/
    Body: { "filenames": ["AJ-GR-000123.jpg", ...] }
    Preview matching without uploading.
    """
    filenames = request.data.get("filenames") or []
    if not isinstance(filenames, list):
        return Response({"detail": "filenames must be a list."}, status=status.HTTP_400_BAD_REQUEST)

    results = []
    matched = []
    unmatched = []
    for raw in filenames[:2000]:
        name = str(raw or "").strip()
        if not name:
            continue
        if not _is_image_filename(name):
            row = {
                "filename": os.path.basename(name),
                "keys": [],
                "matched": False,
                "matched_key": "",
                "matched_via": "",
                "product_item_id": None,
                "product_code": "",
                "pattern_code": "",
                "sku_code": "",
                "store_variant_name": "",
                "existing_image_count": 0,
                "error": "Unsupported file type",
            }
            results.append(row)
            unmatched.append(row)
            continue
        row = _match_filename(name)
        results.append(row)
        if row["matched"]:
            matched.append(row)
        else:
            unmatched.append(row)

    # Products that matched at least once — also surface products with zero existing images
    products_without_images = [
        {
            "product_item_id": r["product_item_id"],
            "product_code": r["product_code"],
            "sku_code": r["sku_code"],
            "store_variant_name": r["store_variant_name"],
        }
        for r in matched
        if r["existing_image_count"] == 0
    ]
    # de-dupe by product_item_id
    seen_pid: set[int] = set()
    products_needing_images = []
    for p in products_without_images:
        pid = p["product_item_id"]
        if pid and pid not in seen_pid:
            seen_pid.add(pid)
            products_needing_images.append(p)

    # Duplicate filenames in the batch (same basename)
    name_counts: dict[str, int] = {}
    for r in results:
        fn = (r["filename"] or "").lower()
        name_counts[fn] = name_counts.get(fn, 0) + 1
    duplicates = [r for r in results if name_counts.get((r["filename"] or "").lower(), 0) > 1]

    # Per-tag / per-product image counts in this batch
    by_target: dict[str, dict] = {}
    for r in matched:
        pid = r.get("product_item_id")
        if not pid:
            continue
        tid = r.get("product_tag_id")
        key = f"tag:{tid}" if tid else f"item:{pid}"
        bucket = by_target.get(key)
        if not bucket:
            bucket = {
                "product_item_id": pid,
                "product_tag_id": tid,
                "tag_value": r.get("tag_value") or "",
                "product_code": r.get("product_code") or "",
                "pattern_code": r.get("pattern_code") or "",
                "sku_code": r.get("sku_code") or "",
                "store_variant_name": r.get("store_variant_name") or "",
                "image_count": 0,
                "filenames": [],
            }
            by_target[key] = bucket
        bucket["image_count"] += 1
        bucket["filenames"].append(r.get("filename") or "")

    products_in_batch = sorted(
        by_target.values(),
        key=lambda p: (
            (p.get("tag_value") or p.get("product_code") or p.get("sku_code") or "").upper()
        ),
    )

    return Response(
        {
            "total": len(results),
            "matched_count": len(matched),
            "unmatched_count": len(unmatched),
            "duplicate_filename_count": len(duplicates),
            "results": results,
            "matched": matched,
            "unmatched": unmatched,
            "duplicates": duplicates,
            "products_without_existing_images": products_needing_images,
            "products_in_batch": products_in_batch,
        }
    )


def _import_group_key(row: dict) -> str | None:
    pid = row.get("product_item_id")
    if not pid:
        return None
    tid = row.get("product_tag_id")
    return f"tag:{tid}" if tid else f"item:{pid}"


def _upload_and_record(
    *,
    f,
    row: dict,
    admin,
    upload_file_to_s3,
    build_public_object_url,
    tag: ProductTag | None,
    item: ProductItem,
    sort_order: int,
) -> dict:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    safe_name = os.path.basename(getattr(f, "name", "image.jpg")).replace(" ", "_")
    if tag:
        object_name = f"Taranya/tags/tag_{tag.id}/bulk_{timestamp}_{safe_name}"
    else:
        object_name = f"Taranya/products/item_{item.id}/bulk_{timestamp}_{safe_name}"
    ok = upload_file_to_s3(f, object_name)
    if not ok:
        raise RuntimeError("S3 upload failed")
    url = build_public_object_url(object_name)
    if tag:
        ProductTagPhoto.objects.create(
            product_tag=tag,
            image_url=url,
            sort_order=sort_order,
            created_by=admin,
            updated_by=admin,
        )
    else:
        ProductImage.objects.create(
            product=item,
            image_url=url,
            is_primary=sort_order == 0,
            created_by=admin,
            updated_by=admin,
        )
    return {
        "filename": row["filename"],
        "product_item_id": item.id,
        "product_tag_id": tag.id if tag else None,
        "tag_value": row.get("tag_value") or "",
        "product_code": row.get("product_code") or "",
        "sku_code": row.get("sku_code") or "",
        "image_url": url,
        "is_primary": sort_order == 0 and tag is None,
        "matched_via": row.get("matched_via") or "",
    }


@api_view(["POST"])
@admin_auth("CRM_MASTERS_GRN_BULK_IMAGE_IMPORT_CREATE")
def bulk_image_import(request):
    """
    POST /master/grn/bulk-images/import/
    Multipart: images (files), conflict_mode=replace|skip|add (default add)
    Barcode/tag filenames → ProductTagPhoto (per piece).
    Product/pattern code filenames → ProductImage (shared product gallery).
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    conflict_mode = (request.data.get("conflict_mode") or "add").strip().lower()
    if conflict_mode not in ("replace", "skip", "add"):
        conflict_mode = "add"

    files = request.FILES.getlist("images") or request.FILES.getlist("image")
    if not files:
        return Response({"detail": "No images provided."}, status=status.HTTP_400_BAD_REQUEST)
    if len(files) > 500:
        return Response(
            {"detail": "Maximum 500 images per import batch."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Optional parallel relative paths (folder import): same order as `images`
    raw_paths = request.data.getlist("paths") if hasattr(request.data, "getlist") else None
    if raw_paths is None:
        single = request.data.get("paths")
        raw_paths = single if isinstance(single, list) else []
    paths = [str(p or "").strip() for p in (raw_paths or [])]

    from shared.services.s3_service import upload_file_to_s3, build_public_object_url

    imported: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    unmatched: list[dict] = []

    # Group by tag (per-piece) or product item (shared gallery)
    groups: dict[str, list] = {}

    for idx, f in enumerate(files):
        name = (paths[idx] if idx < len(paths) and paths[idx] else None) or getattr(
            f, "name", ""
        ) or "image.jpg"
        if not _is_image_filename(name):
            unmatched.append(
                {
                    "filename": os.path.basename(name.replace("\\", "/")),
                    "reason": "Unsupported file type",
                }
            )
            continue
        row = _match_filename(name)
        if not row["matched"] or not row["product_item_id"]:
            unmatched.append(
                {
                    "filename": row["filename"],
                    "reason": "No matching product code / pattern code / barcode",
                    "keys": row.get("keys") or [],
                }
            )
            continue
        gkey = _import_group_key(row)
        if not gkey:
            continue
        groups.setdefault(gkey, []).append((f, row))

    with transaction.atomic():
        for gkey, pairs in groups.items():
            row0 = pairs[0][1]
            item = ProductItem.objects.filter(pk=row0["product_item_id"]).first()
            if not item:
                for f, row in pairs:
                    failed.append({"filename": row["filename"], "reason": "Product item not found"})
                continue

            tag = None
            if gkey.startswith("tag:"):
                tag_id = int(gkey.split(":", 1)[1])
                tag = ProductTag.objects.filter(pk=tag_id, is_active=True).first()
                if not tag:
                    for f, row in pairs:
                        failed.append({"filename": row["filename"], "reason": "Barcode tag not found"})
                    continue
                existing_qs = ProductTagPhoto.objects.filter(product_tag=tag)
            else:
                existing_qs = ProductImage.objects.filter(product_id=item.id)
            existing_count = existing_qs.count()

            if conflict_mode == "skip" and existing_count > 0:
                for f, row in pairs:
                    skipped.append(
                        {
                            "filename": row["filename"],
                            "product_item_id": item.id,
                            "product_tag_id": tag.id if tag else None,
                            "tag_value": row.get("tag_value") or "",
                            "product_code": row.get("product_code") or "",
                            "reason": "Already has images (skip mode)",
                        }
                    )
                continue

            if conflict_mode == "replace" and existing_count > 0:
                existing_qs.delete()
                existing_count = 0

            base_sort = existing_count
            for idx, (f, row) in enumerate(pairs):
                try:
                    rec = _upload_and_record(
                        f=f,
                        row=row,
                        admin=admin,
                        upload_file_to_s3=upload_file_to_s3,
                        build_public_object_url=build_public_object_url,
                        tag=tag,
                        item=item,
                        sort_order=base_sort + idx,
                    )
                    imported.append(rec)
                except Exception as exc:  # noqa: BLE001
                    failed.append(
                        {
                            "filename": row["filename"],
                            "product_item_id": item.id,
                            "product_tag_id": tag.id if tag else None,
                            "reason": str(exc),
                        }
                    )

    summary = {
        "conflict_mode": conflict_mode,
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "unmatched_count": len(unmatched),
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "unmatched_images": unmatched,
        "products_in_batch": [
            {
                "product_item_id": pairs[0][1].get("product_item_id"),
                "product_tag_id": pairs[0][1].get("product_tag_id"),
                "tag_value": pairs[0][1].get("tag_value") or "",
                "product_code": pairs[0][1].get("product_code") or "",
                "sku_code": pairs[0][1].get("sku_code") or "",
                "image_count": len(pairs),
                "filenames": [row.get("filename") or "" for _, row in pairs],
            }
            for pairs in groups.values()
        ],
        "audit": {
            "user_id": admin.id,
            "user_name": getattr(admin, "full_name", None)
            or getattr(admin, "username", None)
            or getattr(admin, "email", "")
            or str(admin.id),
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "file_count": len(files),
        },
    }
    return Response(summary, status=status.HTTP_200_OK)
