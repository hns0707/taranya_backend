-- Day Book grouping lookup (manual entry types)
-- Safe to re-run (ON DUPLICATE KEY / NOT EXISTS)

INSERT INTO lookups (code, name, description, is_active, system_created_at, system_updated_at)
SELECT 'DAY_BOOK_GROUP', 'Day Book Group', 'Manual entry / ledger grouping for Daily Book', 1, NOW(6), NOW(6)
WHERE NOT EXISTS (SELECT 1 FROM lookups WHERE code = 'DAY_BOOK_GROUP');

SET @lookup_id = (SELECT id FROM lookups WHERE code = 'DAY_BOOK_GROUP');

INSERT INTO lookup_values (lookup_id, code, label, is_active, sort_order, system_created_at, system_updated_at)
SELECT @lookup_id, v.code, v.label, 1, v.sort_order, NOW(6), NOW(6)
FROM (
  SELECT 'ADVANCE' AS code, 'Advance' AS label, 10 AS sort_order
  UNION ALL SELECT 'BORROWING', 'Borrowing', 20
  UNION ALL SELECT 'UDHAR', 'Udhar', 30
  UNION ALL SELECT 'LENDING', 'Lending', 40
  UNION ALL SELECT 'MISC', 'Misc.', 50
  UNION ALL SELECT 'HUF', 'HUF', 60
  UNION ALL SELECT 'HUF_I', 'HUF I', 70
) AS v
WHERE @lookup_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM lookup_values lv
    WHERE lv.lookup_id = @lookup_id AND lv.code = v.code
  );
