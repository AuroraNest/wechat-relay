ALTER TABLE replies
  MODIFY target_message_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
  ADD COLUMN contact_snapshot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL AFTER target_message_id,
  ADD CONSTRAINT replies_exactly_one_target CHECK (
    (target_message_id IS NOT NULL) <> (contact_snapshot_id IS NOT NULL)
  );

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (7, 'contact-send-replies', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
