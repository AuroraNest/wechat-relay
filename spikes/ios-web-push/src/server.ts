import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import { EventEmitter } from "node:events";
import {
  appendFile,
  mkdir,
  readFile,
  readdir,
  rename,
  stat,
  writeFile,
} from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import {
  createCipheriv,
  createDecipheriv,
  createHash,
  createPublicKey,
  randomBytes,
  randomUUID,
  timingSafeEqual,
  verify,
} from "node:crypto";
import type {
  Pool,
  PoolConnection,
  ResultSetHeader,
  RowDataPacket,
} from "mysql2/promise";
import webpush, { type PushSubscription } from "web-push";
import { createClient, type RedisClientType } from "redis";
import { pool, transaction, verifySchema } from "./mysql.js";
import { classifyRequestError } from "./errors.js";
import {
  DEFAULT_RELAY_POLICY,
  relayActive,
  relayPolicyJson,
  validateRelayPolicy,
  type RelayPolicy,
} from "./relay-policy.js";
import {
  buildDeclarativePayload,
  validateTestPushRequest,
  type PreviewEnvelope,
} from "./protocol.js";
import {
  ApnsSender,
  loadApnsConfig,
  normalizeDeviceToken,
  validateApnsEnvironment,
  type ApnsTransport,
  type NativePushDestination,
} from "./apns.js";

const port = Number(process.env.PORT ?? 8080);
const publicOrigin = required("PUBLIC_ORIGIN").replace(/\/$/, "");
const publicDir = process.env.PUBLIC_DIR ?? join(process.cwd(), "public");
const dataDir = process.env.DATA_DIR ?? join(process.cwd(), "data");
const testTokens = [
  required("TEST_TOKEN"),
  ...(process.env.TEST_TOKENS ?? "").split(/[\s,]+/).filter(Boolean),
];
const storageKey = Buffer.from(required("STORAGE_KEY"), "base64url");
const vapidPublicKey = required("VAPID_PUBLIC_KEY");
const vapidPrivateKey = required("VAPID_PRIVATE_KEY");
const vapidSubject =
  process.env.VAPID_SUBJECT ?? "mailto:relay@example.com";
const buildVersion = process.env.BUILD_VERSION ?? "dev";
const subscriptionFile = join(dataDir, "subscription.json");
const eventsFile = join(dataDir, "events.jsonl");
const messagesDir = join(dataDir, "messages");
const maxBodyBytes = 16_384;
const maxContactsBodyBytes = 1_572_864;
const maxContactsCiphertextBytes = 1_048_576 + 16;
// Two bounded 8 MiB assets need room for base64 and envelope metadata.
const maxAndroidBodyBytes = 24 * 1024 * 1024;
const outboxInFlight = new Set<string>();
const replyEvents = new EventEmitter();
replyEvents.setMaxListeners(0);
const messageEvents = new EventEmitter();
messageEvents.setMaxListeners(0);
const iosEvents = new EventEmitter();
iosEvents.setMaxListeners(0);
const iosSseHeartbeatMs = 22_000;
const iosSseDrainTimeoutMs = 30_000;
const maxPendingIosReplyHints = 32;
const apnsConfig = await loadApnsConfig();
const apnsSender = apnsConfig
  ? new ApnsSender(apnsConfig, testApnsTransport() ?? undefined)
  : undefined;

interface StoredSubscription {
  subscription: PushSubscription;
  ackToken: string;
  savedAt: number;
  activePairId?: string;
}
interface BrowserSession {
  sessionId: string;
  subscription: PushSubscription | NativePushDestination;
  pairId: string | null;
}
interface MessageRecord {
  messageId: string;
  payload: string;
  payloadBytes: number;
  queuedAt: number;
  sendAfter: number;
  context: object;
  attempts: number;
  serverSentAt?: number;
  pushServiceAcceptedAt?: number;
  responseCode?: number;
  swProcessedAt?: number;
  notificationOpenedAt?: number;
  sessionId: string;
  pairId: string;
}
interface AndroidMessage {
  v: 1 | 2 | 3 | 4 | 5;
  id: string;
  deviceId: string;
  seq: number;
  createdAt: number;
  previewEnvelope: PreviewEnvelope;
  assets?: AndroidAsset[];
  wechatUserId: 0 | 999;
  replyCapable: boolean;
  conversationSendCapable: boolean;
}
interface AndroidAsset {
  id: string;
  kind: "avatar" | "image" | "sticker";
  mimeType: string;
  width: number;
  height: number;
  envelope: PreviewEnvelope;
}
interface CachedAsset extends AndroidAsset { pairId: string }
interface ReplyEnvelope {
  alg: "A256GCM";
  kid: "phase1-reply";
  iv: string;
  aad: string;
  ct: string;
}
interface BrowserReply {
  v: 1 | 2 | 3 | 4;
  id: string;
  deviceId: string;
  createdAt: number;
  replyEnvelope: ReplyEnvelope;
  wechatUserId: 0 | 999;
  targetMessageId?: string;
  targetContactSnapshotId?: string;
}
interface ClaimedReply {
  v: 1 | 2 | 3 | 4;
  id: string;
  deviceId: string;
  createdAt: number;
  replyEnvelope: ReplyEnvelope;
  wechatUserId: 0 | 999;
  pairId?: string;
  targetMessageId?: string;
  targetContactSnapshotId?: string;
}
interface ContactsEnvelope {
  alg: "A256GCM";
  kid: "phase1-contacts";
  iv: string;
  aad: string;
  ct: string;
}
interface ContactsSnapshot {
  v: 1 | 2;
  id: string;
  deviceId: string;
  wechatUserId: 0 | 999;
  capturedAt: number;
  contactsEnvelope: ContactsEnvelope;
}

type DbExecutor = Pool | PoolConnection;

async function dbRows<T>(
  executor: DbExecutor,
  sql: string,
  values: unknown[] = [],
): Promise<T[]> {
  const [rows] = await executor.query<RowDataPacket[]>({ sql, values });
  return rows as T[];
}

async function dbOne<T>(
  executor: DbExecutor,
  sql: string,
  values: unknown[] = [],
): Promise<T | undefined> {
  return (await dbRows<T>(executor, sql, values))[0];
}

async function dbRun(
  executor: DbExecutor,
  sql: string,
  values: unknown[] = [],
): Promise<ResultSetHeader> {
  const [result] = await executor.execute<ResultSetHeader>({ sql, values });
  return result;
}

function dbInteger(value: unknown): number {
  const number = Number(value);
  if (!Number.isSafeInteger(number)) throw new Error("INVALID_DATABASE_INTEGER");
  return number;
}

const memoryCache = new Map<string, { value: CachedAsset; expiresAt: number }>();
let redis: RedisClientType | undefined;
let cacheMode: "memory" | "redis" = "memory";

if (storageKey.length !== 32)
  throw new Error("STORAGE_KEY must be 32 bytes base64url");
await mkdir(dataDir, { recursive: true, mode: 0o700 });
await mkdir(messagesDir, { recursive: true, mode: 0o700 });
await verifySchema();
await importLegacySubscription();
await initializeCache();
webpush.setVapidDetails(vapidSubject, vapidPublicKey, vapidPrivateKey);
await recoverScheduledMessages();
await recoverOutbox();
const outboxPoll = setInterval(() => void recoverOutbox(), 30_000);
outboxPoll.unref();
const nonceCleanup = setInterval(() => void cleanExpiredNonces(), 60_000);
nonceCleanup.unref();

createServer(async (request, response) => {
  try {
    await route(request, response);
  } catch (error) {
    const { status, code } = classifyRequestError(error);
    event("REQUEST_FAILED", { code });
    json(response, status, { error: code });
  }
}).listen(port, "0.0.0.0", () =>
  event("SERVER_STARTED", { buildVersion, port }),
);

