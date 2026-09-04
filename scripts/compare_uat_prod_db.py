"""
Compare UAT vs Prod MySQL schemas (read-only).
Does NOT modify Prod.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import os

import pymysql

# Prefer env vars so credentials are not stored in source.
UAT = {
    "host": os.getenv("UAT_DB_HOST", "ashish-jewel-uat.cxy0make60ag.ap-south-1.rds.amazonaws.com"),
    "user": os.getenv("UAT_DB_USER", "ashishjewels"),
    "password": os.getenv("UAT_DB_PASSWORD", ""),
    "port": int(os.getenv("UAT_DB_PORT", "3306")),
    "connect_timeout": 25,
    "charset": "utf8mb4",
}
PROD = {
    "host": os.getenv("PROD_DB_HOST", "ashishjewels-db.cxy0make60ag.ap-south-1.rds.amazonaws.com"),
    "user": os.getenv("PROD_DB_USER", "ashishjewels"),
    "password": os.getenv("PROD_DB_PASSWORD", ""),
    "port": int(os.getenv("PROD_DB_PORT", "3306")),
    "connect_timeout": 25,
    "charset": "utf8mb4",
}

SKIP_DBS = {
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
    "tmp",
    "innodb",
}


def connect(cfg, database: str | None = None):
    kw = dict(cfg)
    if database:
        kw["database"] = database
    return pymysql.connect(**kw)


def list_dbs(cfg) -> list[str]:
    conn = connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            return [r[0] for r in cur.fetchall() if r[0] not in SKIP_DBS]
    finally:
        conn.close()


def list_tables(cfg, database: str) -> list[str]:
    conn = connect(cfg, database)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
                """,
                (database,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def columns(cfg, database: str, table: str) -> dict[str, dict]:
    conn = connect(cfg, database)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
                       EXTRA, COLUMN_KEY, CHARACTER_SET_NAME, COLLATION_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (database, table),
            )
            rows = cur.fetchall()
            return {r["COLUMN_NAME"]: r for r in rows}
    finally:
        conn.close()


