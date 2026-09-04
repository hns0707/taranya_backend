"""
Day Book ledger grouping for cashier / accounts reporting.

Built-in groups: Advance | Borrowing | Udhar | Lending | Misc. | HUF | HUF I
Custom groups: LookupValue rows under lookup code DAY_BOOK_GROUP
  (e.g. UPI, INVOICE, SCHEME_PAYMENT).

Rule: if an entry's mode / payment / source maps to any active grouping,
use that group. Misc. is only the fallback when nothing matches.
"""

from __future__ import annotations

import re
from functools import lru_cache

GROUP_ADVANCE = 'ADVANCE'
GROUP_BORROWING = 'BORROWING'
GROUP_UDHAR = 'UDHAR'
GROUP_LENDING = 'LENDING'
GROUP_MISC = 'MISC'
GROUP_HUF = 'HUF'
GROUP_HUF_I = 'HUF_I'

DAY_BOOK_LOOKUP_CODE = 'DAY_BOOK_GROUP'

DAY_BOOK_GROUP_ORDER = (
    GROUP_ADVANCE,
    GROUP_BORROWING,
    GROUP_UDHAR,
    GROUP_LENDING,
    GROUP_MISC,
    GROUP_HUF,
    GROUP_HUF_I,
)

DAY_BOOK_GROUP_LABELS = {
    GROUP_ADVANCE: 'Advance',
    GROUP_BORROWING: 'Borrowing',
    GROUP_UDHAR: 'Udhar',
    GROUP_LENDING: 'Lending',
    GROUP_MISC: 'Misc.',
    GROUP_HUF: 'HUF',
    GROUP_HUF_I: 'HUF I',
}

# Legacy / alternate spellings → canonical group code
ALIASES: dict[str, str] = {
    'BORROWINGS': GROUP_BORROWING,
    'BORROWING': GROUP_BORROWING,
    'MONEY_LENDING': GROUP_LENDING,
    'LENDING': GROUP_LENDING,
    'JAMA': GROUP_ADVANCE,
    'ADVANCE': GROUP_ADVANCE,
    'UDHAR': GROUP_UDHAR,
    'HUF': GROUP_HUF,
    'HUF_I': GROUP_HUF_I,
    'MISC': GROUP_MISC,
    'EXPENSE': GROUP_MISC,
    'REPAIR_RECEIPT': GROUP_MISC,
    'OTHER': GROUP_MISC,
    'SCHEME': 'SCHEME_PAYMENT',
    'SCHEME_PAYMENT': 'SCHEME_PAYMENT',
    'SCHEME_PAYMENTS': 'SCHEME_PAYMENT',
    'POS_INVOICE': 'INVOICE',
    'INVOICE': 'INVOICE',
    'CATALOGUE_INVOICE': 'INVOICE',
}

# Source → candidate group codes to try (in order)
SOURCE_CANDIDATES: dict[str, tuple[str, ...]] = {
    'POS_INVOICE': ('INVOICE',),
    'POS_ADVANCE': ('ADVANCE',),
    'POS_UDHAR': ('UDHAR',),
    'SCHEME_PAYMENT': ('SCHEME_PAYMENT', 'SCHEME'),
}

VALID_MANUAL_ENTRY_TYPES = frozenset(DAY_BOOK_GROUP_ORDER)

_CODE_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,49}$')


def _norm(value: str | None) -> str:
    if not value:
        return ''
    return (
        str(value)
        .strip()
        .upper()
        .replace('-', '_')
        .replace('.', '')
        .replace('/', '_')
        .replace(' ', '_')
    )


def _norm_codes(raw_codes) -> set[str]:
    return {_norm(c) for c in raw_codes if _norm(c)}


def clear_day_book_group_cache() -> None:
    """Call after Lookup Master changes so classify picks up new groups."""
    fn = _load_group_index
    clear = getattr(fn, 'cache_clear', None)
    if callable(clear):
        clear()


@lru_cache(maxsize=1)
def _load_group_index() -> tuple[frozenset[str], dict[str, str]]:
    """
    Returns (active_codes, label_or_alias → code).

    Codes come from DAY_BOOK_GROUP lookup + built-ins.
    Labels map so "Upi" / "Invoice" match UPI / INVOICE.
    """
    codes: set[str] = set(DAY_BOOK_GROUP_ORDER)
    label_map: dict[str, str] = {}

    for code, label in DAY_BOOK_GROUP_LABELS.items():
        label_map[_norm(label)] = code
        label_map[code] = code

    try:
        from shared.models import LookupValue

        rows = LookupValue.objects.filter(
            lookup__code=DAY_BOOK_LOOKUP_CODE,
            lookup__is_active=True,
            is_active=True,
        ).values_list('code', 'label')
        for code, label in rows:
            c = _norm(code)
            if not c:
                continue
            codes.add(c)
            label_map[c] = c
            ln = _norm(label)
            if ln:
                label_map[ln] = c
    except Exception:
        pass

    for alias, target in ALIASES.items():
        # Prefer alias → target when target exists; else alias as itself if known
        if target in codes:
            label_map[_norm(alias)] = target
        elif _norm(alias) in codes:
            label_map[_norm(alias)] = _norm(alias)

    return frozenset(codes), dict(label_map)