async function route(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const url = new URL(request.url ?? "/", publicOrigin);
  setSecurityHeaders(response);
  if (
    request.method === "GET" &&
    (url.pathname === "/healthz" || url.pathname === "/readyz")
  ) {
    if (url.pathname === "/readyz") await dbOne(pool, "SELECT 1 AS ready");
    return json(response, 200, {
      status: "ok",
      buildVersion,
      cacheMode,
      database: "mysql",
    });
  }
  if (request.method === "GET" && url.pathname === "/api/config")
    return json(response, 200, {
      vapidPublicKey,
      buildVersion,
      maxPushBytes: 2800,
    });
  if (request.method === "POST" && url.pathname === "/api/subscription") {
    authorizeBrowser(request);
    const subscription = validateSubscription((await bodyJson(request)).json);
    const ackHeader = request.headers["x-awr-ack-token"];
    const pairHeader = request.headers["x-awr-pair-id"];
    if ((ackHeader === undefined) !== (pairHeader === undefined))
      throw new Error("PAIRING_MISMATCH");
    if (ackHeader !== undefined && pairHeader !== undefined) {
      const session = await authorizeBrowserPair(request, true);
      await dbRun(
        pool,
        "UPDATE browser_sessions SET subscription_envelope = ?, updated_at = ? WHERE session_id = ? AND invalidated_at IS NULL",
        [encryptSubscription(subscription), Date.now(), session.sessionId],
      );
      event("SUBSCRIPTION_UPDATED", { sessionId: session.sessionId });
      return json(response, 200, { ackToken: ackHeader });
    }
    const ackToken = randomBytes(32).toString("base64url");
    const sessionId = randomUUID();
    const now = Date.now();
    await dbRun(
      pool,
      "INSERT INTO browser_sessions(session_id, ack_token_hash, subscription_envelope, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
      [sessionId, tokenHash(ackToken), encryptSubscription(subscription), now, now],
    );
    event("SUBSCRIPTION_SAVED", { sessionId, savedAt: now });
    return json(response, 201, { ackToken });
  }
  if (request.method === "POST" && url.pathname === "/api/v1/ios/sessions") {
    authorizeBrowser(request);
    const body = validateIosSessionRequest((await bodyJson(request)).json);
    const ackToken = randomBytes(32).toString("base64url");
    const sessionId = randomUUID();
    const now = Date.now();
    const destination: NativePushDestination = {
      kind: "apns",
      deviceToken: null,
      environment: body.environment,
      previewEnabled: body.previewEnabled,
    };
    await dbRun(
      pool,
      "INSERT INTO browser_sessions(session_id, ack_token_hash, subscription_envelope, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
      [sessionId, tokenHash(ackToken), encryptSubscription(destination), now, now],
    );
    event("IOS_SESSION_CREATED", { sessionId });
    return json(response, 201, { ackToken });
  }
  if (request.method === "POST" && url.pathname === "/api/v1/pairings") {
    authorizeBrowser(request);
    await bodyJson(request);
    const session = await authorizeBrowserSession(request);
    const pairId = randomUUID();
    const pairSecret = randomBytes(32).toString("base64url");
    const expiresAt = Date.now() + 600_000;
    await transaction(async (connection) => {
      await dbRun(
        connection,
        "INSERT INTO pairings(pair_id, secret_hash, expires_at) VALUES (?, ?, ?)",
        [pairId, secretHash(pairSecret), expiresAt],
      );
      const result = await dbRun(
        connection,
        "UPDATE browser_sessions SET pair_id = ?, updated_at = ? WHERE session_id = ? AND invalidated_at IS NULL",
        [pairId, Date.now(), session.sessionId],
      );
      if (result.affectedRows !== 1) throw new Error("UNAUTHORIZED");
    });
    event("PAIRING_CREATED", { pairId, expiresAt });
    return json(response, 201, { pairId, pairSecret, expiresAt });
  }
  if (request.method === "POST" && url.pathname === "/api/v1/android/pair")
    return pairAndroid(request, response);
  if (request.method === "PUT" && url.pathname === "/api/v1/ios/push")
    return updateIosPush(request, response);
  if (request.method === "GET" && url.pathname === "/api/v1/ios/status")
    return getIosStatus(request, response);
  if (request.method === "GET" && url.pathname === "/api/v1/ios/events")
    return streamIosEvents(request, response, url);
  if (request.method === "GET" && url.pathname === "/api/v1/relay-policy")
    return getBrowserRelayPolicy(request, response);
  if (request.method === "PUT" && url.pathname === "/api/v1/relay-policy")
    return updateBrowserRelayPolicy(request, response);
  if (request.method === "POST" && url.pathname === "/api/v1/android/messages")
    return uploadAndroidMessage(request, response);
  if (request.method === "POST" && url.pathname === "/api/v1/android/contacts")
    return uploadAndroidContacts(request, response);
  if (request.method === "GET" && url.pathname === "/api/v1/ios/contacts")
    return getIosContacts(request, response);
  if (request.method === "POST" && url.pathname === "/api/v1/replies")
    return submitBrowserReply(request, response);
  if (request.method === "GET" && url.pathname.startsWith("/api/v1/replies/"))
    return getBrowserReply(request, response, decodeURIComponent(url.pathname.slice("/api/v1/replies/".length)));
  if (request.method === "GET" && url.pathname === "/api/v1/android/replies/v4")
    return getAndroidReply(request, response, url, true);
  if (request.method === "GET" && url.pathname === "/api/v1/android/replies")
    return getAndroidReply(request, response, url, false);
  if (request.method === "POST" && url.pathname.startsWith("/api/v1/android/replies/"))
    return acknowledgeAndroidReply(request, response, url);
  if (request.method === "GET" && url.pathname.startsWith("/api/v1/assets/"))
    return getAsset(request, response, decodeURIComponent(url.pathname.slice("/api/v1/assets/".length)));
  if (request.method === "POST" && url.pathname === "/api/test-push") {
    authorizeBrowser(request);
    const session = await authorizeBrowserPair(request, true);
    const pushRequest = validateTestPushRequest((await bodyJson(request)).json);
    const payload = addPushMetadata(
      buildDeclarativePayload(publicOrigin, pushRequest),
      session.pairId,
      0,
    );
    const delayMs = (pushRequest.delaySeconds ?? 0) * 1000;
    await saveMessage({
      messageId: pushRequest.messageId,
      payload,
      payloadBytes: Buffer.byteLength(payload),
      queuedAt: Date.now(),
      sendAfter: Date.now() + delayMs,
      context: pushRequest.context ?? {},
      attempts: 0,
      sessionId: session.sessionId,
      pairId: session.pairId,
    });
    event("PUSH_QUEUED", {
      messageId: pushRequest.messageId,
      delaySeconds: pushRequest.delaySeconds ?? 0,
    });
    schedulePush(pushRequest.messageId, delayMs);
    return json(response, 202, {
      messageId: pushRequest.messageId,
      payloadBytes: Buffer.byteLength(payload),
    });
  }
  if (request.method === "POST" && url.pathname === "/api/acks") {
    const session = await authorizeBrowserPair(request, true);
    const body = (await bodyJson(request)).json as Record<string, unknown>;
    if (
      typeof body.messageId !== "string" ||
      (body.type !== "SW_PROCESSED" && body.type !== "OPENED")
    )
      throw new Error("INVALID_ACK");
    const androidMessage = await dbOne<{ sent_at: string | number | null }>(
      pool,
      "SELECT push_outbox.sent_at FROM messages JOIN devices ON devices.device_id = messages.device_id JOIN push_outbox ON push_outbox.message_id = messages.id WHERE messages.id = ? AND devices.pair_id = ?",
      [body.messageId, session.pairId],
    );
    if (androidMessage) {
      if (androidMessage.sent_at === null) throw new Error("ACK_NOT_SENT");
      const at = Date.now();
      event(body.type, { messageId: body.messageId, at });
      return json(response, 202, { accepted: true });
    }
    const record = await loadMessage(body.messageId);
    if (record.sessionId !== session.sessionId || record.pairId !== session.pairId)
      throw new Error("MESSAGE_NOT_FOUND");
    if (!record.pushServiceAcceptedAt) throw new Error("ACK_NOT_SENT");
    const at = Date.now();
    if (body.type === "SW_PROCESSED" && !record.swProcessedAt)
      record.swProcessedAt = at;
    if (body.type === "OPENED" && !record.notificationOpenedAt)
      record.notificationOpenedAt = at;
    await saveMessage(record);
    event(body.type, { messageId: body.messageId, at });
    return json(response, 202, { accepted: true });
  }
  if (request.method === "GET" && url.pathname === "/api/v1/messages/wait")
    return waitBrowserMessage(request, response, url);
  if (request.method === "GET" && url.pathname === "/api/v1/messages/stream")
    return streamBrowserMessages(request, response, url);
  if (request.method === "GET" && url.pathname === "/api/v1/messages") {
    const { pairId } = await authorizeBrowserPair(request, false);
    const beforeSeq = parseCursor(url.searchParams.get("beforeSeq"));
    const messages = await dbRows<{
      id: string;
      device_id: string;
      seq: string | number;
      created_at: string | number;
      envelope_json: string;
      assets_json: string | null;
      received_at: string | number;
      wechat_user_id: string | number;
      reply_capable: string | number;
      conversation_send_capable: string | number;
    }>(
      pool,
      "SELECT messages.id, messages.device_id, messages.wechat_user_id, messages.reply_capable, messages.conversation_send_capable, messages.seq, messages.created_at, messages.envelope_json, messages.assets_json, messages.received_at FROM messages JOIN devices ON devices.device_id = messages.device_id WHERE devices.pair_id = ? AND messages.seq < ? ORDER BY messages.seq DESC, messages.wechat_user_id DESC LIMIT 21",
      [pairId, beforeSeq ?? Number.MAX_SAFE_INTEGER],
    );
    const hasMore = messages.length > 20;
    const page = messages.slice(0, 20);
    response.setHeader("Cache-Control", "no-store");
    return json(response, 200, {
      pairId,
      messages: page.map((message) => ({
        messageId: message.id,
        deviceId: message.device_id,
        wechatUserId: dbInteger(message.wechat_user_id),
        replyCapable: dbInteger(message.reply_capable) === 1,
        conversationSendCapable: dbInteger(message.conversation_send_capable) === 1,
        seq: dbInteger(message.seq),
        createdAt: dbInteger(message.created_at),
        receivedAt: dbInteger(message.received_at),
        previewEnvelope: JSON.parse(message.envelope_json),
        assets: message.assets_json ? JSON.parse(message.assets_json).map(assetMetadata) : [],
      })),
      nextCursor: hasMore ? String(dbInteger(page.at(-1)!.seq)) : null,
      hasMore,
    });
  }
  if (request.method === "GET" && url.pathname === "/api/status") {
    authorizeBrowser(request);
    const session = await authorizeBrowserPair(request, false);
    return json(response, 200, {
      events: await recentEvents(session.sessionId, session.pairId),
      messages: await recentMessages(session.sessionId),
      subscription: "active",
    });
  }
  if (request.method === "GET" || request.method === "HEAD")
    return staticFile(url.pathname, response, request.method === "HEAD");
  json(response, 404, { error: "NOT_FOUND" });
}

function validateIosSessionRequest(value: unknown): {
  environment: "sandbox" | "production";
  previewEnabled: boolean;
} {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("INVALID_IOS_SESSION");
  const body = value as Record<string, unknown>;
  if (Object.keys(body).some((key) => key !== "environment" && key !== "previewEnabled"))
    throw new Error("INVALID_IOS_SESSION");
  if (body.previewEnabled !== undefined && typeof body.previewEnabled !== "boolean")
    throw new Error("INVALID_IOS_SESSION");
  return {
    environment: body.environment === undefined
      ? "sandbox"
      : validateApnsEnvironment(body.environment),
    previewEnabled: body.previewEnabled ?? false,
  };
}

function validateIosPushRequest(value: unknown): NativePushDestination {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("INVALID_IOS_PUSH");
  const body = value as Record<string, unknown>;
  const keys = Object.keys(body).sort();
  if (
    keys.length !== 3 ||
    keys[0] !== "deviceToken" ||
    keys[1] !== "environment" ||
    keys[2] !== "previewEnabled" ||
    typeof body.previewEnabled !== "boolean"
  )
    throw new Error("INVALID_IOS_PUSH");
  return {
    kind: "apns",
    deviceToken: normalizeDeviceToken(body.deviceToken),
    environment: validateApnsEnvironment(body.environment),
    previewEnabled: body.previewEnabled,
  };
}

async function updateIosPush(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const session = await authorizeBrowserPair(request, true);
  const existing = await loadBrowserSessionById(session.sessionId);
  if (!existing || !isNativeDestination(existing.subscription))
    throw new Error("INVALID_IOS_SESSION");
  const destination = validateIosPushRequest((await bodyJson(request)).json);
  const result = await dbRun(
    pool,
    "UPDATE browser_sessions SET subscription_envelope = ?, updated_at = ? WHERE session_id = ? AND pair_id = ? AND invalidated_at IS NULL",
    [
      encryptSubscription(destination),
      Date.now(),
      session.sessionId,
      session.pairId,
    ],
  );
  if (result.affectedRows !== 1) throw new Error("UNAUTHORIZED");
  if (destination.deviceToken) void sendPendingOutboxForPair(session.pairId);
  event("IOS_PUSH_UPDATED", {
    sessionId: session.sessionId,
    registered: destination.deviceToken !== null,
  });
  json(response, 200, {
    pushConfigured: apnsSender !== undefined,
    pushRegistered: destination.deviceToken !== null,
  });
}

async function getIosStatus(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const session = await authorizeBrowserPair(request, true);
  const current = await loadBrowserSessionById(session.sessionId);
  if (!current || !isNativeDestination(current.subscription))
    throw new Error("INVALID_IOS_SESSION");
  const device = await dbOne<{
    device_id: string;
    last_seen_at: string | number | null;
  }>(
    pool,
    "SELECT devices.device_id, MAX(request_nonces.seen_at) AS last_seen_at FROM devices LEFT JOIN request_nonces ON request_nonces.device_id = devices.device_id WHERE devices.pair_id = ? GROUP BY devices.device_id",
    [session.pairId],
  );
  response.setHeader("Cache-Control", "no-store");
  json(response, 200, {
    paired: device !== undefined,
    deviceId: device?.device_id ?? null,
    lastSeenAt:
      device?.last_seen_at === null || device?.last_seen_at === undefined
        ? null
        : dbInteger(device.last_seen_at),
    serverTime: Date.now(),
    pushConfigured: apnsSender !== undefined,
    pushRegistered: current.subscription.deviceToken !== null,
  });
}

async function pairAndroid(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const body = (await bodyJson(request)).json as Record<string, unknown>;
  if (
    !isUuid(body.pairId) ||
    typeof body.pairSecret !== "string" ||
    !isDeviceId(body.deviceId) ||
    typeof body.publicKey !== "string"
  )
    throw new Error("INVALID_PAIR_REQUEST");
  const pairId = body.pairId;
  const pairSecret = body.pairSecret;
  const deviceId = body.deviceId;
  const publicKey = decodeBase64url(body.publicKey, 512, "INVALID_PUBLIC_KEY");
  try {
    const key = createPublicKey({
      key: publicKey,
      format: "der",
      type: "spki",
    });
    if (
      key.asymmetricKeyType !== "ec" ||
      key.asymmetricKeyDetails?.namedCurve !== "prime256v1"
    )
      throw new Error();
  } catch {
    throw new Error("INVALID_PUBLIC_KEY");
  }
  await transaction(async (connection) => {
    const pairing = await dbOne<{
      secret_hash: Buffer;
      expires_at: string | number;
      consumed_at: string | number | null;
    }>(
      connection,
      "SELECT secret_hash, expires_at, consumed_at FROM pairings WHERE pair_id = ? FOR UPDATE",
      [pairId],
    );
    if (
      !pairing ||
      pairing.consumed_at !== null ||
      dbInteger(pairing.expires_at) < Date.now() ||
      !timingSafeBufferEqual(pairing.secret_hash, secretHash(pairSecret))
    )
      throw new Error("PAIRING_INVALID");
    const now = Date.now();
    const result = await dbRun(
      connection,
      "UPDATE pairings SET consumed_at = ? WHERE pair_id = ? AND consumed_at IS NULL AND expires_at >= ?",
      [now, pairId, now],
    );
    if (result.affectedRows !== 1) throw new Error("PAIRING_INVALID");
    await dbRun(
      connection,
      "INSERT INTO devices(device_id, pair_id, public_key, paired_at) VALUES (?, ?, ?, ?)",
      [deviceId, pairId, publicKey, now],
    );
  });
  event("ANDROID_PAIRED", { pairId });
  json(response, 201, { paired: true });
}

