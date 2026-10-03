CREATE TABLE IF NOT EXISTS native_asset_slots (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  message_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  ordinal TINYINT UNSIGNED NOT NULL,
  kind VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  role VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  derived_from CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  expires_at BIGINT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES native_contents(message_id),
  UNIQUE KEY native_asset_slots_order(message_id, ordinal),
  KEY native_asset_slots_expiry(expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Reserve legacy IDs in the same PK namespace to reject cross-version concurrent reuse.
-- Legacy API order still comes from native_contents.assets_json.
INSERT INTO native_asset_slots(id, message_id, ordinal, kind, role, derived_from, expires_at)
SELECT id, message_id, ROW_NUMBER() OVER (PARTITION BY message_id ORDER BY id) - 1,
  JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.kind')),
  JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.role')),
  JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.derivedFrom')), expires_at
FROM native_assets
WHERE NOT EXISTS (SELECT 1 FROM native_asset_slots WHERE native_asset_slots.id = native_assets.id);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (9, 'native-asset-slots', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
