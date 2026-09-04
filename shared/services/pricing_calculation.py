from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django.utils import timezone

from shared.services.metal_rate_service import get_metal_rate_by_date


_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


def _to_decimal(value, default: Decimal = _ZERO) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return default


def _basis_weight(item, net_weight_override: Decimal | None, gross_weight_override: Decimal | None) -> Decimal:
    """
    Weight used for making (per-gm / % of metal value), from product
    `charge_apply`: net_wt (default) or gross_wt.
    """
    charge_apply = (getattr(item, "charge_apply", None) or "").strip().lower()
    if charge_apply in ("gross_wt", "gross", "grosswt"):
        return _to_decimal(
            gross_weight_override if gross_weight_override is not None else getattr(item, "gross_weight", None)
        )
    return _to_decimal(
        net_weight_override if net_weight_override is not None else getattr(item, "net_weight", None)
    )


def _metal_bom_weight_total(item) -> Decimal:
    total = _ZERO
    for bom in getattr(item, "bom_items", []).all():
        if (getattr(bom, "material_type", None) or "").strip().upper() != "METAL":
            continue
        total += _to_decimal(getattr(bom, "weight", None))
    return total


def _making_line_weight(
    bom,
    basis_wt: Decimal,
    *,
    metal_bom_total: Decimal,
    piece_override: bool,
    template_basis: Decimal,
) -> Decimal:
    """
    Weight for one BOM line in making calc.

    Making applies on `basis_wt` (net or gross per charge_apply). METAL lines
    share that basis by BOM.weight proportion. BOM.weight is usually net, so we
    must NOT use raw BOM.weight when charge_apply is gross_wt.

    Example: rate 14000, net 10, gross 12, special 9.9%
      net_wt  → 10 × 14000 × 9.9% = 13,860
      gross_wt → 12 × 14000 × 9.9% = 16,632
    """
    mt = (getattr(bom, "material_type", None) or "").strip().upper()
    line_template = _to_decimal(getattr(bom, "weight", None), default=_ZERO)

    if mt == "METAL" and basis_wt > _ZERO:
        if metal_bom_total > _ZERO and line_template > _ZERO:
            return basis_wt * line_template / metal_bom_total
        return basis_wt

    # Stone / other: keep template line weight; scale when piece weights differ.
    line_wt = line_template if line_template > _ZERO else basis_wt
    if piece_override and template_basis > _ZERO and basis_wt > _ZERO and line_template > _ZERO:
        line_wt = line_template * basis_wt / template_basis
    elif piece_override and basis_wt > _ZERO and line_template <= _ZERO:
        line_wt = basis_wt
    if line_wt <= _ZERO:
        line_wt = basis_wt
    return line_wt


def _charge_mode(attr) -> str:
    """
    Resolve making charge mode from LookupValue code/label (+ special_charge hint).

    Percentage is checked before per-gram so labels/codes that mention both
    (e.g. PERCENTAGE_PER_GM) still compute % of metal value, not ₹/g.
    """
    lv = getattr(attr, "charge_type", None)
    code = (getattr(lv, "code", None) or "").strip().lower().replace("-", "_").replace(" ", "_")
    label = (getattr(lv, "label", None) or "").strip().lower().replace("-", "_").replace(" ", "_")
    txt = f"{code}|{label}"

    if code in ("percentage", "percent", "pct", "per_cent", "pct_of_metal", "pct_of_gold") or any(
        k in txt for k in ("percentage", "percent", "pct", "per_cent", "%")
    ):
        return "percentage"
    if code in ("per_gm", "pergram", "per_gram", "pergm", "per_g") or any(
        k in txt for k in ("per_gm", "pergram", "per_gram", "pergm")
    ):
        return "per_gm"
    if code in ("flat", "fixed", "lumpsum", "lump_sum") or any(
        k in txt for k in ("flat", "fixed", "lumpsum", "lump_sum")
    ):
        return "flat"
    raw = str(getattr(attr, "special_charge", "") or "")
    if "%" in raw:
        return "percentage"
    return "flat"


def _attr_has_making_charge(attr) -> bool:
    return bool(str(getattr(attr, "special_charge", "") or "").strip())