async function submitBrowserReply(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const { pairId } = await authorizeBrowserPair(request, true);
  const reply = validateBrowserReply((await bodyJson(request)).json, pairId);
  const now = Date.now();
  const envelopeJson = JSON.stringify(reply.replyEnvelope);
  const existing = await dbOne<ReplyIdentityRow>(
    pool,
    "SELECT pair_id, target_message_id, contact_snapshot_id, device_id, wechat_user_id, created_at, envelope_json, status FROM replies WHERE id = ?",
    [reply.id],
  );
  if (existing) return replyIdempotentResponse(response, existing, reply, pairId, envelopeJson);

  if (reply.v === 4)
    await validateContactReplyTarget(reply, pairId);
  else
    await validateMessageReplyTarget(reply, pairId);
  try {
    await dbRun(
      pool,
      "INSERT INTO replies(id, pair_id, target_message_id, contact_snapshot_id, device_id, wechat_user_id, created_at, envelope_json, status, status_at, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?)",
      [
        reply.id,
        pairId,
        reply.targetMessageId ?? null,
        reply.targetContactSnapshotId ?? null,
        reply.deviceId,
        reply.wechatUserId,
        reply.createdAt,
        envelopeJson,
        now,
        now,
      ],
    );
  } catch (error) {
    if (isUniqueConstraint(error)) {
      const concurrent = await dbOne<ReplyIdentityRow>(
        pool,
        "SELECT pair_id, target_message_id, contact_snapshot_id, device_id, wechat_user_id, created_at, envelope_json, status FROM replies WHERE id = ?",
        [reply.id],
      );
      if (concurrent) return replyIdempotentResponse(response, concurrent, reply, pairId, envelopeJson);
    }
    throw error;
  }
  event(
    "REPLY_QUEUED",
    reply.v === 4 ? { replyId: reply.id } : { replyId: reply.id, targetMessageId: reply.targetMessageId },
  );
  replyEvents.emit("queued", reply.deviceId);
  iosEvents.emit("reply", pairId, reply.id);
  json(response, 201, { replyId: reply.id, status: "QUEUED" });
}

interface ReplyIdentityRow {
  pair_id: string;
  target_message_id: string | null;
  contact_snapshot_id: string | null;
  device_id: string;
  created_at: string | number;
  envelope_json: string;
  status: string;
  wechat_user_id: string | number;
}

function replyIdempotentResponse(
  response: ServerResponse,
  existing: ReplyIdentityRow,
  reply: BrowserReply,
  pairId: string,
  envelopeJson: string,
): void {
  if (
    existing.pair_id === pairId &&
    existing.target_message_id === (reply.targetMessageId ?? null) &&
    existing.contact_snapshot_id === (reply.targetContactSnapshotId ?? null) &&
    existing.device_id === reply.deviceId &&
    dbInteger(existing.wechat_user_id) === reply.wechatUserId &&
    dbInteger(existing.created_at) === reply.createdAt &&
    existing.envelope_json === envelopeJson
  ) return json(response, 200, { replyId: reply.id, status: existing.status });
  throw new Error("REPLY_ID_CONFLICT");
}

async function validateMessageReplyTarget(reply: BrowserReply, pairId: string): Promise<void> {
  const target = await dbOne<{
    wechat_user_id: string | number;
    reply_capable: string | number;
    conversation_send_capable: string | number;
  }>(
    pool,
    "SELECT messages.wechat_user_id, messages.reply_capable, messages.conversation_send_capable FROM messages JOIN devices ON devices.device_id = messages.device_id WHERE messages.id = ? AND messages.device_id = ? AND devices.pair_id = ?",
    [reply.targetMessageId, reply.deviceId, pairId],
  );
  if (!target) throw new Error("TARGET_MESSAGE_NOT_FOUND");
  if (dbInteger(target.reply_capable) !== 1) throw new Error("REPLY_UNSUPPORTED");
  if (reply.v === 3 && dbInteger(target.conversation_send_capable) !== 1)
    throw new Error("CONVERSATION_SEND_UNSUPPORTED");
  if (dbInteger(target.wechat_user_id) !== reply.wechatUserId)
    throw new Error("PROFILE_MISMATCH");
}

async function validateContactReplyTarget(reply: BrowserReply, pairId: string): Promise<void> {
  const snapshot = await dbOne<{
    id: string;
    device_id: string;
    wechat_user_id: string | number;
    captured_at: string | number;
    envelope_json: string;
  }>(
    pool,
    "SELECT contacts_snapshots.id, contacts_snapshots.device_id, contacts_snapshots.wechat_user_id, contacts_snapshots.captured_at, contacts_snapshots.envelope_json FROM contacts_snapshots JOIN devices ON devices.device_id = contacts_snapshots.device_id WHERE contacts_snapshots.id = ? AND contacts_snapshots.device_id = ? AND contacts_snapshots.wechat_user_id = ? AND devices.pair_id = ?",
    [reply.targetContactSnapshotId, reply.deviceId, reply.wechatUserId, pairId],
  );
  if (!snapshot || !isV2ContactsEnvelope(snapshot))
    throw new Error("TARGET_CONTACT_SNAPSHOT_NOT_FOUND");
}

function isV2ContactsEnvelope(snapshot: {
  id: string;
  device_id: string;
  wechat_user_id: string | number;
  captured_at: string | number;
  envelope_json: string;
}): boolean {
  try {
    const envelope = JSON.parse(snapshot.envelope_json) as ContactsEnvelope;
    return envelope.aad === `AWR1|A2I_CONTACTS|2|${snapshot.id}|${snapshot.device_id}|${dbInteger(snapshot.captured_at)}|${dbInteger(snapshot.wechat_user_id)}`;
  } catch {
    return false;
  }
}

async function getBrowserReply(
  request: IncomingMessage,
  response: ServerResponse,
  replyId: string,
): Promise<void> {
  if (!isUuid(replyId)) throw new Error("REPLY_NOT_FOUND");
  const { pairId } = await authorizeBrowserPair(request, false);
  const reply = await dbOne<ReplyStatusDbRow>(
    pool,
    "SELECT id, target_message_id, contact_snapshot_id, device_id, wechat_user_id, created_at, status, status_at FROM replies WHERE id = ? AND pair_id = ?",
    [replyId, pairId],
  );
  if (!reply) throw new Error("REPLY_NOT_FOUND");
  response.setHeader("Cache-Control", "no-store");
  json(response, 200, replyMetadata(reply));
}

async function getAndroidReply(
  request: IncomingMessage,
  response: ServerResponse,
  url: URL,
  includeContactReplies: boolean,
): Promise<void> {
  const pathname = includeContactReplies
    ? "/api/v1/android/replies/v4"
    : "/api/v1/android/replies";
  if (url.pathname !== pathname || url.search) throw new Error("INVALID_PATH");
  const bytes = await bodyBytes(request);
  if (bytes.length !== 0) throw new Error("INVALID_BODY");
  const signed = await authenticateAndroidRequest(
    request,
    bytes,
    "GET",
    pathname,
  );
  response.setHeader("Cache-Control", "no-store");
  const wake = waitForReply(signed.deviceId);
  try {
    if (response.destroyed) return;
    let reply = await claimReply(signed.deviceId, includeContactReplies, signed.nonce);
    if (!reply) {
      await wake.wait;
      if (response.destroyed) return;
      reply = await claimReply(signed.deviceId, includeContactReplies);
    }
    const relayPolicy = relayPolicyJson(await loadRelayPolicy(signed.pairId));
    if (!reply) return json(response, 200, { reply: null, relayPolicy });
    event("REPLY_DELIVERED_TO_ANDROID", { replyId: reply.id });
    iosEvents.emit("reply", signed.pairId, reply.id);
    json(response, 200, { reply, relayPolicy });
  } finally {
    wake.cancel();
  }
}

async function acknowledgeAndroidReply(
  request: IncomingMessage,
  response: ServerResponse,
  url: URL,
): Promise<void> {
  if (url.search || !url.pathname.endsWith("/ack")) throw new Error("NOT_FOUND");
  const replyId = decodeURIComponent(url.pathname.slice("/api/v1/android/replies/".length, -"/ack".length));
  if (!isUuid(replyId)) throw new Error("REPLY_NOT_FOUND");
  const { bytes, json: value } = await bodyJson(request);
  const status = validateReplyStatus(value);
  const signed = await authenticateAndroidRequest(
    request,
    bytes,
    "POST",
    url.pathname,
  );
  const now = Date.now();
  const pairId = await transaction(async (connection) => {
    await consumeAndroidNonce(connection, signed.deviceId, signed.nonce, now);
    const reply = await dbOne<{ pair_id: string }>(
      connection,
      "SELECT pair_id FROM replies WHERE id = ? AND device_id = ? FOR UPDATE",
      [replyId, signed.deviceId],
    );
    if (!reply) throw new Error("REPLY_NOT_FOUND");
    const result = await dbRun(
      connection,
      "UPDATE replies SET status = ?, status_at = ? WHERE id = ? AND device_id = ?",
      [status, now, replyId, signed.deviceId],
    );
    if (result.affectedRows !== 1) throw new Error("REPLY_NOT_FOUND");
    return reply.pair_id;
  });
  event("REPLY_ACKNOWLEDGED", { replyId, status });
  iosEvents.emit("reply", pairId, replyId);
  json(response, 202, { replyId, status });
}