def active_day_book_group_codes() -> set[str]:
    """Active custom + built-in codes from DAY_BOOK_GROUP lookup."""
    codes, _ = _load_group_index()
    return set(codes)


def day_book_group_label(code: str | None) -> str:
    c = _norm(code)
    if not c:
        return DAY_BOOK_GROUP_LABELS[GROUP_MISC]
    if c in DAY_BOOK_GROUP_LABELS:
        return DAY_BOOK_GROUP_LABELS[c]
    try:
        from shared.models import LookupValue

        label = (
            LookupValue.objects.filter(
                lookup__code=DAY_BOOK_LOOKUP_CODE,
                code__iexact=c,
                is_active=True,
            )
            .values_list('label', flat=True)
            .first()
        )
        if label:
            return str(label)
    except Exception:
        pass
    return c.replace('_', ' ').title()


def is_allowed_manual_entry_type(code: str | None) -> bool:
    c = _norm(code)
    if not c:
        return False
    if c in DAY_BOOK_GROUP_ORDER:
        return True
    codes = active_day_book_group_codes()
    if codes:
        return c in codes
    return bool(_CODE_RE.fullmatch(c))


def normalize_manual_entry_type(value: str | None) -> str:
    code = _norm(value)
    if not code:
        return GROUP_MISC
    resolved = match_day_book_group(code)
    if resolved:
        return resolved
    if is_allowed_manual_entry_type(code):
        return code
    return GROUP_MISC


def match_day_book_group(*candidates: str | None, codes: set[str] | frozenset[str] | None = None) -> str | None:
    """
    Resolve the first candidate that maps to an active Day Book group
    (by code, label, or alias). Returns None if nothing matches.
    """
    index_codes, label_map = _load_group_index()
    known = codes if codes is not None else index_codes

    for raw in candidates:
        if raw is None:
            continue
        c = _norm(raw)
        if not c:
            continue

        # Direct code hit
        if c in known:
            return c

        # Label / alias map (may point at canonical code)
        mapped = label_map.get(c)
        if mapped and mapped in known:
            return mapped

        # ALIASES table → only if target group exists
        aliased = ALIASES.get(c)
        if aliased:
            a = _norm(aliased)
            if a in known:
                return a

    return None


def classify_day_book_entry(
    *,
    source: str | None,
    transaction_mode: str | None,
    payment_mode: str | None = None,
    narration: str | None = None,
) -> str:
    """
    Pick ledger group for a Day Book line.

    Priority:
      1. transaction_mode / payment_mode / source aliases vs active groups
      2. Built-in POS advance / udhar shortcuts
      3. Narration heuristics for built-ins
      4. Well-formed custom mode code (when it is a registered group)
      5. Misc.
    """
    src = _norm(source)
    mode = _norm(transaction_mode)
    pay = _norm(payment_mode)
    codes, _ = _load_group_index()

    # Build ordered candidates — mode first so invoice money-in stays Invoice
    # even when a UPI tender is also present on a related out line.
    candidates: list[str | None] = [transaction_mode, mode, payment_mode, pay]

    for src_alias in SOURCE_CANDIDATES.get(src, ()):
        candidates.append(src_alias)

    # Soft aliases from mode/pay (SCHEME → SCHEME_PAYMENT, JAMA → ADVANCE, …)
    for raw in (mode, pay, src):
        if raw and raw in ALIASES:
            candidates.append(ALIASES[raw])

    matched = match_day_book_group(*candidates, codes=codes)
    if matched and matched != GROUP_MISC:
        return matched
    # Explicit Misc. only when the entry itself is misc / legacy expense
    if matched == GROUP_MISC and mode in {
        'MISC', 'EXPENSE', 'REPAIR_RECEIPT', 'OTHER',
    }:
        return GROUP_MISC

    # Built-in POS buckets (always, even if somehow missing from lookup)
    if src == 'POS_ADVANCE' or mode == 'ADVANCE' or pay == 'JAMA':
        return GROUP_ADVANCE
    if src == 'POS_UDHAR' or mode == 'UDHAR':
        return GROUP_UDHAR

    # Narration heuristics (built-ins only)
    narr = (narration or '').lower()
    if 'huf i' in narr or 'huf-i' in narr:
        return GROUP_HUF_I
    if re.search(r'\bhuf\b', narr):
        return GROUP_HUF
    if 'borrow' in narr:
        return GROUP_BORROWING
    if 'lend' in narr:
        return GROUP_LENDING
    if 'advance' in narr or 'jama' in narr:
        return GROUP_ADVANCE
    if 'udhar' in narr:
        return GROUP_UDHAR

    # Registered custom code used as mode (e.g. user-typed group on auto line)
    if mode and _CODE_RE.fullmatch(mode) and mode in codes:
        return mode

    return GROUP_MISC
