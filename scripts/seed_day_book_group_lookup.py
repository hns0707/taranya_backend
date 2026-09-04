"""
Seed DAY_BOOK_GROUP lookup + default values (idempotent).

Usage (from ecom_backend):
  python scripts/seed_day_book_group_lookup.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ecom_backend.settings")
django.setup()

from django.utils import timezone  # noqa: E402
from shared.models import Lookup, LookupValue  # noqa: E402

LOOKUP_CODE = "DAY_BOOK_GROUP"
DEFAULTS = [
    ("ADVANCE", "Advance", 10),
    ("BORROWING", "Borrowing", 20),
    ("UDHAR", "Udhar", 30),
    ("LENDING", "Lending", 40),
    ("MISC", "Misc.", 50),
    ("HUF", "HUF", 60),
    ("HUF_I", "HUF I", 70),
]


def main() -> None:
    now = timezone.now()
    lookup, created = Lookup.objects.get_or_create(
        code=LOOKUP_CODE,
        defaults={
            "name": "Day Book Group",
            "description": "Manual entry / ledger grouping for Daily Book",
            "is_active": True,
            "system_created_at": now,
            "system_updated_at": now,
        },
    )
    if not created and not lookup.is_active:
        lookup.is_active = True
        lookup.save(update_fields=["is_active", "system_updated_at"])
    print(("Created" if created else "Exists"), f"lookup {LOOKUP_CODE} id={lookup.id}")

    for code, label, sort_order in DEFAULTS:
        _, v_created = LookupValue.objects.get_or_create(
            lookup=lookup,
            code=code,
            defaults={
                "label": label,
                "is_active": True,
                "sort_order": sort_order,
                "system_created_at": now,
                "system_updated_at": now,
            },
        )
        print(("  + " if v_created else "  = "), f"{code} ({label})")


if __name__ == "__main__":
    main()