async function uploadAndroidMessage(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const { bytes, json: value } = await bodyJson(request, maxAndroidBodyBytes);
  const message = validateAndroidMessage(value);
  const headerDeviceId = header(request, "x-awr-device-id");
  if (headerDeviceId !== message.deviceId) throw new Error("DEVICE_MISMATCH");
  const timestamp = Number(header(request, "x-awr-timestamp"));
  if (
    !Number.isSafeInteger(timestamp) ||
    Math.abs(Date.now() - timestamp) > 300_000
  )
    throw new Error("INVALID_TIMESTAMP");
  const nonce = header(request, "x-awr-nonce");
  decodeBase64url(nonce, 128, "INVALID_NONCE", 16);
  const signature = decodeBase64url(
    header(request, "x-awr-signature"),
    256,
    "INVALID_SIGNATURE",
  );
  const device = await dbOne<{ public_key: Buffer; pair_id: string }>(
    pool,
    "SELECT public_key, pair_id FROM devices WHERE device_id = ?",
    [message.deviceId],
  );
  if (!device) throw new Error("DEVICE_UNKNOWN");
  const canonical = `POST\n/api/v1/android/messages\n${timestamp}\n${nonce}\n${createHash("sha256").update(bytes).digest("hex")}`;
  if (
    !verify(
      "sha256",
      Buffer.from(canonical),
      {
        key: createPublicKey({
          key: device.public_key,
          format: "der",
          type: "spki",
        }),
        dsaEncoding: "der",
      },
      signature,
    )
  )
    throw new Error("INVALID_SIGNATURE");
  const now = Date.now();
  const payload = buildAndroidPushPayload(message, device.pair_id);
  const bodyHash = createHash("sha256").update(bytes).digest();
  const stored = await transaction(async (connection) => {
    const lockedDevice = await dbOne<{ pair_id: string }>(
      connection,
      "SELECT pair_id FROM devices WHERE device_id = ? FOR UPDATE",
      [message.deviceId],
    );
    if (!lockedDevice) throw new Error("DEVICE_UNKNOWN");
    await consumeAndroidNonce(connection, message.deviceId, nonce, now);
    const policy = await loadRelayPolicy(lockedDevice.pair_id, connection);
    if (!relayActive(policy))
      return { idempotent: false, pairId: lockedDevice.pair_id, dropped: true };
    const existing = await dbOne<{ body_hash: Buffer }>(
      connection,
      "SELECT body_hash FROM messages WHERE id = ?",
      [message.id],
    );
    if (existing) {
      if (!timingSafeBufferEqual(existing.body_hash, bodyHash))
        throw new Error("MESSAGE_ID_CONFLICT");
      return { idempotent: true, pairId: lockedDevice.pair_id, dropped: false };
    }
    const sequence = await dbOne<{ max_seq: string | number | null }>(
      connection,
      "SELECT MAX(seq) AS max_seq FROM messages WHERE device_id = ?",
      [message.deviceId],
    );
    if (
      sequence?.max_seq !== null &&
      sequence?.max_seq !== undefined &&
      message.seq <= dbInteger(sequence.max_seq)
    )
      throw new Error("INVALID_SEQUENCE");
    await dbRun(
      connection,
      "INSERT INTO messages(id, device_id, wechat_user_id, reply_capable, conversation_send_capable, seq, created_at, body_hash, envelope_json, assets_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      [
        message.id,
        message.deviceId,
        message.wechatUserId,
        message.replyCapable ? 1 : 0,
        message.conversationSendCapable ? 1 : 0,
        message.seq,
        message.createdAt,
        bodyHash,
        JSON.stringify(message.previewEnvelope),
        JSON.stringify((message.assets ?? []).map(assetMetadata)),
        now,
      ],
    );
    await dbRun(
      connection,
      "INSERT INTO push_outbox(message_id, payload, queued_at) VALUES (?, ?, ?)",
      [message.id, payload, now],
    );
    return { idempotent: false, pairId: lockedDevice.pair_id, dropped: false };
  });
  if (stored.dropped) {
    event("ANDROID_MESSAGE_DROPPED_BY_POLICY", { deviceId: message.deviceId });
    return json(response, 202, { id: message.id, dropped: true });
  }
  if (stored.idempotent)
    return json(response, 202, { id: message.id, idempotent: true });
  for (const asset of message.assets ?? [])
    await cacheAsset(stored.pairId, asset).catch(() => event("ASSET_CACHE_FAILED", { messageId: message.id, assetId: asset.id }));
  event("ANDROID_MESSAGE_QUEUED", {
    messageId: message.id,
    seq: message.seq,
  });
  messageEvents.emit("stored", stored.pairId, message.seq);
  void sendOutbox(message.id);
  json(response, 202, { id: message.id, idempotent: false });
}

async function uploadAndroidContacts(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const { bytes, json: value } = await bodyJson(request, maxContactsBodyBytes);
  const snapshot = validateContactsSnapshot(value);
  const signed = await authenticateAndroidRequest(
    request,
    bytes,
    "POST",
    "/api/v1/android/contacts",
  );
  if (signed.deviceId !== snapshot.deviceId) throw new Error("DEVICE_MISMATCH");
  const now = Date.now();
  const bodyHash = createHash("sha256").update(bytes).digest();
  const envelopeJson = JSON.stringify(snapshot.contactsEnvelope);
  const idempotent = await transaction(async (connection) => {
    const device = await dbOne<{ device_id: string }>(
      connection,
      "SELECT device_id FROM devices WHERE device_id = ? FOR UPDATE",
      [snapshot.deviceId],
    );
    if (!device) throw new Error("DEVICE_UNKNOWN");
    await consumeAndroidNonce(connection, snapshot.deviceId, signed.nonce, now);
    const existingId = await dbOne<{
      device_id: string;
      wechat_user_id: string | number;
      captured_at: string | number;
      body_hash: Buffer;
    }>(
      connection,
      "SELECT device_id, wechat_user_id, captured_at, body_hash FROM contacts_snapshots WHERE id = ? FOR UPDATE",
      [snapshot.id],
    );
    if (existingId) {
      if (
        existingId.device_id === snapshot.deviceId &&
        dbInteger(existingId.wechat_user_id) === snapshot.wechatUserId &&
        dbInteger(existingId.captured_at) === snapshot.capturedAt &&
        timingSafeBufferEqual(existingId.body_hash, bodyHash)
      ) return true;
      throw new Error("CONTACTS_ID_CONFLICT");
    }
    const current = await dbOne<{ captured_at: string | number }>(
      connection,
      "SELECT captured_at FROM contacts_snapshots WHERE device_id = ? AND wechat_user_id = ? FOR UPDATE",
      [snapshot.deviceId, snapshot.wechatUserId],
    );
    if (current && snapshot.capturedAt <= dbInteger(current.captured_at))
      throw new Error("CONTACTS_SNAPSHOT_STALE");
    if (current) {
      await dbRun(
        connection,
        "UPDATE contacts_snapshots SET id = ?, captured_at = ?, body_hash = ?, envelope_json = ?, received_at = ? WHERE device_id = ? AND wechat_user_id = ?",
        [snapshot.id, snapshot.capturedAt, bodyHash, envelopeJson, now, snapshot.deviceId, snapshot.wechatUserId],
      );
    } else {
      await dbRun(
        connection,
        "INSERT INTO contacts_snapshots(id, device_id, wechat_user_id, captured_at, body_hash, envelope_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [snapshot.id, snapshot.deviceId, snapshot.wechatUserId, snapshot.capturedAt, bodyHash, envelopeJson, now],
      );
    }
    return false;
  });
  json(response, idempotent ? 200 : 201, { id: snapshot.id, idempotent });
}

async function getIosContacts(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const session = await authorizeBrowserPair(request, false);
  const current = await loadBrowserSessionById(session.sessionId);
  if (!current || !isNativeDestination(current.subscription))
    throw new Error("INVALID_IOS_SESSION");
  const rows = await dbRows<{
    id: string;
    device_id: string;
    wechat_user_id: string | number;
    captured_at: string | number;
    envelope_json: string;
  }>(
    pool,
    "SELECT contacts_snapshots.id, contacts_snapshots.device_id, contacts_snapshots.wechat_user_id, contacts_snapshots.captured_at, contacts_snapshots.envelope_json FROM contacts_snapshots JOIN devices ON devices.device_id = contacts_snapshots.device_id WHERE devices.pair_id = ? ORDER BY contacts_snapshots.wechat_user_id ASC LIMIT 2",
    [session.pairId],
  );
  response.setHeader("Cache-Control", "no-store");
  json(response, 200, {
    snapshots: rows.map((row) => ({
      v: (JSON.parse(row.envelope_json) as ContactsEnvelope).aad.startsWith("AWR1|A2I_CONTACTS|2|") ? 2 : 1,
      id: row.id,
      deviceId: row.device_id,
      wechatUserId: dbInteger(row.wechat_user_id),
      capturedAt: dbInteger(row.captured_at),
      contactsEnvelope: JSON.parse(row.envelope_json),
    })),
  });
}

async function getBrowserRelayPolicy(request: IncomingMessage, response: ServerResponse): Promise<void> {
  const { pairId } = await authorizeBrowserPair(request, false);
  response.setHeader("Cache-Control", "no-store");
  json(response, 200, relayPolicyJson(await loadRelayPolicy(pairId)));
}

async function updateBrowserRelayPolicy(request: IncomingMessage, response: ServerResponse): Promise<void> {
  const { pairId } = await authorizeBrowserPair(request, true);
  const policy = validateRelayPolicy((await bodyJson(request)).json);
  await dbRun(
    pool,
    "INSERT INTO relay_policies(pair_id, enabled, schedule_enabled, weekdays_mask, start_minutes, end_minutes, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON DUPLICATE KEY UPDATE enabled = VALUES(enabled), schedule_enabled = VALUES(schedule_enabled), weekdays_mask = VALUES(weekdays_mask), start_minutes = VALUES(start_minutes), end_minutes = VALUES(end_minutes), updated_at = VALUES(updated_at)",
    [pairId, policy.enabled ? 1 : 0, policy.scheduleEnabled ? 1 : 0, policy.weekdaysMask, policy.startMinutes, policy.endMinutes, policy.updatedAt],
  );
  const device = await dbOne<{ device_id: string }>(pool, "SELECT device_id FROM devices WHERE pair_id = ?", [pairId]);
  if (device) replyEvents.emit("queued", device.device_id);
  event("RELAY_POLICY_UPDATED", { pairId, active: relayActive(policy) });
  response.setHeader("Cache-Control", "no-store");
  json(response, 200, relayPolicyJson(policy));
}

async function loadRelayPolicy(pairId: string, connection: Pool | PoolConnection = pool): Promise<RelayPolicy> {
  const row = await dbOne<{
    enabled: string | number;
    schedule_enabled: string | number;
    weekdays_mask: string | number;
    start_minutes: string | number;
    end_minutes: string | number;
    updated_at: string | number;
  }>(connection, "SELECT enabled, schedule_enabled, weekdays_mask, start_minutes, end_minutes, updated_at FROM relay_policies WHERE pair_id = ?", [pairId]);
  return row ? {
    enabled: dbInteger(row.enabled) === 1,
    scheduleEnabled: dbInteger(row.schedule_enabled) === 1,
    weekdaysMask: dbInteger(row.weekdays_mask),
    startMinutes: dbInteger(row.start_minutes),
    endMinutes: dbInteger(row.end_minutes),
    updatedAt: dbInteger(row.updated_at),
  } : { ...DEFAULT_RELAY_POLICY };
}

async function waitBrowserMessage(
  request: IncomingMessage,
  response: ServerResponse,
  url: URL,
): Promise<void> {
  const { pairId } = await authorizeBrowserPair(request, false);
  const rawAfterSeq = url.searchParams.get("afterSeq");
  if (!rawAfterSeq || !/^(?:0|[1-9][0-9]*)$/.test(rawAfterSeq)) throw new Error("INVALID_CURSOR");
  const afterSeq = Number(rawAfterSeq);
  if (!Number.isSafeInteger(afterSeq)) throw new Error("INVALID_CURSOR");
  response.setHeader("Cache-Control", "no-store");
  const wake = waitForMessage(pairId, afterSeq);
  try {
    let latestSeq = await latestMessageSeq(pairId);
    if (latestSeq <= afterSeq) {
      await wake.wait;
      if (response.destroyed) return;
      latestSeq = await latestMessageSeq(pairId);
    }
    json(response, 200, { available: latestSeq > afterSeq, latestSeq });
  } finally {
    wake.cancel();
  }
}

async function streamBrowserMessages(
  request: IncomingMessage,
  response: ServerResponse,
  url: URL,
): Promise<void> {
  const { pairId } = await authorizeBrowserPair(request, false);
  const rawAfterSeq = url.searchParams.get("afterSeq");
  if (!rawAfterSeq || !/^(?:0|[1-9][0-9]*)$/.test(rawAfterSeq)) throw new Error("INVALID_CURSOR");
  const afterSeq = Number(rawAfterSeq);
  if (!Number.isSafeInteger(afterSeq)) throw new Error("INVALID_CURSOR");
  response.setHeader("Content-Type", "text/event-stream; charset=utf-8");
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Accel-Buffering", "no");
  response.flushHeaders();
  let cursor = afterSeq;
  let closed = false;
  let wake: { wait: Promise<void>; cancel: () => void } | undefined;
  response.on("close", () => {
    closed = true;
    wake?.cancel();
  });
  try {
    while (!closed) {
      wake = waitForMessage(pairId, cursor);
      let latestSeq = await latestMessageSeq(pairId);
      if (latestSeq <= cursor) {
        await wake.wait;
        latestSeq = await latestMessageSeq(pairId);
      }
      wake.cancel();
      if (closed) break;
      if (latestSeq > cursor) {
        cursor = latestSeq;
        response.write(`data: ${cursor}\n\n`);
      } else {
        response.write(": keepalive\n\n");
      }
    }
  } finally {
    wake?.cancel();
  }
}

