CREATE TABLE IF NOT EXISTS contacts_snapshots (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  device_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  wechat_user_id SMALLINT NOT NULL,
  captured_at BIGINT NOT NULL,
  body_hash BINARY(32) NOT NULL,
  envelope_json LONGTEXT NOT NULL,
  received_at BIGINT NOT NULL,
  UNIQUE KEY contacts_snapshots_device_profile(device_id, wechat_user_id),
  FOREIGN KEY(device_id) REFERENCES devices(device_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (6, 'contacts-snapshots', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
