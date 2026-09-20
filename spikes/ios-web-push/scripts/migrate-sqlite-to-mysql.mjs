import { createHash } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import { close, pool, verifySchema } from "../dist/mysql.js";

const sourcePath = process.argv[2];
if (!sourcePath) throw new Error("SQLite path is required");

const tables = [
  {
    name: "pairings",
    columns: ["pair_id", "secret_hash", "expires_at", "consumed_at"],
    orderBy: ["pair_id"],
  },
  {
    name: "devices",
    columns: ["device_id", "pair_id", "public_key", "paired_at"],
    orderBy: ["device_id"],
  },
  {
    name: "request_nonces",
    columns: ["device_id", "nonce", "seen_at"],
    orderBy: ["device_id", "nonce"],
  },
  {
    name: "messages",
    columns: [
      "id",
      "device_id",
      "seq",
      "created_at",
      "body_hash",
      "envelope_json",
      "assets_json",
      "received_at",
    ],
    orderBy: ["id"],
  },
  {
    name: "push_outbox",
    columns: ["message_id", "payload", "queued_at", "attempts", "sent_at"],
    orderBy: ["message_id"],
  },
  {
    name: "replies",
    columns: [
      "id",
      "pair_id",
      "target_message_id",
      "device_id",
      "created_at",
      "envelope_json",
      "status",
      "status_at",
      "queued_at",
    ],
    orderBy: ["id"],
  },
];

const contactsSnapshotsTable = {
  name: "contacts_snapshots",
  columns: [
    "id",
    "device_id",
    "wechat_user_id",
    "captured_at",
    "body_hash",
    "envelope_json",
    "received_at",
  ],
  orderBy: ["id"],
};

function quoted(values) {
  return values.map((value) => `\`${value}\``).join(", ");
}

function normalize(value) {
  if (value === null) return "N";
  if (value instanceof Uint8Array)
    return `B:${Buffer.from(
      value.buffer,
      value.byteOffset,
      value.byteLength,
    ).toString("base64")}`;
  return `V:${String(value)}`;
}

function digest(rows, columns) {
  const hash = createHash("sha256");
  for (const row of rows) {
    for (const column of columns)
      hash.update(`${column}=${normalize(row[column])}\0`);
    hash.update("\n");
  }
  return hash.digest("hex");
}

async function verify(connection, sourceRows) {
  const counts = new Map();
  for (const table of tables) {
    const [rows] = await connection.query(
      `SELECT ${quoted(table.columns)} FROM \`${table.name}\` ORDER BY ${quoted(table.orderBy)}`,
    );
    const source = sourceRows.get(table.name);
    if (!source || rows.length !== source.length)
      throw new Error(`count mismatch for ${table.name}`);
    if (digest(rows, table.columns) !== digest(source, table.columns))
      throw new Error(`content mismatch for ${table.name}`);
    counts.set(table.name, rows.length);
  }
  return counts;
}

const sqlite = new DatabaseSync(sourcePath, { readOnly: true });
if (
  sqlite
    .prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'contacts_snapshots'")
    .get()
)
  tables.push(contactsSnapshotsTable);
const sourceRows = new Map();
let sqliteTransactionStarted = false;
let mysqlTransactionStarted = false;
let connection;

try {
  sqlite.exec("BEGIN");
  sqliteTransactionStarted = true;
  for (const table of tables) {
    const rows = sqlite
      .prepare(
        `SELECT ${quoted(table.columns)} FROM ${table.name} ORDER BY ${quoted(table.orderBy)}`,
      )
      .all();
    sourceRows.set(table.name, rows);
  }

  await verifySchema();
  connection = await pool.getConnection();
  await connection.beginTransaction();
  mysqlTransactionStarted = true;

  for (const table of tables) {
    const [[row]] = await connection.query(
      `SELECT COUNT(*) AS count FROM \`${table.name}\``,
    );
    if (Number(row.count) !== 0)
      throw new Error(`target table ${table.name} is not empty`);
  }

  for (const table of tables) {
    const rows = sourceRows.get(table.name);
    const placeholders = table.columns.map(() => "?").join(", ");
    for (const row of rows)
      await connection.query(
        `INSERT INTO \`${table.name}\` (${quoted(table.columns)}) VALUES (${placeholders})`,
        table.columns.map((column) => row[column]),
      );
  }

  await verify(connection, sourceRows);
  await connection.commit();
  mysqlTransactionStarted = false;
  const counts = await verify(connection, sourceRows);
  sqlite.exec("COMMIT");
  sqliteTransactionStarted = false;

  for (const table of tables)
    console.log(`${table.name}: ${counts.get(table.name)}`);
} catch (error) {
  if (mysqlTransactionStarted && connection) await connection.rollback();
  if (sqliteTransactionStarted) sqlite.exec("ROLLBACK");
  throw error;
} finally {
  connection?.release();
  sqlite.close();
  await close();
}