async function streamIosEvents(
  request: IncomingMessage,
  response: ServerResponse,
  url: URL,
): Promise<void> {
  const { pairId } = await authorizeNativeBrowserPair(request);
  const rawAfterSeq = url.searchParams.get("afterSeq");
  if (!rawAfterSeq || !/^(?:0|[1-9][0-9]*)$/.test(rawAfterSeq))
    throw new Error("INVALID_CURSOR");
  const afterSeq = Number(rawAfterSeq);
  if (!Number.isSafeInteger(afterSeq)) throw new Error("INVALID_CURSOR");

  response.setHeader("Content-Type", "text/event-stream; charset=utf-8");
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Accel-Buffering", "no");
  response.flushHeaders();

  let closed = false;
  let opened = false;
  let changeVersion = 0;
  let pendingMessage = false;
  const pendingReplies = new Set<string>();
  let notifyWaiter: (() => void) | undefined;
  const notify = (): void => {
    changeVersion++;
    const resolve = notifyWaiter;
    notifyWaiter = undefined;
    resolve?.();
  };
  const onMessage = (storedPairId: string): void => {
    if (storedPairId !== pairId) return;
    pendingMessage = true;
    notify();
  };
  const onReply = (storedPairId: string, replyId: string): void => {
    if (storedPairId !== pairId) return;
    if (pendingReplies.size >= maxPendingIosReplyHints) {
      closed = true;
      response.end();
      notify();
      return;
    }
    pendingReplies.add(replyId);
    notify();
  };
  const close = (): void => {
    closed = true;
    notify();
  };
  messageEvents.on("stored", onMessage);
  iosEvents.on("reply", onReply);
  response.once("close", close);

  const waitForChange = (observedVersion: number, timeoutMs: number): Promise<boolean> => {
    if (closed || changeVersion !== observedVersion) return Promise.resolve(true);
    return new Promise((resolve) => {
      const timer = setTimeout(done, timeoutMs);
      timer.unref();
      notifyWaiter = () => done(true);
      function done(changed = false): void {
        clearTimeout(timer);
        if (notifyWaiter) notifyWaiter = undefined;
        resolve(changed);
      }
    });
  };

  try {
    // Subscribe before reading the cursor so a store during the read is observed.
    const readySeq = await latestMessageSeq(pairId);
    if (!await writeSse(response, "ready", String(readySeq))) return;
    opened = true;
    event("IOS_STREAM_OPENED", {});
    let nextAuthorizationAt = Date.now() + iosSseHeartbeatMs;
    while (!closed) {
      if (Date.now() >= nextAuthorizationAt) {
        try {
          await authorizeNativeBrowserPair(request);
        } catch {
          event("IOS_SSE_AUTH_REVOKED", {});
          response.end();
          return;
        }
        if (!await writeSse(response, undefined, "keepalive")) return;
        nextAuthorizationAt = Date.now() + iosSseHeartbeatMs;
        continue;
      }
      if (pendingMessage) {
        pendingMessage = false;
        const latestSeq = await latestMessageSeq(pairId);
        if (!await writeSse(response, "message", String(latestSeq))) return;
        continue;
      }
      const replyId = pendingReplies.values().next().value as string | undefined;
      if (replyId) {
        pendingReplies.delete(replyId);
        if (!await writeSse(response, "reply", replyId)) return;
        continue;
      }
      const observedVersion = changeVersion;
      await waitForChange(observedVersion, nextAuthorizationAt - Date.now());
    }
  } finally {
    messageEvents.off("stored", onMessage);
    iosEvents.off("reply", onReply);
    response.off("close", close);
    if (!response.destroyed && !response.writableEnded) response.end();
    if (opened) event("IOS_STREAM_CLOSED", {});
  }
}

async function writeSse(
  response: ServerResponse,
  eventName: "ready" | "message" | "reply" | undefined,
  data: string,
): Promise<boolean> {
  if (response.destroyed || response.writableEnded) return false;
  const payload = eventName
    ? `event: ${eventName}\ndata: ${data}\n\n`
    : `: ${data}\n\n`;
  if (response.write(payload)) return true;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      response.destroy();
      done(false);
    }, iosSseDrainTimeoutMs);
    timer.unref();
    response.once("drain", drained);
    response.once("close", closed);
    function done(result: boolean): void {
      clearTimeout(timer);
      response.off("drain", drained);
      response.off("close", closed);
      resolve(result && !response.destroyed && !response.writableEnded);
    }
    function drained(): void { done(true); }
    function closed(): void { done(false); }
  });
}

function validateAndroidMessage(value: unknown): AndroidMessage {
  if (!value || typeof value !== "object") throw new Error("INVALID_MESSAGE");
  const message = value as Partial<AndroidMessage>;
  if (
    (message.v !== 1 && message.v !== 2 && message.v !== 3 && message.v !== 4 && message.v !== 5) ||
    !isUuid(message.id) ||
    !isDeviceId(message.deviceId) ||
    !Number.isSafeInteger(message.seq) ||
    message.seq! < 1 ||
    !Number.isSafeInteger(message.createdAt) ||
    Math.abs(Date.now() - message.createdAt!) > 172_800_000
  )
    throw new Error("INVALID_MESSAGE");
  const profileVersion = message.v === 3 || message.v === 4 || message.v === 5;
  const wechatUserId = profileVersion ? message.wechatUserId : 0;
  if (wechatUserId !== 0 && wechatUserId !== 999)
    throw new Error("INVALID_PROFILE");
  const replyCapable = message.v === 4 || message.v === 5 ? message.replyCapable : false;
  if ((message.v === 4 || message.v === 5) && typeof replyCapable !== "boolean")
    throw new Error("INVALID_REPLY_CAPABILITY");
  const conversationSendCapable = message.v === 5 ? message.conversationSendCapable : false;
  if (message.v === 5 && typeof conversationSendCapable !== "boolean")
    throw new Error("INVALID_CONVERSATION_SEND_CAPABILITY");
  if (conversationSendCapable && !replyCapable)
    throw new Error("INVALID_CONVERSATION_SEND_CAPABILITY");
  const e = message.previewEnvelope;
  if (
    !e ||
    e.alg !== "A256GCM" ||
    e.kid !== "phase1" ||
    typeof e.aad !== "string"
  )
    throw new Error("INVALID_ENVELOPE");
  decodeBase64url(e.iv, 24, "INVALID_ENVELOPE", 12);
  if (decodeBase64url(e.ct, 4096, "INVALID_ENVELOPE").length < 17)
    throw new Error("INVALID_ENVELOPE");
  if (
    e.aad !==
    `AWR1|A2I|${message.id}|${message.deviceId}|${message.seq}|${message.createdAt}${profileVersion ? `|${wechatUserId}` : ""}`
  )
    throw new Error("INVALID_AAD");
  if (message.v === 1 && message.assets !== undefined) throw new Error("INVALID_ASSETS");
  const assets = message.assets ?? [];
  if (!Array.isArray(assets) || assets.length > 2) throw new Error("INVALID_ASSETS");
  const ids = new Set<string>();
  for (const asset of assets) {
    if (!asset || typeof asset !== "object") throw new Error("INVALID_ASSET");
    const candidate = asset as Partial<AndroidAsset>;
    if (!isUuid(candidate.id) || ids.has(candidate.id) || (candidate.kind !== "avatar" && candidate.kind !== "image" && candidate.kind !== "sticker") || typeof candidate.mimeType !== "string" || !/^image\/(?:jpeg|png|webp)$/i.test(candidate.mimeType) || !Number.isSafeInteger(candidate.width) || !Number.isSafeInteger(candidate.height) || candidate.width! < 1 || candidate.height! < 1 || candidate.width! > 16384 || candidate.height! > 16384 || candidate.width! * candidate.height! > 64_000_000) throw new Error("INVALID_ASSET");
    const envelope = candidate.envelope;
    if (!envelope || envelope.alg !== "A256GCM" || envelope.kid !== "phase1-asset" || envelope.aad !== `AWR1|A2I_ASSET|${candidate.id}|${message.deviceId}|${message.seq}|${message.createdAt}${profileVersion ? `|${wechatUserId}` : ""}`) throw new Error("INVALID_ASSET_AAD");
    decodeBase64url(envelope.iv, 24, "INVALID_ASSET", 12);
    if (decodeBase64url(envelope.ct, 8 * 1024 * 1024 + 16, "INVALID_ASSET").length < 17) throw new Error("INVALID_ASSET");
    ids.add(candidate.id);
  }
  return { ...message, wechatUserId, replyCapable, conversationSendCapable } as AndroidMessage;
}
function validateContactsSnapshot(value: unknown): ContactsSnapshot {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("INVALID_CONTACTS_SNAPSHOT");
  const snapshot = value as Partial<ContactsSnapshot>;
  const capturedAt = snapshot.capturedAt;
  const keys = Object.keys(snapshot).sort();
  if (
    keys.length !== 6 ||
    keys[0] !== "capturedAt" ||
    keys[1] !== "contactsEnvelope" ||
    keys[2] !== "deviceId" ||
    keys[3] !== "id" ||
    keys[4] !== "v" ||
    keys[5] !== "wechatUserId" ||
    (snapshot.v !== 1 && snapshot.v !== 2) ||
    !isUuidV7(snapshot.id) ||
    !isDeviceId(snapshot.deviceId) ||
    (snapshot.wechatUserId !== 0 && snapshot.wechatUserId !== 999) ||
    typeof capturedAt !== "number" ||
    !Number.isSafeInteger(capturedAt) ||
    capturedAt <= 0 ||
    capturedAt > Date.now() + 300_000
  )
    throw new Error("INVALID_CONTACTS_SNAPSHOT");
  const envelope = snapshot.contactsEnvelope;
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope))
    throw new Error("INVALID_CONTACTS_ENVELOPE");
  const envelopeKeys = Object.keys(envelope).sort();
  if (
    envelopeKeys.length !== 5 ||
    envelopeKeys[0] !== "aad" ||
    envelopeKeys[1] !== "alg" ||
    envelopeKeys[2] !== "ct" ||
    envelopeKeys[3] !== "iv" ||
    envelopeKeys[4] !== "kid" ||
    envelope.alg !== "A256GCM" ||
    envelope.kid !== "phase1-contacts" ||
    typeof envelope.aad !== "string" ||
    envelope.aad !== `AWR1|A2I_CONTACTS|${snapshot.v}|${snapshot.id}|${snapshot.deviceId}|${capturedAt}|${snapshot.wechatUserId}` ||
    typeof envelope.iv !== "string" ||
    typeof envelope.ct !== "string"
  )
    throw new Error("INVALID_CONTACTS_ENVELOPE");
  decodeBase64url(envelope.iv, 24, "INVALID_CONTACTS_ENVELOPE", 12);
  if (decodeBase64url(envelope.ct, maxContactsCiphertextBytes, "INVALID_CONTACTS_ENVELOPE").length < 17)
    throw new Error("INVALID_CONTACTS_ENVELOPE");
  return snapshot as ContactsSnapshot;
}
function validateBrowserReply(value: unknown, pairId: string): BrowserReply {
  if (!value || typeof value !== "object") throw new Error("INVALID_REPLY");
  const reply = value as Partial<BrowserReply>;
  if (
    (reply.v !== 1 && reply.v !== 2 && reply.v !== 3 && reply.v !== 4) ||
    !isUuid(reply.id) ||
    !isDeviceId(reply.deviceId) ||
    !Number.isSafeInteger(reply.createdAt) ||
    Math.abs(Date.now() - reply.createdAt!) > 172_800_000
  )
    throw new Error("INVALID_REPLY");
  if (
    reply.v === 4
      ? reply.targetMessageId !== undefined || !isUuid(reply.targetContactSnapshotId)
      : !isUuid(reply.targetMessageId) || reply.targetContactSnapshotId !== undefined
  ) throw new Error("INVALID_REPLY");
  const wechatUserId = reply.v === 1 ? 0 : reply.wechatUserId;
  if (wechatUserId !== 0 && wechatUserId !== 999)
    throw new Error("INVALID_PROFILE");
  const envelope = reply.replyEnvelope;
  if (
    !envelope ||
    envelope.alg !== "A256GCM" ||
    envelope.kid !== "phase1-reply" ||
    typeof envelope.aad !== "string"
  )
    throw new Error("INVALID_REPLY_ENVELOPE");
  decodeBase64url(envelope.iv, 24, "INVALID_REPLY_ENVELOPE", 12);
  if (decodeBase64url(envelope.ct, 4096, "INVALID_REPLY_ENVELOPE").length < 17)
    throw new Error("INVALID_REPLY_ENVELOPE");
  if (
    envelope.aad !==
    (reply.v === 4
      ? `AWR1|I2A|4|CONTACT_SEND|${pairId}|${reply.id}|${reply.deviceId}|${reply.targetContactSnapshotId}|${reply.createdAt}|${wechatUserId}`
      : reply.v === 3
      ? `AWR1|I2A|3|CONVERSATION_SEND|${pairId}|${reply.id}|${reply.deviceId}|${reply.targetMessageId}|${reply.createdAt}|${wechatUserId}`
      : `AWR1|I2A|${reply.id}|${reply.deviceId}|${reply.targetMessageId}|${reply.createdAt}${reply.v === 2 ? `|${wechatUserId}` : ""}`)
  )
    throw new Error("INVALID_REPLY_AAD");
  return { ...reply, wechatUserId } as BrowserReply;
}

