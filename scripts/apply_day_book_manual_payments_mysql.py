"""
Create day_book_manual_payments + backfill from existing payment_mode (idempotent).

Usage:
  python scripts/apply_day_book_manual_payments_mysql.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

DDL = """
CREATE TABLE IF NOT EXISTS day_book_manual_payments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  system_created_at DATETIME(6) NOT NULL,
  system_updated_at DATETIME(6) NOT NULL,
  payment_mode VARCHAR(32) NOT NULL,
  amount DECIMAL(14,2) NOT NULL,
  sort_order SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  created_by_id BIGINT NULL,
  updated_by_id BIGINT NULL,
  entry_id BIGINT NOT NULL,
  KEY day_book_manual_payments_entry_id (entry_id),
  CONSTRAINT day_book_manual_payments_entry_fk
    FOREIGN KEY (entry_id) REFERENCES day_book_manual_entries (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

BACKFILL = """
INSERT INTO day_book_manual_payments
  (system_created_at, system_updated_at, payment_mode, amount, sort_order, entry_id, created_by_id, updated_by_id)
SELECT
  NOW(6), NOW(6),
  COALESCE(NULLIF(TRIM(e.payment_mode), ''), 'CASH'),
  e.amount,
  0,
  e.id,
  e.created_by_id,
  e.updated_by_id
FROM day_book_manual_entries e
WHERE e.is_deleted = 0
  AND NOT EXISTS (
    SELECT 1 FROM day_book_manual_payments p WHERE p.entry_id = e.id
  );
"""


def main() -> None:
    conn = pymysql.connect(
        host=os.environ['DB_HOST'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        database=os.environ['DB_NAME'],
        port=int(os.environ.get('DB_PORT', 3306)),
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            print('OK: day_book_manual_payments ensured')
            cur.execute(BACKFILL)
            print(f'OK: backfilled {cur.rowcount} payment row(s) from legacy payment_mode')
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'FAILED: {exc}', file=sys.stderr)
        raise SystemExit(1)