def indexes(cfg, database: str, table: str) -> dict[str, list[str]]:
    conn = connect(cfg, database)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols,
                       NON_UNIQUE, INDEX_TYPE
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                GROUP BY INDEX_NAME, NON_UNIQUE, INDEX_TYPE
                ORDER BY INDEX_NAME
                """,
                (database, table),
            )
            out = {}
            for name, cols, non_unique, index_type in cur.fetchall():
                out[name] = {
                    "columns": cols,
                    "non_unique": non_unique,
                    "index_type": index_type,
                }
            return out
    finally:
        conn.close()


def migrations(cfg, database: str) -> set[tuple[str, str]]:
    tables = set(list_tables(cfg, database))
    if "django_migrations" not in tables:
        return set()
    conn = connect(cfg, database)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT app, name FROM django_migrations")
            return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def show_create(cfg, database: str, table: str) -> str:
    conn = connect(cfg, database)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE `{database}`.`{table}`")
            return cur.fetchone()[1]
    finally:
        conn.close()


def pick_db(dbs: list[str], preferred: str | None = None) -> str:
    if preferred and preferred in dbs:
        return preferred
    # Prefer names that look like the app DB
    for hint in ("ashish_db_uat", "ashish_db", "ashishjewels", "taranya", "oneashish"):
        for d in dbs:
            if hint in d.lower():
                return d
    if len(dbs) == 1:
        return dbs[0]
    raise SystemExit(f"Could not pick database from: {dbs}")


def main():
    print("Connecting UAT...")
    uat_dbs = list_dbs(UAT)
    print("UAT DBs:", uat_dbs)
    print("Connecting PROD...")
    prod_dbs = list_dbs(PROD)
    print("PROD DBs:", prod_dbs)

    uat_db = pick_db(uat_dbs, "ashish_db_uat")
    # Prefer non-uat name on prod
    prod_candidates = [d for d in prod_dbs if "uat" not in d.lower()] or prod_dbs
    prod_db = pick_db(prod_candidates, "ashish_db")
    print(f"\nComparing UAT.`{uat_db}`  vs  PROD.`{prod_db}`\n")

    uat_tables = set(list_tables(UAT, uat_db))
    prod_tables = set(list_tables(PROD, prod_db))

    missing_tables = sorted(uat_tables - prod_tables)
    extra_tables = sorted(prod_tables - uat_tables)
    common = sorted(uat_tables & prod_tables)

    missing_cols: dict[str, list[str]] = {}
    col_diffs: dict[str, list[str]] = {}
    missing_indexes: dict[str, list[str]] = {}

    for table in common:
        uc = columns(UAT, uat_db, table)
        pc = columns(PROD, prod_db, table)
        miss = sorted(set(uc) - set(pc))
        if miss:
            missing_cols[table] = miss
        for col in sorted(set(uc) & set(pc)):
            if uc[col]["COLUMN_TYPE"] != pc[col]["COLUMN_TYPE"] or uc[col]["IS_NULLABLE"] != pc[col]["IS_NULLABLE"]:
                col_diffs.setdefault(table, []).append(
                    f"{col}: UAT({uc[col]['COLUMN_TYPE']}, null={uc[col]['IS_NULLABLE']}) "
                    f"vs PROD({pc[col]['COLUMN_TYPE']}, null={pc[col]['IS_NULLABLE']})"
                )
        ui = indexes(UAT, uat_db, table)
        pi = indexes(PROD, prod_db, table)
        miss_ix = sorted(set(ui) - set(pi))
        if miss_ix:
            missing_indexes[table] = miss_ix

    uat_migs = migrations(UAT, uat_db)
    prod_migs = migrations(PROD, prod_db)
    missing_migs = sorted(uat_migs - prod_migs)
    extra_migs = sorted(prod_migs - uat_migs)

    report = {
        "uat_db": uat_db,
        "prod_db": prod_db,
        "uat_table_count": len(uat_tables),
        "prod_table_count": len(prod_tables),
        "missing_tables_in_prod": missing_tables,
        "extra_tables_in_prod": extra_tables,
        "missing_columns_in_prod": missing_cols,
        "column_type_diffs": col_diffs,
        "missing_indexes_in_prod": missing_indexes,
        "missing_django_migrations_in_prod": [{"app": a, "name": n} for a, n in missing_migs],
        "extra_django_migrations_in_prod": [{"app": a, "name": n} for a, n in extra_migs],
    }

    out_dir = Path(__file__).resolve().parent / "db_compare_out"
    out_dir.mkdir(exist_ok=True)
    report_path = out_dir / "uat_vs_prod_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Generate CREATE TABLE for missing tables + ALTER for missing columns
    sql_parts = [
        f"-- Generated from UAT `{uat_db}` vs PROD `{prod_db}`",
        "-- REVIEW BEFORE APPLYING TO PRODUCTION",
        "SET NAMES utf8mb4;",
        f"USE `{prod_db}`;",
        "",
    ]

    for table in missing_tables:
        ddl = show_create(UAT, uat_db, table)
        # Rewrite for target DB if needed
        sql_parts.append(f"-- Missing table: {table}")
        sql_parts.append(ddl + ";")
        sql_parts.append("")

    for table, cols in missing_cols.items():
        uc = columns(UAT, uat_db, table)
        sql_parts.append(f"-- Missing columns on {table}")
        for col in cols:
            c = uc[col]
            null_sql = "NULL" if c["IS_NULLABLE"] == "YES" else "NOT NULL"
            default = c["COLUMN_DEFAULT"]
            default_sql = ""
            if default is not None:
                # Keep simple defaults readable
                if str(default).upper() in ("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"):
                    default_sql = f" DEFAULT {default}"
                else:
                    default_sql = f" DEFAULT {repr(default)}"
            extra = f" {c['EXTRA']}" if c["EXTRA"] else ""
            sql_parts.append(
                f"ALTER TABLE `{table}` ADD COLUMN `{col}` {c['COLUMN_TYPE']} {null_sql}{default_sql}{extra};"
            )
        sql_parts.append("")

    sql_path = out_dir / "prod_missing_objects.sql"
    sql_path.write_text("\n".join(sql_parts), encoding="utf-8")

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"UAT tables:  {len(uat_tables)}")
    print(f"PROD tables: {len(prod_tables)}")
    print(f"Missing tables in PROD ({len(missing_tables)}):")
    for t in missing_tables:
        print(f"  - {t}")
    print(f"\nMissing columns in PROD ({sum(len(v) for v in missing_cols.values())} across {len(missing_cols)} tables):")
    for t, cols in missing_cols.items():
        print(f"  - {t}: {', '.join(cols)}")
    print(f"\nColumn type/null diffs ({sum(len(v) for v in col_diffs.values())}):")
    for t, diffs in list(col_diffs.items())[:30]:
        for d in diffs:
            print(f"  - {t}.{d}")
    if sum(len(v) for v in col_diffs.values()) > 30:
        print("  ... (see JSON report for full list)")
    print(f"\nMissing indexes in PROD ({sum(len(v) for v in missing_indexes.values())}):")
    for t, ixs in list(missing_indexes.items())[:40]:
        print(f"  - {t}: {', '.join(ixs)}")
    print(f"\nMissing django_migrations in PROD: {len(missing_migs)}")
    for a, n in missing_migs[:50]:
        print(f"  - {a}.{n}")
    if len(missing_migs) > 50:
        print(f"  ... +{len(missing_migs) - 50} more")
    print(f"\nWrote:\n  {report_path}\n  {sql_path}")


if __name__ == "__main__":
    main()
