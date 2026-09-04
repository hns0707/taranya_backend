"""Seed DAY_BOOK_GROUP lookup + widen transaction_mode (PyMySQL, idempotent)."""
from __future__ import annotations

import os

import pymysql

CFG = {
    "host": os.getenv("DB_HOST", "ashish-jewel-uat.cxy0make60ag.ap-south-1.rds.amazonaws.com"),
    "user": os.getenv("DB_USER", "ashishjewels"),
    "password": os.getenv("DB_PASSWORD", "OneAshish08642"),
    "database": os.getenv("DB_NAME", "ashish_db_uat"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "charset": "utf8mb4",
    "autocommit": True,
}

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
    conn = pymysql.connect(**CFG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM lookups WHERE code=%s", ("DAY_BOOK_GROUP",))
            row = cur.fetchone()
            if row:
                lookup_id = row[0]
                print(f"Lookup exists id={lookup_id}")
            else:
                cur.execute(
                    """
                    INSERT INTO lookups
                      (code, name, description, is_active, system_created_at, system_updated_at)
                    VALUES
                      (%s, %s, %s, 1, NOW(6), NOW(6))
                    """,
                    (
                        "DAY_BOOK_GROUP",
                        "Day Book Group",
                        "Manual entry / ledger grouping for Daily Book",
                    ),
                )
                lookup_id = cur.lastrowid
                print(f"Created lookup id={lookup_id}")

            for code, label, sort_order in DEFAULTS:
                cur.execute(
                    "SELECT id FROM lookup_values WHERE lookup_id=%s AND code=%s",
                    (lookup_id, code),
                )
                if cur.fetchone():
                    print(f"  = {code}")
                    continue
                cur.execute(
                    """
                    INSERT INTO lookup_values
                      (lookup_id, code, label, is_active, sort_order, system_created_at, system_updated_at)
                    VALUES
                      (%s, %s, %s, 1, %s, NOW(6), NOW(6))
                    """,
                    (lookup_id, code, label, sort_order),
                )
                print(f"  + {code}")

            cur.execute(
                """
                ALTER TABLE day_book_manual_entries
                MODIFY COLUMN transaction_mode VARCHAR(50) NOT NULL
                """
            )
            print("Altered day_book_manual_entries.transaction_mode -> VARCHAR(50)")

            cur.execute(
                "SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='django_migrations'",
                (CFG["database"],),
            )
            if cur.fetchone():
                cur.execute(
                    "SELECT 1 FROM django_migrations WHERE app='shared' AND name=%s",
                    ("0021_daybook_dynamic_groups",),
                )
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO django_migrations (app, name, applied) VALUES (%s,%s,NOW(6))",
                        ("shared", "0021_daybook_dynamic_groups"),
                    )
                    print("Recorded django_migrations 0021")

            cur.execute(
                "SELECT code, label FROM lookup_values WHERE lookup_id=%s ORDER BY sort_order",
                (lookup_id,),
            )
            print("Final values:", cur.fetchall())
    finally:
        conn.close()


if __name__ == "__main__":
    main()
