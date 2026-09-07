CREATE TABLE IF NOT EXISTS relay_policies (
  pair_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  schedule_enabled TINYINT(1) NOT NULL DEFAULT 0,
  weekdays_mask INT NOT NULL DEFAULT 31,
  start_minutes INT NOT NULL DEFAULT 570,
  end_minutes INT NOT NULL DEFAULT 1080,
  updated_at BIGINT NOT NULL,
  FOREIGN KEY(pair_id) REFERENCES pairings(pair_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (5, 'relay-policy', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
