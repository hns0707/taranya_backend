-- Generated from UAT `ashish_db_uat` vs PROD `ashish_db`
-- REVIEW BEFORE APPLYING TO PRODUCTION
SET NAMES utf8mb4;
USE `ashish_db`;

-- Missing table: catalogue_quote_change_logs
CREATE TABLE `catalogue_quote_change_logs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `action` varchar(32) NOT NULL,
  `summary` varchar(512) NOT NULL DEFAULT '',
  `payload` json NOT NULL DEFAULT (json_object()),
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `quote_id` bigint NOT NULL,
  `actor_id` bigint DEFAULT NULL,
  `line_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `catalogue_quote_change_logs_quote_id_idx` (`quote_id`),
  KEY `catalogue_quote_change_logs_actor_id_idx` (`actor_id`),
  KEY `catalogue_quote_change_logs_line_id_idx` (`line_id`),
  KEY `catalogue_quote_change_logs_created_at_idx` (`created_at`),
  CONSTRAINT `catalogue_quote_change_logs_actor_id_fk` FOREIGN KEY (`actor_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `catalogue_quote_change_logs_line_id_fk` FOREIGN KEY (`line_id`) REFERENCES `catalogue_quote_lines` (`id`) ON DELETE SET NULL,
  CONSTRAINT `catalogue_quote_change_logs_quote_id_fk` FOREIGN KEY (`quote_id`) REFERENCES `catalogue_quotes` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=75 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing table: catalogue_quote_contributors
CREATE TABLE `catalogue_quote_contributors` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `system_created_at` datetime(6) NOT NULL,
  `system_updated_at` datetime(6) NOT NULL,
  `share_percent` decimal(5,2) NOT NULL DEFAULT '100.00',
  `role` varchar(16) NOT NULL DEFAULT 'assistant',
  `quote_id` bigint NOT NULL,
  `admin_user_id` bigint NOT NULL,
  `created_by_id` bigint DEFAULT NULL,
  `updated_by_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `catalogue_quote_contributors_quote_admin_uniq` (`quote_id`,`admin_user_id`),
  KEY `catalogue_quote_contributors_quote_id_idx` (`quote_id`),
  KEY `catalogue_quote_contributors_admin_user_id_idx` (`admin_user_id`),
  KEY `catalogue_quote_contributors_created_by_id_idx` (`created_by_id`),
  KEY `catalogue_quote_contributors_updated_by_id_idx` (`updated_by_id`),
  CONSTRAINT `catalogue_quote_contributors_admin_user_id_fk` FOREIGN KEY (`admin_user_id`) REFERENCES `admin_users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `catalogue_quote_contributors_created_by_id_fk` FOREIGN KEY (`created_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `catalogue_quote_contributors_quote_id_fk` FOREIGN KEY (`quote_id`) REFERENCES `catalogue_quotes` (`id`) ON DELETE CASCADE,
  CONSTRAINT `catalogue_quote_contributors_updated_by_id_fk` FOREIGN KEY (`updated_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=13 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing table: catalogue_quote_line_removal_requests
CREATE TABLE `catalogue_quote_line_removal_requests` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `system_created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `system_updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  `status` varchar(16) NOT NULL DEFAULT 'pending',
  `reviewed_at` datetime(6) DEFAULT NULL,
  `request_notes` longtext NOT NULL,
  `review_notes` longtext NOT NULL,
  `created_by_id` bigint DEFAULT NULL,
  `updated_by_id` bigint DEFAULT NULL,
  `quote_id` bigint NOT NULL,
  `line_id` bigint NOT NULL,
  `requested_by_id` bigint NOT NULL,
  `owner_sales_user_id` bigint NOT NULL,
  `reviewed_by_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `catalogue_quote_line_removal_requests_line_fk` (`line_id`),
  KEY `catalogue_quote_line_removal_requests_requested_by_fk` (`requested_by_id`),
  KEY `catalogue_quote_line_removal_requests_reviewed_by_fk` (`reviewed_by_id`),
  KEY `catalogue_quote_line_removal_requests_created_by_fk` (`created_by_id`),
  KEY `catalogue_quote_line_removal_requests_updated_by_fk` (`updated_by_id`),
  KEY `idx_cq_line_removal_quote` (`quote_id`),
  KEY `idx_cq_line_removal_owner_status` (`owner_sales_user_id`,`status`),
  CONSTRAINT `catalogue_quote_line_removal_requests_created_by_fk` FOREIGN KEY (`created_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `catalogue_quote_line_removal_requests_line_fk` FOREIGN KEY (`line_id`) REFERENCES `catalogue_quote_lines` (`id`) ON DELETE CASCADE,
  CONSTRAINT `catalogue_quote_line_removal_requests_owner_fk` FOREIGN KEY (`owner_sales_user_id`) REFERENCES `admin_users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `catalogue_quote_line_removal_requests_quote_fk` FOREIGN KEY (`quote_id`) REFERENCES `catalogue_quotes` (`id`) ON DELETE CASCADE,
  CONSTRAINT `catalogue_quote_line_removal_requests_requested_by_fk` FOREIGN KEY (`requested_by_id`) REFERENCES `admin_users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `catalogue_quote_line_removal_requests_reviewed_by_fk` FOREIGN KEY (`reviewed_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `catalogue_quote_line_removal_requests_updated_by_fk` FOREIGN KEY (`updated_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing table: catalogue_quote_visits
CREATE TABLE `catalogue_quote_visits` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `system_created_at` datetime(6) NOT NULL,
  `system_updated_at` datetime(6) NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'open',
  `closed_at` datetime(6) DEFAULT NULL,
  `customer_id` bigint NOT NULL,
  `quote_id` bigint NOT NULL,
  `primary_sales_user_id` bigint DEFAULT NULL,
  `created_by_id` bigint DEFAULT NULL,
  `updated_by_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `catalogue_quote_visits_quote_id_uniq` (`quote_id`),
  KEY `catalogue_quote_visits_customer_id_idx` (`customer_id`),
  KEY `catalogue_quote_visits_primary_sales_user_id_idx` (`primary_sales_user_id`),
  KEY `catalogue_quote_visits_created_by_id_idx` (`created_by_id`),
  KEY `catalogue_quote_visits_updated_by_id_idx` (`updated_by_id`),
  CONSTRAINT `catalogue_quote_visits_created_by_id_fk` FOREIGN KEY (`created_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `catalogue_quote_visits_customer_id_fk` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`),
  CONSTRAINT `catalogue_quote_visits_primary_sales_user_id_fk` FOREIGN KEY (`primary_sales_user_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `catalogue_quote_visits_quote_id_fk` FOREIGN KEY (`quote_id`) REFERENCES `catalogue_quotes` (`id`) ON DELETE CASCADE,
  CONSTRAINT `catalogue_quote_visits_updated_by_id_fk` FOREIGN KEY (`updated_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing table: day_book_days
CREATE TABLE `day_book_days` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `system_created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `system_updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  `book_date` date NOT NULL,
  `opening_balance` decimal(14,2) DEFAULT NULL COMMENT 'Manual opening cash. NULL = use previous day closing.',
  `is_opening_manual` tinyint(1) NOT NULL DEFAULT '0',
  `closing_balance` decimal(14,2) DEFAULT NULL COMMENT 'Cached closing cash; speeds up next-day opening.',
  `created_by_id` bigint DEFAULT NULL,
  `updated_by_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `day_book_days_book_date_uniq` (`book_date`),
  KEY `day_book_days_book_date_idx` (`book_date`),
  KEY `day_book_days_created_by_id_idx` (`created_by_id`),
  KEY `day_book_days_updated_by_id_idx` (`updated_by_id`),
  CONSTRAINT `day_book_days_created_by_id_fk` FOREIGN KEY (`created_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `day_book_days_updated_by_id_fk` FOREIGN KEY (`updated_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=545 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing table: day_book_manual_entries
CREATE TABLE `day_book_manual_entries` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `system_created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `system_updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  `entry_date` date NOT NULL,
  `direction` varchar(3) NOT NULL COMMENT 'IN = Money In, OUT = Money Out',
  `amount` decimal(14,2) NOT NULL,
  `transaction_mode` varchar(32) NOT NULL COMMENT 'EXPENSE | REPAIR_RECEIPT | BORROWINGS | MONEY_LENDING | OTHER',
  `narration` longtext NOT NULL,
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',
  `created_by_id` bigint DEFAULT NULL,
  `updated_by_id` bigint DEFAULT NULL,
  `payment_mode` varchar(32) NOT NULL DEFAULT 'CASH',
  PRIMARY KEY (`id`),
  KEY `day_book_manual_entries_entry_date_idx` (`entry_date`),
  KEY `day_book_manual_entries_is_deleted_idx` (`is_deleted`),
  KEY `day_book_manual_entries_created_by_id_idx` (`created_by_id`),
  KEY `day_book_manual_entries_updated_by_id_idx` (`updated_by_id`),
  CONSTRAINT `day_book_manual_entries_created_by_id_fk` FOREIGN KEY (`created_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `day_book_manual_entries_updated_by_id_fk` FOREIGN KEY (`updated_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=61 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing table: upi_mandate_executions
CREATE TABLE `upi_mandate_executions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `system_created_at` datetime(6) NOT NULL,
  `system_updated_at` datetime(6) NOT NULL,
  `merchant_tran_id` varchar(100) NOT NULL,
  `mandate_seq_no` varchar(50) DEFAULT NULL,
  `retry_count` smallint unsigned NOT NULL DEFAULT '0',
  `amount` decimal(10,2) NOT NULL,
  `txn_status` varchar(20) NOT NULL DEFAULT 'PENDING',
  `bank_rrn` varchar(100) DEFAULT NULL,
  `gateway_response` json DEFAULT NULL,
  `executed_at` datetime(6) DEFAULT NULL,
  `upi_mandate_id` bigint NOT NULL,
  `scheme_instalment_id` bigint NOT NULL,
  `payment_id` bigint DEFAULT NULL,
  `created_by_id` bigint DEFAULT NULL,
  `updated_by_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `merchant_tran_id` (`merchant_tran_id`),
  UNIQUE KEY `uniq_upi_execution_per_instalment` (`scheme_instalment_id`),
  KEY `upi_mandate_executions_txn_status_idx` (`txn_status`),
  KEY `upi_mandate_executions_upi_mandate_id_fk` (`upi_mandate_id`),
  KEY `upi_mandate_executions_payment_id_fk` (`payment_id`),
  KEY `upi_mandate_executions_created_by_id_fk` (`created_by_id`),
  KEY `upi_mandate_executions_updated_by_id_fk` (`updated_by_id`),
  CONSTRAINT `upi_mandate_executions_created_by_id_fk` FOREIGN KEY (`created_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `upi_mandate_executions_payment_id_fk` FOREIGN KEY (`payment_id`) REFERENCES `payments` (`id`) ON DELETE SET NULL,
  CONSTRAINT `upi_mandate_executions_scheme_instalment_id_fk` FOREIGN KEY (`scheme_instalment_id`) REFERENCES `scheme_instalments` (`id`),
  CONSTRAINT `upi_mandate_executions_updated_by_id_fk` FOREIGN KEY (`updated_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `upi_mandate_executions_upi_mandate_id_fk` FOREIGN KEY (`upi_mandate_id`) REFERENCES `upi_mandates` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing table: upi_mandates
CREATE TABLE `upi_mandates` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `system_created_at` datetime(6) NOT NULL,
  `system_updated_at` datetime(6) NOT NULL,
  `merchant_tran_id` varchar(100) NOT NULL,
  `umn` varchar(100) DEFAULT NULL,
  `payer_vpa` varchar(150) DEFAULT NULL,
  `payer_name` varchar(150) DEFAULT NULL,
  `payer_mobile` varchar(15) DEFAULT NULL,
  `amount` decimal(10,2) NOT NULL,
  `frequency` varchar(30) NOT NULL DEFAULT 'MONTHLY',
  `start_date` date DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `bank_rrn` varchar(100) DEFAULT NULL,
  `status` varchar(20) NOT NULL DEFAULT 'PENDING',
  `mandate_created_at` datetime(6) DEFAULT NULL,
  `mandate_approved_at` datetime(6) DEFAULT NULL,
  `revoked_at` datetime(6) DEFAULT NULL,
  `customer_scheme_id` bigint NOT NULL,
  `created_by_id` bigint DEFAULT NULL,
  `updated_by_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `merchant_tran_id` (`merchant_tran_id`),
  KEY `upi_mandates_umn_idx` (`umn`),
  KEY `upi_mandates_status_idx` (`status`),
  KEY `upi_mandates_custome_idx` (`customer_scheme_id`,`status`),
  KEY `upi_mandates_customer_scheme_id_fk` (`customer_scheme_id`),
  KEY `upi_mandates_created_by_id_fk` (`created_by_id`),
  KEY `upi_mandates_updated_by_id_fk` (`updated_by_id`),
  CONSTRAINT `upi_mandates_created_by_id_fk` FOREIGN KEY (`created_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `upi_mandates_customer_scheme_id_fk` FOREIGN KEY (`customer_scheme_id`) REFERENCES `customer_schemes` (`id`) ON DELETE CASCADE,
  CONSTRAINT `upi_mandates_updated_by_id_fk` FOREIGN KEY (`updated_by_id`) REFERENCES `admin_users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Missing columns on catalogue_quote_lines
ALTER TABLE `catalogue_quote_lines` ADD COLUMN `added_by_id` bigint NULL;
ALTER TABLE `catalogue_quote_lines` ADD COLUMN `is_removed` tinyint(1) NOT NULL DEFAULT '0';
ALTER TABLE `catalogue_quote_lines` ADD COLUMN `pricing_meta` json NOT NULL DEFAULT 'json_object()' DEFAULT_GENERATED;
ALTER TABLE `catalogue_quote_lines` ADD COLUMN `removed_at` datetime(6) NULL;
ALTER TABLE `catalogue_quote_lines` ADD COLUMN `removed_by_id` bigint NULL;

-- Missing columns on catalogue_quote_payments
ALTER TABLE `catalogue_quote_payments` ADD COLUMN `notes` longtext NOT NULL;
ALTER TABLE `catalogue_quote_payments` ADD COLUMN `reference_no` varchar(128) NOT NULL DEFAULT '';

-- Missing columns on catalogue_quotes
ALTER TABLE `catalogue_quotes` ADD COLUMN `cart_pricing_meta` json NOT NULL DEFAULT 'json_object()' DEFAULT_GENERATED;
ALTER TABLE `catalogue_quotes` ADD COLUMN `sales_credit_snapshot` json NOT NULL DEFAULT 'json_array()' DEFAULT_GENERATED;
ALTER TABLE `catalogue_quotes` ADD COLUMN `version` int unsigned NOT NULL DEFAULT '1';

-- Missing columns on grn_batches
ALTER TABLE `grn_batches` ADD COLUMN `stone_rate_basis` varchar(16) NOT NULL DEFAULT 'per_gram';
ALTER TABLE `grn_batches` ADD COLUMN `stone_wt_unit` varchar(16) NOT NULL DEFAULT 'grams';

-- Missing columns on grn_lots
ALTER TABLE `grn_lots` ADD COLUMN `stone_rate_basis` varchar(16) NOT NULL DEFAULT 'per_gram';
ALTER TABLE `grn_lots` ADD COLUMN `stone_wt_unit` varchar(16) NOT NULL DEFAULT 'grams';

-- Missing columns on pattern_code_registry
ALTER TABLE `pattern_code_registry` ADD COLUMN `description` varchar(255) NOT NULL DEFAULT '';

-- Missing columns on payments
ALTER TABLE `payments` ADD COLUMN `payment_provider` varchar(20) NOT NULL DEFAULT 'EASEBUZZ';
ALTER TABLE `payments` ADD COLUMN `upi_execution_id` bigint NULL;

-- Missing columns on product_operation_charges
ALTER TABLE `product_operation_charges` ADD COLUMN `DESCRIPTION` text NULL;

-- Missing columns on purchase_orders
ALTER TABLE `purchase_orders` ADD COLUMN `status` varchar(32) NOT NULL DEFAULT 'Draft';