const replyStatuses = new Set([
  "SENT_TO_WECHAT",
  "NOTIFICATION_NOT_ACTIVE",
  "WECHAT_ACTION_CHANGED",
  "REMOTE_INPUT_UNSUPPORTED",
  "CONTACT_SNAPSHOT_STALE",
  "AUTOMATION_NOT_READY",
  "WECHAT_WINDOW_TIMEOUT",
  "PENDING_INTENT_CANCELED",
  "INVALID_REPLY",
  "FAILED",
]);
function validateReplyStatus(value: unknown): string {
  if (
    !value ||
    typeof value !== "object" ||
    !replyStatuses.has((value as { status?: unknown }).status as string)
  )
    throw new Error("INVALID_REPLY_STATUS");
  return (value as { status: string }).status;
}

interface AndroidRequest {
  deviceId: string;
  pairId: string;
  nonce: string;
}
interface ReplyStatusDbRow {
  id: string;
  target_message_id: string | null;
  contact_snapshot_id: string | null;
  device_id: string;
  created_at: string | number;
  status: string;
  status_at: string | number;
  wechat_user_id: string | number;
}
async function authenticateAndroidRequest(
  request: IncomingMessage,
  bytes: Buffer,
  method: "GET" | "POST",
  pathname: string,
): Promise<AndroidRequest> {
  const deviceId = header(request, "x-awr-device-id");
  if (!isDeviceId(deviceId)) throw new Error("DEVICE_UNKNOWN");
  const timestamp = Number(header(request, "x-awr-timestamp"));
  if (!Number.isSafeInteger(timestamp) || Math.abs(Date.now() - timestamp) > 300_000)
    throw new Error("INVALID_TIMESTAMP");
  const nonce = header(request, "x-awr-nonce");
  decodeBase64url(nonce, 128, "INVALID_NONCE", 16);
  const signature = decodeBase64url(
    header(request, "x-awr-signature"),
    256,
    "INVALID_SIGNATURE",
  );
  const device = await dbOne<{ public_key: Buffer; pair_id: string }>(
    pool,
    "SELECT public_key, pair_id FROM devices WHERE device_id = ?",
    [deviceId],
  );
  if (!device) throw new Error("DEVICE_UNKNOWN");
  const canonical = `${method}\n${pathname}\n${timestamp}\n${nonce}\n${createHash("sha256").update(bytes).digest("hex")}`;
  if (
    !verify(
      "sha256",
      Buffer.from(canonical),
      {
        key: createPublicKey({ key: device.public_key, format: "der", type: "spki" }),
        dsaEncoding: "der",
      },
      signature,
    )
  )
    throw new Error("INVALID_SIGNATURE");
  return { deviceId, pairId: device.pair_id, nonce };
}
async function consumeAndroidNonce(
  connection: PoolConnection,
  deviceId: string,
  nonce: string,
  now: number,
): Promise<void> {
  try {
    await dbRun(
      connection,
      "INSERT INTO request_nonces(device_id, nonce, seen_at) VALUES (?, ?, ?)",
      [deviceId, nonce, now],
    );
  } catch (error) {
    if (isUniqueConstraint(error)) throw new Error("REPLAYED_NONCE");
    throw error;
  }
}

