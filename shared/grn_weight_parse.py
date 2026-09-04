"""
Parse GRN weight fields: plain numbers or optional trailing 'g' (grams).
Examples: 20, 20.5, 20g, 20.5G
"""
import re
from decimal import Decimal, InvalidOperation

# Number with optional trailing g/G (whole field must match).
_WEIGHT_WITH_G = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+))\s*g\s*$",
    re.IGNORECASE,
)
# Plain numeric (no unit suffix).
_WEIGHT_PLAIN = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+))\s*$",
)


def normalize_weight_input(raw) -> str:
    """
    Strip optional 'g' suffix for API/storage. Returns numeric string or '' if empty.
    Does not validate range; raises ValueError if format is invalid.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    m = _WEIGHT_WITH_G.match(s)
    if m:
        return m.group(1)
    m = _WEIGHT_PLAIN.match(s)
    if m:
        return m.group(1)
    raise ValueError(f"Invalid weight: {s!r}. Use a number or add g (e.g. 20 or 20g).")


def parse_optional_weight_decimal(raw, field_name: str, errors: dict):
    """
    Like optional decimal for GRN weights: empty → None; 20 / 20g → Decimal.
    Appends to errors dict on failure.
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        normalized = normalize_weight_input(raw)
        return Decimal(normalized)
    except (InvalidOperation, ValueError, TypeError):
        errors.setdefault(field_name, []).append(
            f"{field_name} must be a valid number (e.g. 20 or 20g)."
        )
        return None
