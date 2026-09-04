"""
Structured size on ProductItem only (not on ProductSKU).

NUMBER — size_number (others NULL)
MM     — size_mm only
HW     — height_mm + width_mm
All size fields are optional on a stock line unless a flow explicitly requires them
(e.g. make-bag multi-size receive).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Q

SIZE_NUMBER = "NUMBER"
SIZE_MM = "MM"
SIZE_HW = "HW"


def infer_sku_size_type_from_step4(step4: dict | None) -> str:
    """
    Wizard step4 mode (number vs dimension) for API responses — not stored on SKU.
    """
    if not step4:
        return SIZE_NUMBER
    st = str(step4.get("size_type") or "number").lower()
    raw_sizes = [x for x in (step4.get("sizes") or []) if isinstance(x, dict)]
    if st == "number":
        return SIZE_NUMBER
    for row in raw_sizes:
        hm = row.get("height_mm") if row.get("height_mm") not in (None, "") else row.get("height")
        wm = row.get("width_mm") if row.get("width_mm") not in (None, "") else row.get("width")
        if hm not in (None, "") and wm not in (None, ""):
            return SIZE_HW
    return SIZE_MM


def infer_size_type_from_wizard_row(raw: dict | None) -> str | None:
    """Infer NUMBER / MM / HW from one wizard or API size row (or None if empty)."""
    raw = raw if isinstance(raw, dict) else {}
    row_st = str(raw.get("size_type") or "").lower()
    if row_st == "number":
        return SIZE_NUMBER
    if row_st == "dimension":
        hm = raw.get("height_mm") if raw.get("height_mm") not in (None, "") else raw.get("height")
        wm = raw.get("width_mm") if raw.get("width_mm") not in (None, "") else raw.get("width")
        if hm not in (None, "") and wm not in (None, ""):
            return SIZE_HW
        if raw.get("size_mm") not in (None, ""):
            return SIZE_MM
        return SIZE_MM
    n = _int_safe(raw.get("size_number") if raw.get("size_number") not in (None, "") else raw.get("size"))
    if n is not None:
        return SIZE_NUMBER
    sm = _dec(raw.get("size_mm"))
    if sm is not None:
        return SIZE_MM
    hm = _dec(raw.get("height_mm") if raw.get("height_mm") not in (None, "") else raw.get("height"))
    wm = _dec(raw.get("width_mm") if raw.get("width_mm") not in (None, "") else raw.get("width"))
    if hm is not None and wm is not None:
        return SIZE_HW
    return None


def infer_item_size_type(
    *,
    size_number: int | None = None,
    size_mm: Decimal | None = None,
    height_mm: Decimal | None = None,
    width_mm: Decimal | None = None,
) -> str | None:
    """Infer display/API size_type from ProductItem size columns."""
    if size_number is not None:
        return SIZE_NUMBER
    if size_mm is not None:
        return SIZE_MM
    if height_mm is not None or width_mm is not None:
        return SIZE_HW if height_mm is not None and width_mm is not None else None
    return None


def _dec(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int_safe(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def size_fields_from_wizard_row(raw_size: dict | None) -> dict[str, Any]:
    """Build ProductItem size_* kwargs from one wizard row; size is optional."""
    raw = raw_size if isinstance(raw_size, dict) else {}
    st = infer_size_type_from_wizard_row(raw)
    empty = {"size_number": None, "size_mm": None, "height_mm": None, "width_mm": None}
    if st is None:
        return empty
    if st == SIZE_NUMBER:
        return {**empty, "size_number": _int_safe(raw.get("size_number"))}
    if st == SIZE_MM:
        return {**empty, "size_mm": _dec(raw.get("size_mm"))}
    hm = _dec(raw.get("height_mm") if raw.get("height_mm") not in (None, "") else raw.get("height"))
    wm = _dec(raw.get("width_mm") if raw.get("width_mm") not in (None, "") else raw.get("width"))
    return {**empty, "height_mm": hm, "width_mm": wm}


def size_key_tuple(item) -> tuple:
    """Dedupe / merge key for items under same SKU."""
    return (item.size_number, item.size_mm, item.height_mm, item.width_mm)


def normalize_product_item_size_fields(item) -> None:
    """Clear size columns that do not apply to the populated size shape."""
    st = infer_item_size_type(
        size_number=item.size_number,
        size_mm=item.size_mm,
        height_mm=item.height_mm,
        width_mm=item.width_mm,
    )
    if st == SIZE_NUMBER:
        item.size_mm = None
        item.height_mm = None
        item.width_mm = None
    elif st == SIZE_MM:
        item.size_number = None
        item.height_mm = None
        item.width_mm = None
    elif st == SIZE_HW:
        item.size_number = None
        item.size_mm = None


def validate_product_item_size_fields(
    *,
    size_number: int | None,
    size_mm: Decimal | None,
    height_mm: Decimal | None,
    width_mm: Decimal | None,
) -> None:
    """Size is optional; only one shape allowed; no partial H×W."""
    has_n = size_number is not None
    has_mm = size_mm is not None
    has_h = height_mm is not None
    has_w = width_mm is not None
    if has_h != has_w:
        raise ValidationError("height_mm and width_mm must both be set or both empty.")
    has_hw = has_h and has_w
    shapes = sum([has_n, has_mm, has_hw])
    if shapes > 1:
        raise ValidationError(
            "Only one size form may be set per item: ring number, diameter (mm), or height×width (mm)."
        )


def format_product_item_size_display(item) -> str:
    """Human-readable size for tables, labels, and bag slug suffix."""
    if item.size_number is not None:
        return f"Size {item.size_number}"
    if item.size_mm is not None:
        s = format(item.size_mm, "f").rstrip("0").rstrip(".")
        return f"{s} mm" if s else "—"
    if item.height_mm is not None and item.width_mm is not None:
        h = format(item.height_mm, "f").rstrip("0").rstrip(".")
        w = format(item.width_mm, "f").rstrip("0").rstrip(".")
        return f"{h} × {w} mm"
    return "—"


def format_size_kwargs_display(size_type: str | None, kw: dict[str, Any]) -> str:
    """Display string from raw size_* kwargs (size_type hint optional)."""
    item = SimpleNamespace(
        size_number=kw.get("size_number"),
        size_mm=kw.get("size_mm"),
        height_mm=kw.get("height_mm"),
        width_mm=kw.get("width_mm"),
    )
    return format_product_item_size_display(item)


def slug_from_size_display(display: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", (display or "X").strip())
    return (s or "X")[:48]


def serialize_product_item_size_for_api(item) -> dict[str, Any]:
    """Response fragment: size_type (inferred), size_display, and populated size keys."""
    st = infer_item_size_type(
        size_number=item.size_number,
        size_mm=item.size_mm,
        height_mm=item.height_mm,
        width_mm=item.width_mm,
    )
    out: dict[str, Any] = {
        "size_type": st,
        "size_display": format_product_item_size_display(item),
    }
    if item.size_number is not None:
        out["size"] = item.size_number
    if item.size_mm is not None:
        out["size_mm"] = float(item.size_mm)
    if item.height_mm is not None and item.width_mm is not None:
        out["height_mm"] = float(item.height_mm)
        out["width_mm"] = float(item.width_mm)
    return out


def item_filter_for_size(sku, *, size_number=None, size_mm=None, height_mm=None, width_mm=None):
    """Q filter for exact size line match under sku (including all-null size)."""
    q = Q(sku=sku)
    if size_number is not None:
        q &= Q(size_number=size_number)
    else:
        q &= Q(size_number__isnull=True)
    if size_mm is not None:
        q &= Q(size_mm=size_mm)
    else:
        q &= Q(size_mm__isnull=True)
    if height_mm is not None:
        q &= Q(height_mm=height_mm)
    else:
        q &= Q(height_mm__isnull=True)
    if width_mm is not None:
        q &= Q(width_mm=width_mm)
    else:
        q &= Q(width_mm__isnull=True)
    return q


def parse_size_distribution_row(size_type: str, row: dict, idx: int, errors: dict) -> tuple[dict[str, Any], int] | None:
    """
    Parse one make-bag / API row into (size_field_kwargs, qty).
    ``size_type`` is NUMBER / MM / HW (from request or UI).
    """
    if not isinstance(row, dict):
        errors.setdefault("size_distribution", []).append(f"Row {idx}: must be an object.")
        return None
    try:
        q = int(row.get("qty"))
    except (TypeError, ValueError):
        errors.setdefault("size_distribution", []).append(f"Row {idx}: qty must be an integer.")
        return None
    if q < 1:
        errors.setdefault("size_distribution", []).append(f"Row {idx}: qty must be >= 1.")
        return None

    st = (size_type or SIZE_NUMBER).strip().upper() if isinstance(size_type, str) else SIZE_NUMBER
    if st not in (SIZE_NUMBER, SIZE_MM, SIZE_HW):
        errors.setdefault("size_distribution", []).append(f"Row {idx}: invalid size_type {st!r}.")
        return None
    try:
        if st == SIZE_NUMBER:
            n = _int_safe(row.get("size") if row.get("size") is not None else row.get("size_number"))
            if n is None:
                raise ValidationError("size (integer) is required for NUMBER.")
            kw = {"size_number": n, "size_mm": None, "height_mm": None, "width_mm": None}
        elif st == SIZE_MM:
            sm = _dec(row.get("size_mm") if row.get("size_mm") is not None else row.get("size"))
            if sm is None:
                raise ValidationError("size_mm is required for MM.")
            kw = {"size_number": None, "size_mm": sm, "height_mm": None, "width_mm": None}
        else:
            hm = _dec(row.get("height_mm"))
            wm = _dec(row.get("width_mm"))
            if hm is None or wm is None:
                raw = (row.get("size") or row.get("pair") or "")
                if isinstance(raw, str) and "x" in raw.lower():
                    parts = re.split(r"[x×]", raw, maxsplit=1, flags=re.I)
                    if len(parts) == 2:
                        hm = _dec(parts[0].strip())
                        wm = _dec(parts[1].strip())
            if hm is None or wm is None:
                raise ValidationError("height_mm and width_mm (or size like '20x10') are required for HW.")
            kw = {"size_number": None, "size_mm": None, "height_mm": hm, "width_mm": wm}
        validate_product_item_size_fields(**kw)
        return (kw, q)
    except ValidationError as e:
        errors.setdefault("size_distribution", []).append(f"Row {idx}: {e.messages[0] if e.messages else str(e)}")
        return None


def merge_size_distribution_rows(size_type: str, raw_rows: list) -> list[tuple[dict[str, Any], int]]:
    """Merge duplicate size keys; returns [(kwargs, qty), ...]."""
    merged: dict[tuple, int] = {}
    errors: dict = {}
    for idx, row in enumerate(raw_rows or []):
        parsed = parse_size_distribution_row(size_type, row, idx, errors)
        if not parsed:
            continue
        kwargs, q = parsed
        key = (
            kwargs.get("size_number"),
            str(kwargs.get("size_mm")) if kwargs.get("size_mm") is not None else None,
            str(kwargs.get("height_mm")) if kwargs.get("height_mm") is not None else None,
            str(kwargs.get("width_mm")) if kwargs.get("width_mm") is not None else None,
        )
        merged[key] = merged.get(key, 0) + q
    if errors.get("size_distribution"):
        raise ValidationError(errors)
    out = []
    for key, qty in merged.items():
        sn, sm_s, hm_s, wm_s = key
        sm = Decimal(sm_s) if sm_s is not None else None
        hm = Decimal(hm_s) if hm_s is not None else None
        wm = Decimal(wm_s) if wm_s is not None else None
        out.append(
            (
                {
                    "size_number": sn,
                    "size_mm": sm,
                    "height_mm": hm,
                    "width_mm": wm,
                },
                qty,
            )
        )
    return out


def product_item_search_q(term: str) -> Q:
    """Broad text search across structured size + code/name."""
    t = (term or "").strip()
    if not t:
        return Q()
    q = (
        Q(sku__product_code__icontains=t)
        | Q(sku__sku_code__icontains=t)
        | Q(store_variant_name__icontains=t)
        | Q(sku__product_group__style_name__icontains=t)
    )
    if t.isdigit():
        q |= Q(size_number=int(t))
    low = t.lower()
    if "x" in low:
        parts = re.split(r"[x×]", t, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            ha, wa = _dec(parts[0]), _dec(parts[1])
            if ha is not None and wa is not None:
                q |= Q(height_mm=ha, width_mm=wa)
    dm = _dec(t)
    if dm is not None:
        q |= Q(size_mm=dm) | Q(height_mm=dm) | Q(width_mm=dm)
    return q


def apply_create_payload_size_fields(create_payload: dict) -> dict[str, Any]:
    """
    Build size_* kwargs from create_item / template body (optional size).
    """
    row = {
        "size_number": create_payload.get("size_number"),
        "size_mm": create_payload.get("size_mm"),
        "height_mm": create_payload.get("height_mm"),
        "width_mm": create_payload.get("width_mm"),
        "size": create_payload.get("size"),
        "size_type": create_payload.get("size_type"),
    }
    if row["size_number"] is None and row.get("size") is not None:
        row["size_number"] = row.get("size")
    st = infer_size_type_from_wizard_row(row)
    if st is None:
        kw = {"size_number": None, "size_mm": None, "height_mm": None, "width_mm": None}
        validate_product_item_size_fields(**kw)
        return kw
    parsed_row = {**row, "qty": 1}
    errs: dict = {}
    parsed = parse_size_distribution_row(st, parsed_row, 0, errs)
    if errs.get("size_distribution"):
        raise ValidationError(errs)
    if not parsed:
        kw = size_fields_from_wizard_row(row)
        validate_product_item_size_fields(**kw)
        return kw
    return parsed[0]
