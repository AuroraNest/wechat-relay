import mysql, {
  type Pool,
  type PoolConnection,
  type ResultSetHeader,
  type RowDataPacket,
} from "mysql2/promise";

const required = (name: string): string => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
};

export const pool: Pool = mysql.createPool({
  host: required("MYSQL_HOST"),
  port: Number(process.env.MYSQL_PORT ?? 3306),
  database: required("MYSQL_DATABASE"),
  user: required("MYSQL_USER"),
  password: required("MYSQL_PASSWORD"),
  waitForConnections: true,
  connectionLimit: 10,
  charset: "utf8mb4",
  supportBigNumbers: true,
  bigNumberStrings: true,
});

const requiredSchemaVersion = 4;
const requiredColumns: Record<string, readonly string[]> = {
  schema_migrations: ["version", "name", "applied_at"],
  pairings: ["pair_id", "secret_hash", "expires_at", "consumed_at"],
  devices: ["device_id", "pair_id", "public_key", "paired_at"],
  request_nonces: ["device_id", "nonce", "seen_at"],
  messages: ["id", "device_id", "wechat_user_id", "reply_capable", "conversation_send_capable", "seq", "created_at", "body_hash", "envelope_json", "assets_json", "received_at"],
  push_outbox: ["message_id", "payload", "queued_at", "attempts", "sent_at"],
  replies: ["id", "pair_id", "target_message_id", "device_id", "wechat_user_id", "created_at", "envelope_json", "status", "status_at", "queued_at"],
  browser_sessions: ["session_id", "ack_token_hash", "subscription_envelope", "pair_id", "created_at", "updated_at", "invalidated_at"],
};

export async function verifySchema(): Promise<void> {
  const connection = await pool.getConnection();
  try {
    const [versionRows] = await connection.query<(RowDataPacket & { version: number | null })[]>(
      "SELECT MAX(version) AS version FROM schema_migrations",
    );
    if (Number(versionRows[0]?.version ?? 0) !== requiredSchemaVersion)
      throw new Error("SCHEMA_VERSION_MISMATCH");

    const [rows] = await connection.query<(RowDataPacket & {
      table_name: string;
      column_name: string;
      is_nullable: string;
      column_default: string | number | null;
    })[]>(
      "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default FROM information_schema.columns WHERE TABLE_SCHEMA = DATABASE()",
    );
    const present = new Set(rows.map((row) => `${row.table_name}.${row.column_name}`));
    for (const [table, columns] of Object.entries(requiredColumns))
      for (const column of columns)
        if (!present.has(`${table}.${column}`)) throw new Error("SCHEMA_COLUMN_MISSING");

    for (const table of ["messages", "replies"])
      for (const row of rows.filter((candidate) => candidate.table_name === table && candidate.column_name === "wechat_user_id"))
        if (row.is_nullable !== "NO" || Number(row.column_default) !== 0)
          throw new Error("SCHEMA_PROFILE_COLUMN_INVALID");
    const replyCapable = rows.find((row) => row.table_name === "messages" && row.column_name === "reply_capable");
    if (!replyCapable || replyCapable.is_nullable !== "NO" || Number(replyCapable.column_default) !== 0)
      throw new Error("SCHEMA_REPLY_CAPABILITY_INVALID");
    const conversationSendCapable = rows.find((row) => row.table_name === "messages" && row.column_name === "conversation_send_capable");
    if (!conversationSendCapable || conversationSendCapable.is_nullable !== "NO" || Number(conversationSendCapable.column_default) !== 0)
      throw new Error("SCHEMA_CONVERSATION_SEND_CAPABILITY_INVALID");
  } finally {
    connection.release();
  }
}

export async function transaction<T>(work: (connection: PoolConnection) => Promise<T>): Promise<T> {
  const connection = await pool.getConnection();
  try {
    await connection.beginTransaction();
    const result = await work(connection);
    await connection.commit();
    return result;
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

export async function close(): Promise<void> { await pool.end(); }
export type MysqlResult = ResultSetHeader;
