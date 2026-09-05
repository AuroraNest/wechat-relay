CREATE TABLE IF NOT EXISTS browser_sessions (
  session_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  ack_token_hash BINARY(32) NOT NULL UNIQUE,
  subscription_envelope LONGTEXT NOT NULL,
  pair_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL UNIQUE,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  invalidated_at BIGINT NULL,
  FOREIGN KEY(pair_id) REFERENCES pairings(pair_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

SET @awr_add_messages_profile = IF(
  EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'messages' AND column_name = 'wechat_user_id'),
  'DO 0',
  'ALTER TABLE messages ADD COLUMN wechat_user_id SMALLINT NOT NULL DEFAULT 0 AFTER device_id'
);
PREPARE awr_stmt FROM @awr_add_messages_profile;
EXECUTE awr_stmt;
DEALLOCATE PREPARE awr_stmt;

SET @awr_add_replies_profile = IF(
  EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'replies' AND column_name = 'wechat_user_id'),
  'DO 0',
  'ALTER TABLE replies ADD COLUMN wechat_user_id SMALLINT NOT NULL DEFAULT 0 AFTER device_id'
);
PREPARE awr_stmt FROM @awr_add_replies_profile;
EXECUTE awr_stmt;
DEALLOCATE PREPARE awr_stmt;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (2, 'multi-pair-browser-sessions', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
