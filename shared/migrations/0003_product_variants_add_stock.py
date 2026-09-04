"""
Add product_variants.stock when the column is missing.

The ProductVariant model includes `stock`, but some databases were created
before this column existed, which causes OperationalError (1054) on any query
that touches product_variants (e.g. catalogue prefetch).
"""

from django.db import migrations


def add_stock_if_missing(apps, schema_editor):
    conn = schema_editor.connection
    vendor = conn.vendor

    with conn.cursor() as cursor:
        if vendor == "mysql":
            cursor.execute(
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'product_variants'
                  AND COLUMN_NAME = 'stock'
                """
            )
            (cnt,) = cursor.fetchone()
            if cnt == 0:
                cursor.execute(
                    "ALTER TABLE product_variants ADD COLUMN stock INT NOT NULL DEFAULT 0"
                )
            return

        if vendor == "postgresql":
            cursor.execute(
                """
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'product_variants'
                  AND column_name = 'stock'
                """
            )
            (cnt,) = cursor.fetchone()
            if cnt == 0:
                cursor.execute(
                    "ALTER TABLE product_variants ADD COLUMN stock INTEGER NOT NULL DEFAULT 0"
                )
            return

        if vendor == "sqlite":
            cursor.execute("PRAGMA table_info(product_variants)")
            cols = {row[1] for row in cursor.fetchall()}
            if "stock" not in cols:
                cursor.execute(
                    "ALTER TABLE product_variants ADD COLUMN stock INTEGER NOT NULL DEFAULT 0"
                )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0002_add_ledger_fields"),
    ]

    operations = [
        migrations.RunPython(add_stock_if_missing, noop_reverse),
    ]
