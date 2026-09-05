const DB_NAME = "aurora-relay-phase0";
const STORE_NAME = "state";
const MESSAGE_STORE = "messages";
const PAIR_MESSAGE_STORE = "pairMessages";
const FALLBACK_TITLE = "工作微信";
const FALLBACK_BODY = "收到一条工作微信测试消息, 点击查看";

self.addEventListener("push", (event) => {
  event.waitUntil(handlePush(event));
});

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url ?? event.notification.navigate ?? "/";
  event.waitUntil((async () => {
    try {
      const navigation = notificationNavigation(url);
      if (event.notification.data?.pairId && navigation.pairId && event.notification.data.pairId !== navigation.pairId) throw new Error("PAIRING_MISMATCH");
      await assertActivePushPair(event.notification.data?.pairId ?? navigation.pairId);
    } catch {
      return clients.openWindow("/");
    }
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    if (windows[0]) {
      await windows[0].focus();
      return windows[0].navigate(url);
    }
    return clients.openWindow(url);
  })());
});

async function handlePush(event) {
  // ponytail: declarative notifications must remain UA-owned on Safari; only legacy data pushes use this path.
  if (event.notification) {
    try {
      const navigation = notificationNavigation(event.notification.data?.url ?? event.notification.navigate);
      const pairId = await assertActivePushPair(event.notification.data?.pairId ?? navigation.pairId);
      await notifyClients("INBOX_SYNC", pairId);
    } catch {}
    return;
  }
  let messageId = "unknown";
  try {
    if (await pairGet("forceFailure")) throw new Error("FORCED_SW_FAILURE");
    const payload = event.data.json();
    const data = payload.notification?.data;
    const navigation = notificationNavigation(data?.url ?? payload.notification?.navigate);
    if (data?.pairId && navigation.pairId && data.pairId !== navigation.pairId) throw new Error("PAIRING_MISMATCH");
    const pairId = await assertActivePushPair(data?.pairId ?? navigation.pairId);
    const wechatUserId = resolvePushProfile(data?.wechatUserId, navigation.wechatUserId);
    messageId = data?.messageId ?? navigation.messageId ?? messageId;
    const envelope = data?.previewEnvelope ?? navigation.previewEnvelope;
    if (!envelope) throw new Error("ENVELOPE_MISSING");
    assertEnvelopeBinding(messageId, envelope, wechatUserId);
    const key = await pairGet("messageKey", pairId);
    if (!(key instanceof CryptoKey)) throw new Error("PWA_KEY_MISSING");
    const plaintext = await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: base64urlBytes(envelope.iv),
        additionalData: new TextEncoder().encode(envelope.aad),
        tagLength: 128
      },
      key,
      base64urlBytes(envelope.ct)
    );
    const preview = JSON.parse(new TextDecoder().decode(plaintext));
    const message = {
      messageId,
      sender: String(preview.sender ?? FALLBACK_TITLE).slice(0, 120),
      body: String(preview.body ?? "").slice(0, 2000),
      wechatUserId,
      receivedAt: Date.now()
    };
    await storeMessage(message, pairId);
    await self.registration.showNotification(message.sender, {
      body: message.body,
      tag: `wechat:${messageId}`,
      data: { messageId, pairId, wechatUserId, url: navigation.url ?? `/?messageId=${encodeURIComponent(messageId)}` }
    });
    await acknowledge(messageId, "SW_PROCESSED", pairId);
  } catch {
    await self.registration.showNotification(FALLBACK_TITLE, {
      body: FALLBACK_BODY,
      tag: `wechat:${messageId}`,
      data: { messageId, url: "/" }
    });
  }
}

function assertEnvelopeBinding(messageId, envelope, wechatUserId) {
  const parts = typeof envelope?.aad === "string" ? envelope.aad.split("|") : [];
  const legacy = parts.length === 6 && parts[0] === "AWR1" && parts[1] === "A2I" && parts[2] === messageId && parts[3] && parts[4] && parts[5] && wechatUserId === 0;
  const profile = parts.length === 7 && parts[0] === "AWR1" && parts[1] === "A2I" && parts[2] === messageId && parts[3] && parts[4] && parts[5] && (parts[6] === "0" || parts[6] === "999") && parts[6] === String(wechatUserId);
  if (envelope?.alg !== "A256GCM" || envelope?.kid !== "phase1" || (!legacy && !profile)) {
    throw new Error("INVALID_ENVELOPE_BINDING");
  }
}

function notificationNavigation(navigate) {
  if (!navigate) return {};
  const url = new URL(navigate, self.location.origin);
  const fragment = new URLSearchParams(url.hash.slice(1));
  const serializedEnvelope = fragment.get("encryptedPreview") ?? fragment.get("previewEnvelope");
  return {
    messageId: fragment.get("messageId") ?? undefined,
    pairId: fragment.get("pairId") ?? undefined,
    wechatUserId: fragment.get("wechatUserId") ?? undefined,
    previewEnvelope: serializedEnvelope ? JSON.parse(serializedEnvelope) : undefined,
    url: url.toString()
  };
}

