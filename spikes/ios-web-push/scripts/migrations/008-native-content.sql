CREATE TABLE IF NOT EXISTS native_contents (
  message_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
  envelope_json LONGTEXT NOT NULL,
  assets_json TEXT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES messages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Declarations survive ciphertext expiry so clients can distinguish expired from unknown.
CREATE TABLE IF NOT EXISTS native_assets (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  message_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  byte_length BIGINT NOT NULL,
  metadata_json TEXT NOT NULL,
  expires_at BIGINT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES native_contents(message_id),
  KEY native_assets_expiry(expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS native_asset_payloads (
  asset_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  envelope_json LONGTEXT NOT NULL,
  envelope_hash BINARY(32) NOT NULL,
  FOREIGN KEY(asset_id) REFERENCES native_assets(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (8, 'native-content', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