def _bom_making_pairs(item):
    """
    (bom, attr) pairs used for making.

    Prefer attributes on the item's own BOM. If none have a special_charge
    (common for variant children whose METAL BOM is recreated without attrs),
    reuse the parent product's making attributes on matching material types
    while still using this item's BOM weight / metal for the amount.
    """
    boms = list(getattr(item, "bom_items", []).all())
    own_pairs = []
    for bom in boms:
        for attr in getattr(bom, "attributes", []).all():
            if _attr_has_making_charge(attr):
                own_pairs.append((bom, attr))
    if own_pairs:
        return own_pairs

    parent = getattr(item, "parent_product_item", None)
    if parent is None:
        parent_id = getattr(item, "parent_product_item_id", None)
        if parent_id:
            parent = item.__class__.objects.filter(pk=parent_id).prefetch_related(
                "bom_items__attributes__charge_type",
            ).first()
    if parent is None or parent is item:
        return []

    parent_attrs_by_mt: dict[str, list] = {}
    for pbom in getattr(parent, "bom_items", []).all():
        mt = (getattr(pbom, "material_type", None) or "").strip().upper() or "METAL"
        for attr in getattr(pbom, "attributes", []).all():
            if _attr_has_making_charge(attr):
                parent_attrs_by_mt.setdefault(mt, []).append(attr)

    inherited = []
    for bom in boms:
        mt = (getattr(bom, "material_type", None) or "").strip().upper() or "METAL"
        for attr in parent_attrs_by_mt.get(mt, []):
            inherited.append((bom, attr))
    return inherited


def _purity_name_for_rate_lookup(bom) -> str | None:
    purity = getattr(bom, "purity", None)
    if purity is None:
        return None
    for candidate in (
        getattr(purity, "purity_name", None),
        getattr(purity, "type", None),
    ):
        s = (candidate or "").strip()
        if s:
            return s
    return None


def _metal_rate_per_gm(bom, *, rate_date=None, branch_id=None) -> Decimal:
    metal_id = getattr(bom, "metal_id", None)
    if not metal_id:
        return _ZERO
    purity_name = _purity_name_for_rate_lookup(bom)
    row = get_metal_rate_by_date(
        metal_id,
        rate_date or timezone.localdate(),
        purity_name=purity_name or "24K",
        branch_id=branch_id,
    )
    rv = getattr(row, "sell_price", None) or getattr(row, "rate_value", None) or row
    return _to_decimal(rv, default=_ZERO)


def _stone_rate_per_weight_unit(bom) -> Decimal:
    stone = getattr(bom, "stone", None)
    if stone is None:
        return _ZERO
    return _to_decimal(getattr(stone, "default_rate", None), default=_ZERO)


def _line_material_value(bom, line_wt: Decimal, *, rate_date=None, branch_id=None, gold_rate_override: Decimal | None = None) -> Decimal:
    mt = (getattr(bom, "material_type", None) or "").strip().upper()
    if mt == "METAL":
        rate = gold_rate_override if gold_rate_override is not None and gold_rate_override > _ZERO else _metal_rate_per_gm(bom, rate_date=rate_date, branch_id=branch_id)
        return rate * line_wt
    if mt == "STONE":
        return _stone_rate_per_weight_unit(bom) * line_wt
    return _ZERO


def primary_metal_rate_for_item(item, *, rate_date=None, branch_id=None) -> Decimal:
    """First available METAL BOM line rate per gm (sell)."""
    for bom in getattr(item, "bom_items", []).all():
        if (getattr(bom, "material_type", None) or "").strip().upper() != "METAL":
            continue
        rate = _metal_rate_per_gm(bom, rate_date=rate_date, branch_id=branch_id)
        if rate > _ZERO:
            return rate
    return _ZERO