async function storeMessage(message, pairId) {
  await dbPutMessage(message, pairId);
  await notifyClients("INBOX_UPDATED", pairId);
}

async function notifyClients(type, pairId) {
  const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of windows) client.postMessage({ type, pairId });
}

async function acknowledge(messageId, type, pairId) {
  if (!messageId) return;
  const ackToken = await pairGet("ackToken", pairId);
  if (!ackToken || !pairId) return;
  await fetch("/api/acks", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...pairHeaders(ackToken, pairId) },
    body: JSON.stringify({ messageId, type })
  }).catch(() => undefined);
}

function base64urlBytes(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  return Uint8Array.from(atob((value + padding).replaceAll("-", "+").replaceAll("_", "/")), (c) => c.charCodeAt(0));
}

function pairStateKey(pairId, key) { return `pair:${pairId}:${key}`; }
function messageStorageId(pairId, messageId) { return `${pairId}:${messageId}`; }
function profileId(message) { return Number(message?.wechatUserId) === 999 ? 999 : 0; }
function parseWechatUserId(value) {
  if (value === undefined || value === null || value === "") return undefined;
  if (value === 0 || value === "0") return 0;
  if (value === 999 || value === "999") return 999;
  throw new Error("INVALID_PROFILE");
}
function resolvePushProfile(dataProfile, navigationProfile) {
  const fromData = parseWechatUserId(dataProfile);
  const fromNavigation = parseWechatUserId(navigationProfile);
  if (fromData !== undefined && fromNavigation !== undefined && fromData !== fromNavigation) throw new Error("PROFILE_MISMATCH");
  return fromData ?? fromNavigation ?? 0;
}
function pairHeaders(ackToken, pairId) {
  return ackToken && pairId ? { "X-AWR-Ack-Token": ackToken, "X-AWR-Pair-Id": pairId } : {};
}
async function activePairId() {
  const pairId = await dbGet("activePairId");
  return typeof pairId === "string" ? pairId : undefined;
}
async function pairGet(key, pairId) {
  const targetPairId = pairId ?? await activePairId();
  return targetPairId ? dbGet(pairStateKey(targetPairId, key)) : undefined;
}
async function assertActivePushPair(pairId) {
  const activePair = await activePairId();
  if (!pairId || pairId !== activePair) throw new Error("PAIRING_MISMATCH");
  return pairId;
}

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 3);
    request.onupgradeneeded = () => {
      const db = request.result;
      const state = db.objectStoreNames.contains(STORE_NAME) ? request.transaction.objectStore(STORE_NAME) : db.createObjectStore(STORE_NAME);
      const messages = db.objectStoreNames.contains(MESSAGE_STORE) ? request.transaction.objectStore(MESSAGE_STORE) : db.createObjectStore(MESSAGE_STORE, { keyPath: "messageId" });
      const pairMessages = db.objectStoreNames.contains(PAIR_MESSAGE_STORE) ? request.transaction.objectStore(PAIR_MESSAGE_STORE) : db.createObjectStore(PAIR_MESSAGE_STORE, { keyPath: "id" });
      if (request.oldVersion < 2) { const legacy = state.get("inbox"); legacy.onsuccess = () => { if (Array.isArray(legacy.result)) for (const item of legacy.result) if (item?.messageId) messages.put(item); state.delete("inbox"); }; }
      if (request.oldVersion < 3) {
        const active = state.get("activePairId");
        active.onsuccess = () => {
          const pairId = active.result;
          if (typeof pairId !== "string") return;
          for (const key of ["ackToken", "messageKey", "replyKey", "nextCursor", "seq", "lastPushRequest", "forceFailure"]) {
            const value = state.get(key);
            value.onsuccess = () => { if (value.result !== undefined) state.put(value.result, pairStateKey(pairId, key)); };
          }
          const legacyMessages = messages.getAll();
          legacyMessages.onsuccess = () => {
            for (const message of legacyMessages.result) {
              if (message?.messageId) pairMessages.put({ ...message, id: messageStorageId(pairId, message.messageId), pairId, wechatUserId: profileId(message) });
            }
          };
        };
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function dbPutMessage(message, pairId) {
  if (!pairId) throw new Error("PAIRING_MISSING");
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(PAIR_MESSAGE_STORE, "readwrite");
    transaction.objectStore(PAIR_MESSAGE_STORE).put({ ...message, id: messageStorageId(pairId, message.messageId), pairId, wechatUserId: profileId(message) });
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
}

async function dbGet(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const request = db.transaction(STORE_NAME).objectStore(STORE_NAME).get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function dbSet(key, value) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put(value, key);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
}
