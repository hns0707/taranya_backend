"""Unit tests for Day Book group classification (no Django DB required)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.day_book_groups as g


def _install_groups(*codes: str, labels: dict[str, str] | None = None) -> None:
    labels = labels or {}
    known = frozenset(set(g.DAY_BOOK_GROUP_ORDER) | set(codes))
    label_map: dict[str, str] = {}
    for code, label in g.DAY_BOOK_GROUP_LABELS.items():
        label_map[g._norm(label)] = code
        label_map[code] = code
    for code in codes:
        c = g._norm(code)
        label_map[c] = c
        if code in labels:
            label_map[g._norm(labels[code])] = c
        else:
            label_map[g._norm(code.replace('_', ' '))] = c
    for alias, target in g.ALIASES.items():
        if target in known:
            label_map[g._norm(alias)] = target

    def _fake_index():
        return known, label_map

    g._load_group_index = _fake_index  # type: ignore


def test_invoice_group_when_seeded():
    _install_groups('INVOICE', 'UPI', labels={'INVOICE': 'Invoice', 'UPI': 'Upi'})
    assert g.classify_day_book_entry(
        source='POS_INVOICE',
        transaction_mode='INVOICE',
        payment_mode=None,
        narration='Catalogue invoice — Trilok',
    ) == 'INVOICE'


def test_invoice_falls_back_misc_without_group():
    _install_groups('UPI')
    assert g.classify_day_book_entry(
        source='POS_INVOICE',
        transaction_mode='INVOICE',
        payment_mode=None,
    ) == 'MISC'


def test_upi_payment_mode_matches_group():
    _install_groups('INVOICE', 'UPI', labels={'UPI': 'Upi'})
    assert g.classify_day_book_entry(
        source='SCHEME_PAYMENT',
        transaction_mode='Upi',
        payment_mode='UPI',
        narration='UPI — RCPT',
    ) == 'UPI'


def test_scheme_payment_when_grouped():
    _install_groups('SCHEME_PAYMENT', labels={'SCHEME_PAYMENT': 'Scheme Payment'})
    assert g.classify_day_book_entry(
        source='SCHEME_PAYMENT',
        transaction_mode='SCHEME PAYMENT',
        payment_mode=None,
    ) == 'SCHEME_PAYMENT'


def test_scheme_without_group_is_misc():
    _install_groups('INVOICE', 'UPI')
    assert g.classify_day_book_entry(
        source='SCHEME_PAYMENT',
        transaction_mode='SCHEME PAYMENT',
        payment_mode=None,
    ) == 'MISC'


def test_label_match_for_custom_group():
    _install_groups('CARD', labels={'CARD': 'Card'})
    assert g.classify_day_book_entry(
        source='POS_INVOICE',
        transaction_mode='Card',
        payment_mode='CARD',
    ) == 'CARD'


def test_advance_and_udhar_builtins():
    _install_groups()
    assert g.classify_day_book_entry(
        source='POS_ADVANCE', transaction_mode='ADVANCE', payment_mode='JAMA'
    ) == 'ADVANCE'
    assert g.classify_day_book_entry(
        source='POS_UDHAR', transaction_mode='UDHAR', payment_mode='UDHAR'
    ) == 'UDHAR'


def test_manual_legacy_borrowing():
    _install_groups()
    assert g.normalize_manual_entry_type('BORROWINGS') == 'BORROWING'
    assert g.normalize_manual_entry_type('INVOICE') == 'MISC'
    _install_groups('INVOICE')
    assert g.normalize_manual_entry_type('INVOICE') == 'INVOICE'


if __name__ == '__main__':
    tests = [
        test_invoice_group_when_seeded,
        test_invoice_falls_back_misc_without_group,
        test_upi_payment_mode_matches_group,
        test_scheme_payment_when_grouped,
        test_scheme_without_group_is_misc,
        test_label_match_for_custom_group,
        test_advance_and_udhar_builtins,
        test_manual_legacy_borrowing,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f'OK  {fn.__name__}')
        except Exception as exc:
            failed += 1
            print(f'FAIL {fn.__name__}: {exc}')
    if failed:
        raise SystemExit(1)
    print(f'\nAll {len(tests)} tests passed.')