def compute_making_charges_for_item(
    item,
    *,
    net_weight_override: Decimal | None = None,
    gross_weight_override: Decimal | None = None,
    rate_date=None,
    branch_id=None,
    gold_rate_override: Decimal | None = None,
) -> Decimal:
    """
    Aggregate making charges from ProductBOM -> ProductAttribute.

    Weight basis comes from item.charge_apply:
      net_wt (default) → use net weight
      gross_wt         → use gross weight

    Formula (percentage / metal):
      making = applicable_weight × metal_rate × (special_charge / 100)

    - per_gm: special_charge × applicable_weight (shared across METAL BOM lines)
    - flat: special_charge
    - percentage: special_charge % of material value
        METAL:  percentage of (metal_rate_per_gm × applicable_weight share)
        STONE:  percentage of (stone.default_rate × stone line weight)
      If material value cannot be resolved, fallback to % of applicable weight.
    """
    basis_wt = _basis_weight(item, net_weight_override, gross_weight_override)
    template_basis = _basis_weight(item, None, None)
    piece_override = net_weight_override is not None or gross_weight_override is not None
    metal_bom_total = _metal_bom_weight_total(item)
    total = _ZERO
    line_wt_cache: dict[int, Decimal] = {}

    for bom, attr in _bom_making_pairs(item):
        bom_id = id(bom)
        if bom_id not in line_wt_cache:
            line_wt_cache[bom_id] = _making_line_weight(
                bom,
                basis_wt,
                metal_bom_total=metal_bom_total,
                piece_override=piece_override,
                template_basis=template_basis,
            )
        line_wt = line_wt_cache[bom_id]

        raw = str(getattr(attr, "special_charge", "") or "").strip()
        if not raw:
            continue
        numeric = _to_decimal(raw.replace("%", ""))
        if numeric <= _ZERO:
            continue
        mode = _charge_mode(attr)
        if mode == "per_gm":
            total += numeric * line_wt
        elif mode == "percentage":
            line_value = _line_material_value(
                bom, line_wt,
                rate_date=rate_date,
                branch_id=branch_id,
                gold_rate_override=gold_rate_override,
            )
            base = line_value if line_value > _ZERO else line_wt
            total += (numeric / _HUNDRED) * base
        else:
            total += numeric

    if total < _ZERO:
        total = _ZERO
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_making_charges_breakdown_for_item(
    item,
    *,
    net_weight_override: Decimal | None = None,
    gross_weight_override: Decimal | None = None,
    rate_date=None,
    branch_id=None,
    gold_rate_override: Decimal | None = None,
):
    """
    Line-level breakdown for making charges.
    Returns dict: { total: Decimal, lines: list[dict] }.
    """
    basis_wt = _basis_weight(item, net_weight_override, gross_weight_override)
    template_basis = _basis_weight(item, None, None)
    piece_override = net_weight_override is not None or gross_weight_override is not None
    metal_bom_total = _metal_bom_weight_total(item)
    total = _ZERO
    lines = []
    line_wt_cache: dict[int, Decimal] = {}

    for bom, attr in _bom_making_pairs(item):
        bom_id = id(bom)
        if bom_id not in line_wt_cache:
            lw = _making_line_weight(
                bom,
                basis_wt,
                metal_bom_total=metal_bom_total,
                piece_override=piece_override,
                template_basis=template_basis,
            )
            if lw <= _ZERO:
                lw = basis_wt
            line_wt_cache[bom_id] = lw
        line_wt = line_wt_cache[bom_id]
        material_type = (getattr(bom, "material_type", None) or "").strip().upper()

        raw = str(getattr(attr, "special_charge", "") or "").strip()
        if not raw:
            continue
        numeric = _to_decimal(raw.replace("%", ""))
        if numeric <= _ZERO:
            continue
        mode = _charge_mode(attr)
        if mode == "per_gm":
            base = line_wt
            amount = numeric * base
        elif mode == "percentage":
            line_value = _line_material_value(
                bom, line_wt,
                rate_date=rate_date,
                branch_id=branch_id,
                gold_rate_override=gold_rate_override,
            )
            base = line_value if line_value > _ZERO else line_wt
            amount = (numeric / _HUNDRED) * base
        else:
            base = _ZERO
            amount = numeric

        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += amount
        lines.append(
            {
                "bomId": getattr(bom, "id", None),
                "materialType": material_type,
                "chargeType": mode,
                "specialChargeInput": raw,
                "specialChargeNumeric": str(numeric),
                "lineWeight": str(line_wt),
                "baseValue": str(base),
                "amount": str(amount),
            }
        )

    if total < _ZERO:
        total = _ZERO
    total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"total": total, "lines": lines}


def compute_operation_charges_for_item(
    item,
    *,
    net_weight_override: Decimal | None = None,
    gross_weight_override: Decimal | None = None,
) -> Decimal:
    """
    Aggregate ProductOperationCharge rows for an item.

    Flat-sum only:
    - "500"  -> +500
    - "10%"  -> +10 (percent symbol stripped; treated as plain numeric value)
    - empty/invalid -> ignored
    """
    total = _ZERO
    for row in getattr(item, "operation_charges", []).all():
        raw = str(getattr(row, "charge_value", "") or "").strip()
        if not raw:
            continue
        numeric = _to_decimal(raw.replace("%", ""))
        if numeric <= _ZERO:
            continue
        total += numeric
    if total < _ZERO:
        total = _ZERO
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_operation_charges_breakdown_for_item(
    item,
    *,
    net_weight_override: Decimal | None = None,
    gross_weight_override: Decimal | None = None,
):
    """
    Line-level breakdown for ProductOperationCharge.
    Returns dict: { total: Decimal, lines: list[dict] }.
    """
    total = _ZERO
    lines = []

    for row in getattr(item, "operation_charges", []).all():
        raw = str(getattr(row, "charge_value", "") or "").strip()
        if not raw:
            continue
        numeric = _to_decimal(raw.replace("%", ""))
        if numeric <= _ZERO:
            continue
        amount = numeric
        mode = "flat"
        base = _ZERO

        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += amount
        lines.append(
            {
                "operationChargeId": getattr(row, "id", None),
                "componentName": (getattr(row, "component_name", None) or "").strip(),
                "chargeType": mode,
                "chargeValueInput": raw,
                "chargeValueNumeric": str(numeric),
                "baseValue": str(base),
                "amount": str(amount),
            }
        )

    if total < _ZERO:
        total = _ZERO
    total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"total": total, "lines": lines}
