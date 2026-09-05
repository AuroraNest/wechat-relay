CREATE TABLE IF NOT EXISTS schema_migrations (
  version INT PRIMARY KEY,
  name VARCHAR(191) NOT NULL,
  applied_at BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS pairings (
  pair_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
  secret_hash BLOB NOT NULL,
  expires_at BIGINT NOT NULL,
  consumed_at BIGINT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS devices (
  device_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
  pair_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL UNIQUE,
  public_key BLOB NOT NULL,
  paired_at BIGINT NOT NULL,
  FOREIGN KEY(pair_id) REFERENCES pairings(pair_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS request_nonces (
  device_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  nonce VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  seen_at BIGINT NOT NULL,
  PRIMARY KEY(device_id, nonce),
  KEY request_nonces_seen_at(seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS messages (
  id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
  device_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  seq BIGINT NOT NULL,
  created_at BIGINT NOT NULL,
  body_hash BLOB NOT NULL,
  envelope_json LONGTEXT NOT NULL,
  assets_json LONGTEXT,
  received_at BIGINT NOT NULL,
  UNIQUE KEY messages_device_seq(device_id, seq),
  FOREIGN KEY(device_id) REFERENCES devices(device_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS push_outbox (
  message_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
  payload LONGTEXT NOT NULL,
  queued_at BIGINT NOT NULL,
  attempts BIGINT NOT NULL DEFAULT 0,
  sent_at BIGINT,
  FOREIGN KEY(message_id) REFERENCES messages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS replies (
  id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
  pair_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  target_message_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  device_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  created_at BIGINT NOT NULL,
  envelope_json LONGTEXT NOT NULL,
  status VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  status_at BIGINT NOT NULL,
  queued_at BIGINT NOT NULL,
  FOREIGN KEY(pair_id) REFERENCES pairings(pair_id),
  FOREIGN KEY(target_message_id) REFERENCES messages(id),
  FOREIGN KEY(device_id) REFERENCES devices(device_id),
  KEY replies_device_status_queued_at(device_id, status, queued_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (1, 'baseline', UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000)
ON DUPLICATE KEY UPDATE name = VALUES(name);