async function cleanExpiredNonces(): Promise<void> {
  await dbRun(
    pool,
    "DELETE FROM request_nonces WHERE seen_at < ? ORDER BY seen_at LIMIT 1000",
    [Date.now() - 600_000],
  ).catch(() => event("NONCE_CLEANUP_FAILED", {}));
}
async function claimReply(
  deviceId: string,
  includeContactReplies: boolean,
  nonce?: string,
): Promise<ClaimedReply | undefined> {
  const now = Date.now();
  return transaction(async (connection) => {
    if (nonce)
      await consumeAndroidNonce(connection, deviceId, nonce, now);
    const reply = await dbOne<{
      id: string;
      pair_id: string;
      target_message_id: string | null;
      contact_snapshot_id: string | null;
      device_id: string;
      created_at: string | number;
      envelope_json: string;
      wechat_user_id: string | number;
    }>(
      connection,
      `SELECT id, pair_id, target_message_id, contact_snapshot_id, device_id, wechat_user_id, created_at, envelope_json FROM replies WHERE device_id = ? AND status = 'QUEUED'${includeContactReplies ? "" : " AND contact_snapshot_id IS NULL"} ORDER BY queued_at ASC LIMIT 1 FOR UPDATE`,
      [deviceId],
    );
    if (!reply) return undefined;
    const result = await dbRun(
      connection,
      "UPDATE replies SET status = 'DELIVERED_TO_ANDROID', status_at = ? WHERE id = ? AND status = 'QUEUED'",
      [now, reply.id],
    );
    if (result.affectedRows !== 1) throw new Error("REPLY_CLAIM_CONFLICT");
    const replyEnvelope = JSON.parse(reply.envelope_json) as ReplyEnvelope;
    const isContactSend = reply.contact_snapshot_id !== null;
    const isConversationSend = replyEnvelope.aad.startsWith("AWR1|I2A|3|CONVERSATION_SEND|");
    if (isContactSend && !replyEnvelope.aad.startsWith("AWR1|I2A|4|CONTACT_SEND|"))
      throw new Error("INVALID_STORED_REPLY");
    return {
      v: isContactSend ? 4 : isConversationSend ? 3 : replyEnvelope.aad.split("|").length === 7 ? 2 : 1,
      id: reply.id,
      deviceId: reply.device_id,
      createdAt: dbInteger(reply.created_at),
      replyEnvelope,
      wechatUserId: dbInteger(reply.wechat_user_id) as 0 | 999,
      ...(isContactSend
        ? { pairId: reply.pair_id, targetContactSnapshotId: reply.contact_snapshot_id! }
        : { targetMessageId: reply.target_message_id! }),
      ...(isConversationSend ? { pairId: reply.pair_id } : {}),
    };
  });
}
function waitForReply(deviceId: string): { wait: Promise<void>; cancel: () => void } {
  let cancel: () => void = () => undefined;
  const wait = new Promise<void>((resolve) => {
    const timer = setTimeout(done, 20_000);
    timer.unref();
    const listener = (queuedDeviceId: string) => {
      if (queuedDeviceId === deviceId) done();
    };
    function done(): void {
      clearTimeout(timer);
      replyEvents.off("queued", listener);
      resolve();
    }
    replyEvents.on("queued", listener);
    cancel = done;
  });
  return { wait, cancel };
}
async function latestMessageSeq(pairId: string): Promise<number> {
  const row = await dbOne<{ latest_seq: string | number | null }>(
    pool,
    "SELECT MAX(messages.seq) AS latest_seq FROM messages JOIN devices ON devices.device_id = messages.device_id WHERE devices.pair_id = ?",
    [pairId],
  );
  return row?.latest_seq === null || row?.latest_seq === undefined
    ? 0
    : dbInteger(row.latest_seq);
}
function waitForMessage(pairId: string, afterSeq: number): { wait: Promise<void>; cancel: () => void } {
  let cancel: () => void = () => undefined;
  const wait = new Promise<void>((resolve) => {
    const timer = setTimeout(done, 20_000);
    timer.unref();
    const listener = (storedPairId: string, seq: number) => {
      if (storedPairId === pairId && seq > afterSeq) done();
    };
    function done(): void {
      clearTimeout(timer);
      messageEvents.off("stored", listener);
      resolve();
    }
    messageEvents.on("stored", listener);
    cancel = done;
  });
  return { wait, cancel };
}
function replyMetadata(reply: ReplyStatusDbRow): Record<string, unknown> {
  return {
    replyId: reply.id,
    ...(reply.target_message_id ? { targetMessageId: reply.target_message_id } : {}),
    ...(reply.contact_snapshot_id ? { targetContactSnapshotId: reply.contact_snapshot_id } : {}),
    deviceId: reply.device_id,
    wechatUserId: dbInteger(reply.wechat_user_id),
    createdAt: dbInteger(reply.created_at),
    status: reply.status,
    statusAt: dbInteger(reply.status_at),
  };
}
function assetMetadata(asset: AndroidAsset): Omit<AndroidAsset, "envelope"> {
  const { envelope: _envelope, ...metadata } = asset;
  return metadata;
}
function parseCursor(value: string | null): number | undefined {
  if (value === null) return undefined;
  if (!/^[1-9][0-9]*$/.test(value)) throw new Error("INVALID_CURSOR");
  const cursor = Number(value);
  if (!Number.isSafeInteger(cursor)) throw new Error("INVALID_CURSOR");
  return cursor;
}
function assetCacheKey(pairId: string, assetId: string): string {
  return `awr:asset:${pairId}:${assetId}`;
}
async function initializeCache(): Promise<void> {
  const url = process.env.REDIS_URL;
  if (!url) return;
  try {
    redis = createClient({ url });
    redis.on("ready", () => { cacheMode = "redis"; });
    redis.on("error", () => { cacheMode = "memory"; });
    await redis.connect();
    cacheMode = "redis";
  } catch {
    redis = undefined;
    cacheMode = "memory";
  }
}
async function cacheAsset(pairId: string, asset: AndroidAsset): Promise<void> {
  const value: CachedAsset = { ...asset, pairId };
  const ttl = asset.kind === "avatar" ? 30 * 86_400 : 7 * 86_400;
  const key = assetCacheKey(pairId, asset.id);
  if (redis?.isReady) {
    await redis.set(key, JSON.stringify(value), { EX: ttl });
    return;
  }
  memoryCache.set(key, { value, expiresAt: Date.now() + ttl * 1_000 });
}
async function cachedAsset(pairId: string, assetId: string): Promise<CachedAsset | undefined> {
  const key = assetCacheKey(pairId, assetId);
  if (redis?.isReady) {
    const value = await redis.get(key);
    return value ? JSON.parse(value) as CachedAsset : undefined;
  }
  const item = memoryCache.get(key);
  if (!item || item.expiresAt <= Date.now()) { memoryCache.delete(key); return undefined; }
  return item.value;
}
async function getAsset(request: IncomingMessage, response: ServerResponse, assetId: string): Promise<void> {
  if (!isUuid(assetId)) throw new Error("ASSET_EXPIRED");
  const { pairId } = await authorizeBrowserPair(request, false);
  const asset = await cachedAsset(pairId, assetId);
  if (!asset || asset.pairId !== pairId) return json(response, 404, { error: "ASSET_EXPIRED" });
  response.setHeader("Cache-Control", "no-store");
  json(response, 200, asset);
}
function buildAndroidPushPayload(message: AndroidMessage, pairId: string): string {
  const navigate = `${publicOrigin}/#${new URLSearchParams({
    messageId: message.id,
    encryptedPreview: JSON.stringify(message.previewEnvelope),
    pairId,
    wechatUserId: String(message.wechatUserId),
  })}`;
  const payload = JSON.stringify({
    web_push: 8030,
    notification: {
      title: "工作微信",
      body: "收到一条工作微信, 点击查看",
      navigate,
      tag: `wechat:${message.id}`,
      data: {
        v: 3,
        messageId: message.id,
        pairId,
        wechatUserId: message.wechatUserId,
      },
    },
  });
  if (Buffer.byteLength(payload) > 2800) throw new Error("PAYLOAD_TOO_LARGE");
  return payload;
}
function addPushMetadata(payload: string, pairId: string, wechatUserId: 0 | 999): string {
  const parsed = JSON.parse(payload) as {
    notification: {
      navigate: string;
      data?: Record<string, unknown>;
    };
  };
  const navigate = new URL(parsed.notification.navigate);
  const fragment = new URLSearchParams(navigate.hash.slice(1));
  fragment.set("pairId", pairId);
  fragment.set("wechatUserId", String(wechatUserId));
  navigate.hash = fragment.toString();
  parsed.notification.navigate = navigate.toString();
  parsed.notification.data = {
    ...parsed.notification.data,
    pairId,
    wechatUserId,
  };
  const enriched = JSON.stringify(parsed);
  if (Buffer.byteLength(enriched) > 2800) throw new Error("PAYLOAD_TOO_LARGE");
  return enriched;
}
async function sendOutbox(messageId: string): Promise<void> {
  if (outboxInFlight.has(messageId)) return;
  outboxInFlight.add(messageId);
  try {
    const row = await dbOne<{
      payload: string;
      session_id: string;
      subscription_envelope: string;
      pair_id: string;
      device_id: string;
      seq: string | number;
      created_at: string | number;
      envelope_json: string;
      wechat_user_id: string | number;
      reply_capable: string | number;
      conversation_send_capable: string | number;
    }>(
      pool,
      "SELECT push_outbox.payload, browser_sessions.session_id, browser_sessions.subscription_envelope, devices.pair_id, messages.device_id, messages.seq, messages.created_at, messages.envelope_json, messages.wechat_user_id, messages.reply_capable, messages.conversation_send_capable FROM push_outbox JOIN messages ON messages.id = push_outbox.message_id JOIN devices ON devices.device_id = messages.device_id JOIN browser_sessions ON browser_sessions.pair_id = devices.pair_id AND browser_sessions.invalidated_at IS NULL WHERE push_outbox.message_id = ? AND push_outbox.sent_at IS NULL",
      [messageId],
    );
    if (!row) return;
    const destination = decryptSubscription(row.subscription_envelope);
    if (isNativeDestination(destination) && (!apnsSender || !destination.deviceToken))
      return;
    await dbRun(
      pool,
      "UPDATE push_outbox SET attempts = attempts + 1 WHERE message_id = ?",
      [messageId],
    );
    try {
      let statusCode: number;
      if (isNativeDestination(destination)) {
        const result = await apnsSender!.send(destination, {
          pairId: row.pair_id,
          messageId,
          deviceId: row.device_id,
          seq: dbInteger(row.seq),
          createdAt: dbInteger(row.created_at),
          wechatUserId: dbInteger(row.wechat_user_id) as 0 | 999,
          replyCapable: dbInteger(row.reply_capable) === 1,
          conversationSendCapable:
            dbInteger(row.conversation_send_capable) === 1,
          previewEnvelope: JSON.parse(row.envelope_json) as PreviewEnvelope,
        });
        statusCode = result.statusCode;
        if (statusCode < 200 || statusCode >= 300)
          throw Object.assign(new Error("APNS_REJECTED"), {
            statusCode,
            invalidToken: result.invalidToken,
          });
      } else {
        statusCode = await deliverWebPush(
          {
            sessionId: row.session_id,
            pairId: row.pair_id,
            subscription: destination,
          },
          row.payload,
          messageId,
        );
      }
      await dbRun(
        pool,
        "UPDATE push_outbox SET sent_at = ? WHERE message_id = ?",
        [Date.now(), messageId],
      );
      event("ANDROID_PUSH_ACCEPTED", {
        messageId,
        responseCode: statusCode,
      });
    } catch (error) {
      const statusCode =
        typeof error === "object" && error && "statusCode" in error
          ? error.statusCode
          : undefined;
      const invalidNativeToken =
        isNativeDestination(destination) &&
        typeof error === "object" &&
        error !== null &&
        "invalidToken" in error &&
        error.invalidToken === true;
      if (invalidNativeToken)
        await clearNativeDeviceToken(
          row.session_id,
          destination.deviceToken!,
        );
      else if (!isNativeDestination(destination) && (statusCode === 404 || statusCode === 410))
        await invalidateBrowserSession(row.session_id);
      event("ANDROID_PUSH_FAILED", {
        messageId,
        responseCode: statusCode ?? null,
      });
    }
  } finally {
    outboxInFlight.delete(messageId);
  }
}
async function recoverOutbox(): Promise<void> {
  for (const row of await dbRows<{ message_id: string }>(
    pool,
    "SELECT message_id FROM push_outbox WHERE sent_at IS NULL",
  ))
    void sendOutbox(row.message_id);
}
async function sendPendingOutboxForPair(pairId: string): Promise<void> {
  for (const row of await dbRows<{ message_id: string }>(
    pool,
    "SELECT push_outbox.message_id FROM push_outbox JOIN messages ON messages.id = push_outbox.message_id JOIN devices ON devices.device_id = messages.device_id WHERE devices.pair_id = ? AND push_outbox.sent_at IS NULL",
    [pairId],
  ))
    void sendOutbox(row.message_id);
}
async function clearNativeDeviceToken(
  sessionId: string,
  rejectedToken: string,
): Promise<void> {
  const cleared = await transaction(async (connection) => {
    const row = await dbOne<{ subscription_envelope: string }>(
      connection,
      "SELECT subscription_envelope FROM browser_sessions WHERE session_id = ? AND invalidated_at IS NULL FOR UPDATE",
      [sessionId],
    );
    if (!row) return false;
    const destination = decryptSubscription(row.subscription_envelope);
    if (
      !isNativeDestination(destination) ||
      destination.deviceToken !== rejectedToken
    )
      return false;
    await dbRun(
      connection,
      "UPDATE browser_sessions SET subscription_envelope = ?, updated_at = ? WHERE session_id = ? AND invalidated_at IS NULL",
      [
        encryptSubscription({ ...destination, deviceToken: null }),
        Date.now(),
        sessionId,
      ],
    );
    return true;
  });
  if (cleared) event("IOS_PUSH_TOKEN_CLEARED", { sessionId });
}
async function sendPush(messageId: string): Promise<void> {
  const record = await loadMessage(messageId);
  const session = await loadBrowserSessionById(record.sessionId);
  if (!session || session.pairId !== record.pairId) {
    event("PUSH_FAILED", { messageId, responseCode: null });
    return;
  }
  record.serverSentAt = Date.now();
  record.attempts += 1;
  await saveMessage(record);
  try {
    const statusCode = await deliverWebPush(session, record.payload, messageId);
    record.pushServiceAcceptedAt = Date.now();
    record.responseCode = statusCode;
    await saveMessage(record);
    event("PUSH_SERVICE_ACCEPTED", {
      messageId,
      responseCode: statusCode,
    });
  } catch (error) {
    const statusCode =
      typeof error === "object" && error && "statusCode" in error
        ? error.statusCode
        : undefined;
    if (statusCode === 404 || statusCode === 410)
      await invalidateBrowserSession(session.sessionId);
    event("PUSH_FAILED", { messageId, responseCode: statusCode ?? null });
  }
}
async function deliverWebPush(
  session: BrowserSession,
  payload: string,
  messageId: string,
): Promise<number> {
  if (isNativeDestination(session.subscription))
    throw new Error("WEB_PUSH_SUBSCRIPTION_REQUIRED");
  const captureFile = process.env.TEST_PUSH_CAPTURE_FILE;
  if (process.env.NODE_ENV === "test" && captureFile) {
    await appendFile(
      captureFile,
      `${JSON.stringify({ sessionId: session.sessionId, pairId: session.pairId, messageId, payload: JSON.parse(payload) })}\n`,
      { encoding: "utf8", mode: 0o600 },
    );
    const goneSuffix = process.env.TEST_PUSH_GONE_ENDPOINT_SUFFIX;
    if (goneSuffix && session.subscription.endpoint.endsWith(goneSuffix))
      throw Object.assign(new Error("TEST_PUSH_GONE"), { statusCode: 410 });
    return 202;
  }
  const result = await webpush.sendNotification(session.subscription, payload, {
    TTL: 86_400,
    urgency: "high",
    topic: messageId.replaceAll("-", "").slice(0, 32),
  });
  return result.statusCode;
}
function testApnsTransport(): ApnsTransport | undefined {
  const captureFile = process.env.TEST_APNS_CAPTURE_FILE;
  if (process.env.NODE_ENV !== "test" || !captureFile) return undefined;
  return {
    async request(origin, headers, payload) {
      const path = String(headers[":path"] ?? "");
      await appendFile(
        captureFile,
        `${JSON.stringify({
          origin,
          topic: headers["apns-topic"],
          pushType: headers["apns-push-type"],
          expiration: headers["apns-expiration"],
          payload: JSON.parse(payload),
        })}\n`,
        { encoding: "utf8", mode: 0o600 },
      );
      const invalidToken = process.env.TEST_APNS_INVALID_TOKEN;
      if (invalidToken && path.endsWith(`/${invalidToken}`))
        return { statusCode: 410, reason: "Unregistered" };
      return { statusCode: 200 };
    },
  };
}
function schedulePush(messageId: string, delayMs: number): void {
  const timer = setTimeout(
    () => void sendPush(messageId),
    Math.max(0, delayMs),
  );
  timer.unref();
}
async function recoverScheduledMessages(): Promise<void> {
  for (const name of await readdir(messagesDir).catch(() => []))
    if (name.endsWith(".json")) {
      const record = await loadMessage(name.slice(0, -5)).catch(() => null);
      if (record?.sessionId && record.pairId && !record.pushServiceAcceptedAt)
        schedulePush(record.messageId, record.sendAfter - Date.now());
    }
}
function authorizeBrowser(request: IncomingMessage): void {
  const origin = request.headers.origin;
  if (origin !== undefined && origin !== publicOrigin)
    throw new Error("INVALID_ORIGIN");
  if (
    request.method !== "GET" &&
    request.method !== "HEAD" &&
    origin === undefined
  )
    throw new Error("INVALID_ORIGIN");
  authorizeToken(request.headers["x-awr-test-token"], testTokens);
}
async function authorizeBrowserPair(
  request: IncomingMessage,
  requireOrigin: boolean,
): Promise<{ pairId: string; sessionId: string }> {
  if (requireOrigin && request.headers.origin !== publicOrigin)
    throw new Error("INVALID_ORIGIN");
  if (!requireOrigin && request.headers.origin !== undefined && request.headers.origin !== publicOrigin)
    throw new Error("INVALID_ORIGIN");
  const session = await authorizeBrowserSession(request);
  const requestedPairId = request.headers["x-awr-pair-id"];
  if (
    typeof requestedPairId !== "string" ||
    !isUuid(requestedPairId) ||
    !session.pairId ||
    requestedPairId !== session.pairId
  )
    throw new Error("PAIRING_MISMATCH");
  return { pairId: requestedPairId, sessionId: session.sessionId };
}
async function authorizeNativeBrowserPair(
  request: IncomingMessage,
): Promise<{ pairId: string; sessionId: string }> {
  const session = await authorizeBrowserPair(request, true);
  const current = await loadBrowserSessionById(session.sessionId);
  if (!current || !isNativeDestination(current.subscription))
    throw new Error("INVALID_IOS_SESSION");
  return session;
}
async function authorizeBrowserSession(request: IncomingMessage): Promise<BrowserSession> {
  const candidate = request.headers["x-awr-ack-token"];
  if (typeof candidate !== "string") throw new Error("UNAUTHORIZED");
  const row = await dbOne<{
    session_id: string;
    subscription_envelope: string;
    pair_id: string | null;
  }>(
    pool,
    "SELECT session_id, subscription_envelope, pair_id FROM browser_sessions WHERE ack_token_hash = ? AND invalidated_at IS NULL",
    [tokenHash(candidate)],
  );
  if (!row) throw new Error("UNAUTHORIZED");
  return {
    sessionId: row.session_id,
    subscription: decryptSubscription(row.subscription_envelope),
    pairId: row.pair_id,
  };
}
async function loadBrowserSessionById(sessionId: string): Promise<BrowserSession | undefined> {
  const row = await dbOne<{
    session_id: string;
    subscription_envelope: string;
    pair_id: string | null;
  }>(
    pool,
    "SELECT session_id, subscription_envelope, pair_id FROM browser_sessions WHERE session_id = ? AND invalidated_at IS NULL",
    [sessionId],
  );
  return row
    ? {
        sessionId: row.session_id,
        subscription: decryptSubscription(row.subscription_envelope),
        pairId: row.pair_id,
      }
    : undefined;
}
async function invalidateBrowserSession(sessionId: string): Promise<void> {
  const now = Date.now();
  await dbRun(
    pool,
    "UPDATE browser_sessions SET invalidated_at = ?, updated_at = ? WHERE session_id = ? AND invalidated_at IS NULL",
    [now, now, sessionId],
  );
}
function authorizeToken(
  candidate: string | string[] | undefined,
  expected: readonly string[],
): void {
  if (typeof candidate !== "string") throw new Error("UNAUTHORIZED");
  let authorized = false;
  for (const token of expected)
    authorized = timingSafeBufferEqual(Buffer.from(candidate), Buffer.from(token)) || authorized;
  if (!authorized) throw new Error("UNAUTHORIZED");
}
function header(request: IncomingMessage, name: string): string {
  const value = request.headers[name];
  if (typeof value !== "string" || value.length > 1024)
    throw new Error("MISSING_HEADER");
  return value;
}
function validateSubscription(value: unknown): PushSubscription {
  if (!value || typeof value !== "object")
    throw new Error("INVALID_SUBSCRIPTION");
  const subscription = value as Partial<PushSubscription>;
  if (
    !subscription.endpoint?.startsWith("https://") ||
    !subscription.keys?.p256dh ||
    !subscription.keys.auth
  )
    throw new Error("INVALID_SUBSCRIPTION");
  return subscription as PushSubscription;
}
async function bodyJson(
  request: IncomingMessage,
  limit = maxBodyBytes,
): Promise<{ bytes: Buffer; json: unknown }> {
  const bytes = await bodyBytes(request, limit);
  try {
    return { bytes, json: JSON.parse(bytes.toString("utf8")) };
  } catch {
    throw new Error("INVALID_JSON");
  }
}
async function bodyBytes(
  request: IncomingMessage,
  limit = maxBodyBytes,
): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.from(chunk);
    size += buffer.length;
    if (size > limit) throw new Error("BODY_TOO_LARGE");
    chunks.push(buffer);
  }
  return Buffer.concat(chunks);
}
async function loadLegacySubscription(): Promise<StoredSubscription> {
  try {
    const wrapped = JSON.parse(await readFile(subscriptionFile, "utf8")) as {
      iv: string;
      tag: string;
      ct: string;
    };
    const decipher = createDecipheriv(
      "aes-256-gcm",
      storageKey,
      Buffer.from(wrapped.iv, "base64url"),
    );
    decipher.setAuthTag(Buffer.from(wrapped.tag, "base64url"));
    return JSON.parse(
      Buffer.concat([
        decipher.update(Buffer.from(wrapped.ct, "base64url")),
        decipher.final(),
      ]).toString("utf8"),
    ) as StoredSubscription;
  } catch {
    throw new Error("SUBSCRIPTION_MISSING");
  }
}
function encryptSubscription(value: PushSubscription | NativePushDestination): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", storageKey, iv);
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(value), "utf8"),
    cipher.final(),
  ]);
  return JSON.stringify({
      iv: iv.toString("base64url"),
      tag: cipher.getAuthTag().toString("base64url"),
      ct: ciphertext.toString("base64url"),
  });
}
function decryptSubscription(value: string): PushSubscription | NativePushDestination {
  try {
    const wrapped = JSON.parse(value) as { iv: string; tag: string; ct: string };
    const decipher = createDecipheriv(
      "aes-256-gcm",
      storageKey,
      Buffer.from(wrapped.iv, "base64url"),
    );
    decipher.setAuthTag(Buffer.from(wrapped.tag, "base64url"));
    const destination = JSON.parse(
      Buffer.concat([
        decipher.update(Buffer.from(wrapped.ct, "base64url")),
        decipher.final(),
      ]).toString("utf8"),
    ) as unknown;
    if (isNativeDestination(destination)) return destination;
    return validateSubscription(destination);
  } catch {
    throw new Error("SUBSCRIPTION_INVALID");
  }
}

