"""
Apply missing UAT schema objects to Prod ashish_db.

Order:
  1) ALTER missing columns on existing tables
  2) CREATE missing tables (upi_mandates before executions)
  3) Add FKs/indexes that depend on new columns/tables
  4) Day Book RBAC seed

Idempotent where possible. Does not copy row data.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pymysql

UAT = {
    "host": os.getenv("UAT_DB_HOST", "ashish-jewel-uat.cxy0make60ag.ap-south-1.rds.amazonaws.com"),
    "user": os.getenv("UAT_DB_USER", "ashishjewels"),
    "password": os.getenv("UAT_DB_PASSWORD", "OneAshish08642"),
    "port": int(os.getenv("UAT_DB_PORT", "3306")),
    "database": os.getenv("UAT_DB_NAME", "ashish_db_uat"),
    "connect_timeout": 30,
    "charset": "utf8mb4",
    "autocommit": True,
}
PROD = {
    "host": os.getenv("PROD_DB_HOST", "ashishjewels-db.cxy0make60ag.ap-south-1.rds.amazonaws.com"),
    "user": os.getenv("PROD_DB_USER", "ashishjewels"),
    "password": os.getenv("PROD_DB_PASSWORD", "OneAshish0864"),
    "port": int(os.getenv("PROD_DB_PORT", "3306")),
    "database": os.getenv("PROD_DB_NAME", "ashish_db"),
    "connect_timeout": 30,
    "charset": "utf8mb4",
    "autocommit": True,
}

MISSING_TABLES = [
    "upi_mandates",  # before executions
    "upi_mandate_executions",
    "catalogue_quote_change_logs",
    "catalogue_quote_contributors",
    "catalogue_quote_line_removal_requests",
    "catalogue_quote_visits",
    "day_book_days",
    "day_book_manual_entries",
]

MISSING_COLUMNS = {
    "catalogue_quote_lines": [
        "added_by_id",
        "is_removed",
        "pricing_meta",
        "removed_at",
        "removed_by_id",
    ],
    "catalogue_quote_payments": ["notes", "reference_no"],
    "catalogue_quotes": ["cart_pricing_meta", "sales_credit_snapshot", "version"],
    "grn_batches": ["stone_rate_basis", "stone_wt_unit"],
    "grn_lots": ["stone_rate_basis", "stone_wt_unit"],
    "pattern_code_registry": ["description"],
    "payments": ["payment_provider", "upi_execution_id"],
    "product_operation_charges": ["DESCRIPTION"],
    "purchase_orders": ["status"],
}


def connect(cfg):
    return pymysql.connect(**cfg)


def table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
        (schema, table),
    )
    return cur.fetchone() is not None


def column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (schema, table, column),
    )
    return cur.fetchone() is not None


def index_exists(cur, schema: str, table: str, index: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s LIMIT 1",
        (schema, table, index),
    )
    return cur.fetchone() is not None


def fk_exists(cur, schema: str, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS "
        "WHERE TABLE_SCHEMA=%s AND CONSTRAINT_NAME=%s AND CONSTRAINT_TYPE='FOREIGN KEY'",
        (schema, name),
    )
    return cur.fetchone() is not None


def show_create(cur, schema: str, table: str) -> str:
    cur.execute(f"SHOW CREATE TABLE `{schema}`.`{table}`")
    ddl = cur.fetchone()[1]
    # Reset AUTO_INCREMENT for clean create
    ddl = re.sub(r"AUTO_INCREMENT=\d+\s*", "", ddl)
    return ddl


def column_add_sql(cur, schema: str, table: str, column: str) -> str:
    cur.execute(
        """
        SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLUMN_NAME,
               DATA_TYPE, GENERATION_EXPRESSION
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
        """,
        (schema, table, column),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Column {schema}.{table}.{column} not found on UAT")
    col_type, is_nullable, default, extra, col_name, data_type, gen_expr = row

    parts = [f"ADD COLUMN `{col_name}` {col_type}"]

    # JSON defaults / generated
    if data_type == "json" and default is not None:
        # MySQL stores default expression strings oddly via information_schema
        d = str(default).strip()
        if "json_object" in d.lower():
            parts.append("NOT NULL DEFAULT (json_object())" if is_nullable == "NO" else "DEFAULT (json_object())")
        elif "json_array" in d.lower():
            parts.append("NOT NULL DEFAULT (json_array())" if is_nullable == "NO" else "DEFAULT (json_array())")
        else:
            # fallback: nullable JSON without default expression if unsure
            if is_nullable == "YES":
                parts.append("NULL")
            else:
                parts.append("NOT NULL")
        return f"ALTER TABLE `{table}` " + " ".join(parts)

    if is_nullable == "YES":
        parts.append("NULL")
    else:
        parts.append("NOT NULL")

    if default is not None:
        d = str(default)
        if d.upper() in ("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()") or d.upper().startswith("CURRENT_TIMESTAMP"):
            parts.append(f"DEFAULT {d}")
        elif data_type in ("int", "bigint", "smallint", "tinyint", "decimal", "float", "double"):
            parts.append(f"DEFAULT {d}")
        else:
            # quote string defaults; escape quotes
            escaped = d.replace("\\", "\\\\").replace("'", "''")
            parts.append(f"DEFAULT '{escaped}'")
    elif is_nullable == "NO" and data_type in ("text", "longtext", "mediumtext"):
        # existing rows need a default for online add — use empty string then we can leave NOT NULL
        parts.append("DEFAULT ''")
    elif is_nullable == "NO" and default is None and "auto_increment" not in (extra or "").lower():
        # Prefer a safe default for rolling out NOT NULL columns
        if data_type in ("int", "bigint", "smallint", "tinyint"):
            parts.append("DEFAULT 0")
        elif data_type in ("varchar", "char"):
            parts.append("DEFAULT ''")

    if extra and "auto_increment" not in extra.lower() and "DEFAULT_GENERATED" not in extra:
        # Keep ON UPDATE CURRENT_TIMESTAMP etc.
        if "on update" in extra.lower():
            parts.append(extra)

    return f"ALTER TABLE `{table}` " + " ".join(parts)


def run_sql(cur, sql: str, label: str):
    print(f"-> {label}")
    try:
        cur.execute(sql)
        print("  OK")
    except pymysql.err.OperationalError as e:
        # Duplicate column/key etc.
        code = e.args[0]
        if code in (1060, 1061, 1050, 1826):  # duplicate column/key/table/FK
            print(f"  SKIP ({code}): already exists")
        else:
            print(f"  FAIL ({code}): {e}")
            raise
    except Exception as e:
        print(f"  FAIL: {e}")
        raise


def apply_columns(uat_cur, prod_cur, prod_schema: str, uat_schema: str):
    print("\n=== 1) Missing columns ===")
    for table, cols in MISSING_COLUMNS.items():
        if not table_exists(prod_cur, prod_schema, table):
            print(f"SKIP table missing on prod: {table}")
            continue
        for col in cols:
            if column_exists(prod_cur, prod_schema, table, col):
                print(f"SKIP {table}.{col} (exists)")
                continue
            sql = column_add_sql(uat_cur, uat_schema, table, col)
            run_sql(prod_cur, sql, f"{table}.{col}: {sql}")


def apply_tables(uat_cur, prod_cur, prod_schema: str, uat_schema: str):
    print("\n=== 2) Missing tables ===")
    for table in MISSING_TABLES:
        if table_exists(prod_cur, prod_schema, table):
            print(f"SKIP {table} (exists)")
            continue
        if not table_exists(uat_cur, uat_schema, table):
            print(f"ERROR: {table} missing on UAT as well")
            continue
        ddl = show_create(uat_cur, uat_schema, table)
        run_sql(prod_cur, ddl, f"CREATE {table}")


def apply_fks_indexes(prod_cur, prod_schema: str):
    print("\n=== 3) Extra FKs / indexes ===")
    statements = [
        (
            "catalogue_quote_lines_added_by_id_idx",
            "CREATE INDEX `catalogue_quote_lines_added_by_id_idx` ON `catalogue_quote_lines` (`added_by_id`)",
            "catalogue_quote_lines",
            "index",
        ),
        (
            "catalogue_quote_lines_removed_by_id_idx",
            "CREATE INDEX `catalogue_quote_lines_removed_by_id_idx` ON `catalogue_quote_lines` (`removed_by_id`)",
            "catalogue_quote_lines",
            "index",
        ),
        (
            "catalogue_quote_lines_is_removed_idx",
            "CREATE INDEX `catalogue_quote_lines_is_removed_idx` ON `catalogue_quote_lines` (`is_removed`)",
            "catalogue_quote_lines",
            "index",
        ),
        (
            "catalogue_quote_lines_added_by_id_fk",
            "ALTER TABLE `catalogue_quote_lines` ADD CONSTRAINT `catalogue_quote_lines_added_by_id_fk` "
            "FOREIGN KEY (`added_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL",
            None,
            "fk",
        ),
        (
            "catalogue_quote_lines_removed_by_id_fk",
            "ALTER TABLE `catalogue_quote_lines` ADD CONSTRAINT `catalogue_quote_lines_removed_by_id_fk` "
            "FOREIGN KEY (`removed_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL",
            None,
            "fk",
        ),
        (
            "payments_payment_provider_idx",
            "CREATE INDEX `payments_payment_provider_idx` ON `payments` (`payment_provider`)",
            "payments",
            "index",
        ),
        (
            "payments_upi_execution_id_fk",
            "ALTER TABLE `payments` ADD CONSTRAINT `payments_upi_execution_id_fk` "
            "FOREIGN KEY (`upi_execution_id`) REFERENCES `upi_mandate_executions` (`id`) ON DELETE SET NULL",
            None,
            "fk",
        ),
    ]
    for name, sql, table, kind in statements:
        if kind == "index":
            if index_exists(prod_cur, prod_schema, table, name):
                print(f"SKIP index {name}")
                continue
        else:
            if fk_exists(prod_cur, prod_schema, name):
                print(f"SKIP fk {name}")
                continue
        try:
            run_sql(prod_cur, sql, name)
        except Exception as e:
            print(f"WARN continuing after {name}: {e}")


def apply_day_book_rbac(prod_cur, prod_schema: str):
    print("\n=== 4) Day Book RBAC ===")
    # module_id 4 = Accounts — verify
    prod_cur.execute(
        "SELECT id, code FROM modules WHERE id=4 OR code LIKE %s LIMIT 5",
        ("%ACCOUNT%",),
    )
    mods = prod_cur.fetchall()
    print("  modules sample:", mods)
    module_id = 4
    if mods:
        # Prefer id=4 if present, else first match
        for mid, code in mods:
            if mid == 4:
                module_id = 4
                break
        else:
            module_id = mods[0][0]

    # actions map
    prod_cur.execute("SELECT id, code FROM actions WHERE code IN ('VIEW','CREATE','UPDATE','DELETE')")
    action_map = {code: aid for aid, code in prod_cur.fetchall()}
    print("  actions:", action_map)
    if len(action_map) < 4:
        raise RuntimeError("Required actions VIEW/CREATE/UPDATE/DELETE not found")

    # section
    prod_cur.execute("SELECT id FROM sections WHERE code='CRM_ACCOUNTS_DAILY_BOOK'")
    row = prod_cur.fetchone()
    if row:
        section_id = row[0]
        print(f"  section exists id={section_id}")
    else:
        # Detect columns on sections
        prod_cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME='sections'",
            (prod_schema,),
        )
        scol = {r[0] for r in prod_cur.fetchall()}
        cols = ["module_id", "name", "code", "is_active"]
        vals = [module_id, "Daily Book", "CRM_ACCOUNTS_DAILY_BOOK", 1]
        if "sub_module_id" in scol:
            cols.insert(1, "sub_module_id")
            vals.insert(1, None)
        if "sort_order" in scol:
            cols.append("sort_order")
            vals.append(10)
        placeholders = ", ".join(["%s"] * len(vals))
        colsql = ", ".join(f"`{c}`" for c in cols)
        prod_cur.execute(f"INSERT INTO sections ({colsql}) VALUES ({placeholders})", vals)
        section_id = prod_cur.lastrowid
        print(f"  created section id={section_id}")

    perm_defs = [
        ("VIEW", "CRM_ACCOUNTS_DAILY_BOOK_VIEW", "Daily Book View"),
        ("CREATE", "CRM_ACCOUNTS_DAILY_BOOK_CREATE", "Daily Book Create"),
        ("UPDATE", "CRM_ACCOUNTS_DAILY_BOOK_UPDATE", "Daily Book Update"),
        ("DELETE", "CRM_ACCOUNTS_DAILY_BOOK_DELETE", "Daily Book Delete"),
    ]
    for action_code, pcode, pname in perm_defs:
        prod_cur.execute("SELECT id FROM permissions WHERE code=%s", (pcode,))
        if prod_cur.fetchone():
            print(f"  SKIP permission {pcode}")
            continue
        prod_cur.execute(
            "INSERT INTO permissions (section_id, action_id, code, name, is_active) VALUES (%s,%s,%s,%s,1)",
            (section_id, action_map[action_code], pcode, pname),
        )
        print(f"  created permission {pcode}")

    # Grant to role 1 (Super Admin) and role 2 (Accounts) if those roles exist
    for role_id in (1, 2):
        prod_cur.execute("SELECT id FROM roles WHERE id=%s", (role_id,))
        if not prod_cur.fetchone():
            print(f"  SKIP role_id={role_id} (not found)")
            continue
        # Detect role_permissions columns
        prod_cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='role_permissions'",
            (prod_schema,),
        )
        rpcols = {r[0] for r in prod_cur.fetchall()}
        prod_cur.execute(
            """
            SELECT p.id FROM permissions p
            WHERE p.code LIKE %s
              AND NOT EXISTS (
                SELECT 1 FROM role_permissions rp
                WHERE rp.role_id=%s AND rp.permission_id=p.id
              )
            """,
            ("CRM_ACCOUNTS_DAILY_BOOK_%", role_id),
        )
        for (pid,) in prod_cur.fetchall():
            cols = ["role_id", "permission_id"]
            vals = [role_id, pid]
            if "system_created_at" in rpcols:
                cols.append("system_created_at")
                vals.append(None)  # will set via NOW() below
            # Better use SQL NOW for timestamps
            if "system_created_at" in rpcols and "system_updated_at" in rpcols:
                prod_cur.execute(
                    "INSERT INTO role_permissions (role_id, permission_id, system_created_at, system_updated_at) "
                    "VALUES (%s,%s,NOW(6),NOW(6))",
                    (role_id, pid),
                )
            else:
                colsql = ", ".join(cols)
                placeholders = ", ".join(["%s"] * len(vals))
                # filter Nones for non-timestamp path
                prod_cur.execute(
                    f"INSERT INTO role_permissions ({colsql}) VALUES ({placeholders})",
                    vals,
                )
            print(f"  granted permission_id={pid} to role_id={role_id}")


def verify(prod_cur, prod_schema: str):
    print("\n=== VERIFY ===")
    missing_t = []
    for t in MISSING_TABLES:
        if not table_exists(prod_cur, prod_schema, t):
            missing_t.append(t)
    print("Missing tables after apply:", missing_t or "none")

    missing_c = []
    for table, cols in MISSING_COLUMNS.items():
        for col in cols:
            if not column_exists(prod_cur, prod_schema, table, col):
                missing_c.append(f"{table}.{col}")
    print("Missing columns after apply:", missing_c or "none")

    prod_cur.execute(
        "SELECT code FROM permissions WHERE code LIKE 'CRM_ACCOUNTS_DAILY_BOOK_%' ORDER BY code"
    )
    print("Day Book permissions:", [r[0] for r in prod_cur.fetchall()])
    return not missing_t and not missing_c


def main():
    print(f"PROD host={PROD['host']} db={PROD['database']}")
    print(f"UAT  host={UAT['host']} db={UAT['database']}")
    if "--yes" not in sys.argv:
        print("Refusing to run without --yes flag")
        sys.exit(2)

    uat = connect(UAT)
    prod = connect(PROD)
    try:
        with uat.cursor() as uat_cur, prod.cursor() as prod_cur:
            apply_columns(uat_cur, prod_cur, PROD["database"], UAT["database"])
            apply_tables(uat_cur, prod_cur, PROD["database"], UAT["database"])
            apply_fks_indexes(prod_cur, PROD["database"])
            apply_day_book_rbac(prod_cur, PROD["database"])
            ok = verify(prod_cur, PROD["database"])
        print("\nDONE" if ok else "\nDONE WITH GAPS — review output")
        sys.exit(0 if ok else 1)
    finally:
        uat.close()
        prod.close()


if __name__ == "__main__":
    main()
