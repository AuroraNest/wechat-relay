SET @awr_add_messages_reply_capable = IF(
  EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'messages' AND column_name = 'reply_capable'),
  'DO 0',
  'ALTER TABLE messages ADD COLUMN reply_capable TINYINT(1) NOT NULL DEFAULT 0 AFTER wechat_user_id'
);
PREPARE awr_stmt FROM @awr_add_messages_reply_capable;
EXECUTE awr_stmt;
DEALLOCATE PREPARE awr_stmt;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (3, 'message-reply-capability', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