function isNativeDestination(value: unknown): value is NativePushDestination {
  if (!value || typeof value !== "object") return false;
  const destination = value as Partial<NativePushDestination>;
  return (
    destination.kind === "apns" &&
    (destination.deviceToken === null ||
      (typeof destination.deviceToken === "string" &&
        /^[0-9a-f]{32,200}$/.test(destination.deviceToken) &&
        destination.deviceToken.length % 2 === 0)) &&
    (destination.environment === "sandbox" ||
      destination.environment === "production") &&
    typeof destination.previewEnabled === "boolean"
  );
}
async function importLegacySubscription(): Promise<void> {
  let legacy: StoredSubscription;
  try {
    legacy = await loadLegacySubscription();
  } catch {
    return;
  }
  if (!legacy.ackToken || !legacy.subscription) return;
  const now = Date.now();
  const result = await dbRun(
    pool,
    "INSERT IGNORE INTO browser_sessions(session_id, ack_token_hash, subscription_envelope, pair_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
    [
      randomUUID(),
      tokenHash(legacy.ackToken),
      encryptSubscription(legacy.subscription),
      legacy.activePairId ?? null,
      legacy.savedAt || now,
      now,
    ],
  );
  if (result.affectedRows === 1) event("LEGACY_SUBSCRIPTION_IMPORTED", {});
}
function messagePath(messageId: string): string {
  if (!/^[0-9a-f-]{36}$/i.test(messageId))
    throw new Error("INVALID_MESSAGE_ID");
  return join(messagesDir, `${messageId}.json`);
}
async function loadMessage(messageId: string): Promise<MessageRecord> {
  try {
    return JSON.parse(
      await readFile(messagePath(messageId), "utf8"),
    ) as MessageRecord;
  } catch {
    throw new Error("MESSAGE_NOT_FOUND");
  }
}
async function saveMessage(record: MessageRecord): Promise<void> {
  await atomicWrite(
    messagePath(record.messageId),
    JSON.stringify(record),
    0o600,
  );
}
async function recentMessages(sessionId: string): Promise<object[]> {
  const records: MessageRecord[] = [];
  for (const name of await readdir(messagesDir).catch(() => []))
    if (name.endsWith(".json")) {
      const record = await loadMessage(name.slice(0, -5)).catch(() => null);
      if (record?.sessionId === sessionId) records.push(record);
    }
  return records
    .sort((a, b) => b.queuedAt - a.queuedAt)
    .slice(0, 100)
    .map(({ payload: _payload, ...record }) => record);
}
async function staticFile(
  pathname: string,
  response: ServerResponse,
  headOnly: boolean,
): Promise<void> {
  const relative =
    pathname === "/"
      ? "index.html"
      : normalize(decodeURIComponent(pathname)).replace(/^\/+/, "");
  if (relative.startsWith("..")) throw new Error("INVALID_PATH");
  const path = join(publicDir, relative);
  try {
    const info = await stat(path);
    if (!info.isFile()) throw new Error();
    const content = await readFile(path);
    const types: Record<string, string> = {
      ".css": "text/css; charset=utf-8",
      ".html": "text/html; charset=utf-8",
      ".js": "text/javascript; charset=utf-8",
      ".json": "application/manifest+json",
      ".webmanifest": "application/manifest+json",
      ".svg": "image/svg+xml",
      ".png": "image/png",
    };
    response.statusCode = 200;
    response.setHeader(
      "Content-Type",
      types[extname(path)] ?? "application/octet-stream",
    );
    response.setHeader(
      "Cache-Control",
      relative === "index.html" || relative === "sw.js"
        ? "no-cache"
        : "public, max-age=300",
    );
    response.end(headOnly ? undefined : content);
  } catch {
    json(response, 404, { error: "NOT_FOUND" });
  }
}
function setSecurityHeaders(response: ServerResponse): void {
  response.setHeader(
    "Content-Security-Policy",
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; manifest-src 'self'; worker-src 'self'",
  );
  response.setHeader("Referrer-Policy", "no-referrer");
  response.setHeader("X-Content-Type-Options", "nosniff");
  response.setHeader("X-Frame-Options", "DENY");
  response.setHeader(
    "Permissions-Policy",
    "camera=(), microphone=(), geolocation=()",
  );
}
function json(
  response: ServerResponse,
  statusCode: number,
  body: unknown,
): void {
  if (!response.headersSent) {
    response.statusCode = statusCode;
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.end(JSON.stringify(body));
  }
}
function event(type: string, fields: Record<string, unknown>): void {
  void appendFile(
    eventsFile,
    `${JSON.stringify({ type, at: Date.now(), ...fields })}\n`,
    { encoding: "utf8", mode: 0o600 },
  ).catch(() => undefined);
}
async function recentEvents(sessionId: string, pairId: string): Promise<unknown[]> {
  try {
    return (await readFile(eventsFile, "utf8"))
      .trim()
      .split("\n")
      .slice(-100)
      .filter(Boolean)
      .map((line) => JSON.parse(line) as Record<string, unknown>)
      .filter((entry) => entry.sessionId === sessionId || entry.pairId === pairId);
  } catch {
    return [];
  }
}
async function atomicWrite(
  path: string,
  content: string,
  mode: number,
): Promise<void> {
  const temporary = `${path}.tmp`;
  await writeFile(temporary, content, { encoding: "utf8", mode });
  await rename(temporary, path);
}
function decodeBase64url(
  value: string,
  maxBytes: number,
  error: string,
  exactBytes?: number,
): Buffer {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error(error);
  let decoded: Buffer;
  try {
    decoded = Buffer.from(value, "base64url");
  } catch {
    throw new Error(error);
  }
  if (
    decoded.length === 0 ||
    decoded.length > maxBytes ||
    (exactBytes !== undefined && decoded.length !== exactBytes)
  )
    throw new Error(error);
  return decoded;
}
function secretHash(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}
function tokenHash(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}
function timingSafeBufferEqual(left: Buffer, right: Buffer): boolean {
  return left.length === right.length && timingSafeEqual(left, right);
}
function isUniqueConstraint(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    error.code === "ER_DUP_ENTRY"
  );
}
function isUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}
function isUuidV7(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
}
function isDeviceId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{16,128}$/.test(value);
}
function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}
