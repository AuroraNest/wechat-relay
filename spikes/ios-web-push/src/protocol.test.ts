import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { classifyRequestError } from "./errors.js";
import { buildDeclarativePayload, MAX_PUSH_BYTES, validateTestPushRequest } from "./protocol.js";

const request = {
  messageId: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0",
  previewEnvelope: {
    alg: "A256GCM" as const,
    kid: "phase0",
    iv: "AAECAwQFBgcICQoL",
    ct: "AAECAwQFBgcICQoLDA0ODw",
    aad: "AWR1|A2I|019d2f1a-7b4c-7d10-8c21-1c77be6a91b0|phase0-ios|1|1788148800000"
  }
};

test("declarative payload stays within the internal budget", () => {
  const payload = buildDeclarativePayload("https://relay.example.com", request);
  assert.ok(Buffer.byteLength(payload) <= MAX_PUSH_BYTES);
  assert.equal(JSON.parse(payload).mutable, true);
});

test("message id and AAD must match", () => {
  assert.throws(
    () => validateTestPushRequest({ ...request, messageId: "wrong" }),
    /INVALID_MESSAGE_ID/
  );
});

test("request error classification exposes only intentional client errors", () => {
  assert.deepEqual(classifyRequestError(new Error("REPLY_UNSUPPORTED")), { status: 400, code: "REPLY_UNSUPPORTED" });
  assert.deepEqual(classifyRequestError(new Error("CONTACTS_SNAPSHOT_STALE")), { status: 409, code: "CONTACTS_SNAPSHOT_STALE" });
  assert.deepEqual(classifyRequestError(new Error("PAYLOAD_TOO_LARGE:2900")), { status: 400, code: "PAYLOAD_TOO_LARGE" });
  assert.deepEqual(classifyRequestError(new Error("ER_BAD_FIELD_ERROR: database detail")), { status: 500, code: "INTERNAL_ERROR" });
  assert.deepEqual(classifyRequestError(new Error("INVALID_DATABASE_INTEGER")), { status: 500, code: "INTERNAL_ERROR" });
  assert.deepEqual(classifyRequestError("database detail"), { status: 500, code: "INTERNAL_ERROR" });
});

test("AES-GCM vector decrypts with Web Crypto", async () => {
  const vectorUrl = new URL("../../../packages/crypto-test-vectors/aes-gcm-v1.json", import.meta.url);
  const vector = JSON.parse(await readFile(vectorUrl, "utf8")) as Record<string, string>;
  const decode = (value: string) => Buffer.from(value, "base64url");
  const key = await crypto.subtle.importKey("raw", decode(vector.key!), "AES-GCM", false, ["decrypt"]);
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: decode(vector.iv!), additionalData: Buffer.from(vector.aad!, "utf8") },
    key,
    decode(vector.ciphertext!)
  );
  assert.equal(Buffer.from(plaintext).toString("utf8"), vector.plaintext);
});

test("Android pairing keeps its Token and action in the default-open flow", async () => {
  const pageUrl = new URL("../public/index.html", import.meta.url);
  const page = await readFile(pageUrl, "utf8");
  const appUrl = new URL("../public/app.js", import.meta.url);
  const app = await readFile(appUrl, "utf8");
  const pairingSection = page.match(/<details class="settings-group disclosure-card connect-card" open>([\s\S]*?)<\/details>/)?.[1] ?? "";

  assert.match(pairingSection, /<summary>设备连接<\/summary>/);
  assert.match(pairingSection, /<input id="token"/);
  assert.match(pairingSection, /<button id="createPairing"/);
  assert.match(page, /href="\/style\.css\?v=relay-policy-1"/);
  assert.match(page, /src="\/app\.js\?v=relay-policy-1"/);
  assert.match(app, /elements\.token\.focus\(\)/);
  assert.match(app, /setPairingStatus\("loading"/);
  assert.match(page, /id="relayEnabled"/);
  assert.match(page, /id="relayScheduleEnabled"/);
  assert.match(page, /name="relayWeekday"[\s\S]*?value="1"[\s\S]*?checked/);
  assert.match(app, /api\("\/api\/v1\/relay-policy"/);
});

test("Nginx separates control, data, and long-lived protocol traffic", async () => {
  for (const name of ["relay.example.com.conf", "relay.example.com.bootstrap.conf"]) {
    const config = await readFile(new URL(`../../../deploy/phase0a/nginx/${name}`, import.meta.url), "utf8");
    assert.match(config, /zone=awr_control:10m rate=10r\/m/);
    assert.match(config, /zone=awr_data:10m rate=5r\/s/);
    assert.match(config, /limit_req_status 429/);
    assert.match(config, /return 429 '\{"error":"RATE_LIMITED"\}'/);
    assert.match(config, /location @control_rate_limited \{[\s\S]*?add_header Retry-After 6 always/);
    assert.match(config, /location @data_rate_limited \{[\s\S]*?add_header Retry-After 1 always/);
    assert.match(config, /location = \/api\/v1\/android\/messages \{[\s\S]*?client_max_body_size 24m;[\s\S]*?limit_req zone=awr_data burst=20 nodelay;/);
    for (const path of ["/api/v1/messages/stream", "/api/v1/ios/events", "/api/v1/android/replies", "/api/v1/android/replies/v4"]) {
      const location = config.match(new RegExp(`location = ${path.replaceAll("/", "\\/")} \\{([\\s\\S]*?)\\n    \\}`))?.[1] ?? "";
      assert.match(location, /limit_req zone=awr_data burst=20 nodelay/);
      assert.match(location, /limit_conn awr_long_lived 8/);
      assert.match(location, /proxy_buffering off/);
    }
  }
});

test("PWA preserves HTTP status and bounds reply polling with exponential backoff", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(app, /const REPLY_STATUS_DELAYS = \[0, 1000, 2000, 4000, 8000, 16000\]/);
  assert.doesNotMatch(app, /attempt < 15/);
  assert.match(app, /const status = response\.status;\s+const text = await response\.text\(\)/);
  assert.match(app, /status === 429 \|\| status >= 500/);
  assert.match(app, /const INBOX_STREAM_RETRY_DELAYS = \[1000, 5000, 15000\]/);
  assert.match(app, /if \(!response\.ok\) await responseJson\(response\)/);
  assert.match(app, /if \(!isRetryableError\(error\)\) throw error/);
  assert.match(app, /isRetryableError\(error\) && inboxStreamRetryCount < INBOX_STREAM_RETRY_DELAYS\.length/);
  assert.match(app, /replyStatus: permanentReplyFailure\(error\)/);
  assert.match(app, /await syncReplyStatus\(current\.messageId\);[\s\S]*?if \(isRetryableError\(error\)\) continue;[\s\S]*?delete current\.replyRequest/);
  assert.match(app, /incoming\.find\(\(message\) => message\.replyCapable === true && message\.conversationSendCapable === true\)/);
  assert.match(app, /发送到既有会话/);
  assert.match(app, /当前微信通知未提供快捷回复/);
});

test("message v4/v5 reply capability is persisted and defaults legacy messages to false", async () => {
  const server = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  const migration = await readFile(new URL("../scripts/migrations/003-message-reply-capability.sql", import.meta.url), "utf8");
  const android = await readFile(new URL("../../android-notification-probe/app/src/main/java/com/aurora/wechatrelay/probe/SyncNetwork.kt", import.meta.url), "utf8");
  assert.match(server, /message\.v === 4 \|\| message\.v === 5 \? message\.replyCapable : false/);
  assert.match(server, /throw new Error\("REPLY_UNSUPPORTED"\)/);
  assert.match(migration, /ADD COLUMN reply_capable TINYINT\(1\) NOT NULL DEFAULT 0/);
  assert.match(android, /\\"v\\":5/);
  assert.match(android, /\\"replyCapable\\":\$\{item\.replyCapable\}/);
});

test("message and reply transport remain ciphertext-only", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const server = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  const android = await readFile(new URL("../../android-notification-probe/app/src/main/java/com/aurora/wechatrelay/probe/SyncNetwork.kt", import.meta.url), "utf8");
  const queueReply = app.match(/async function queueReply\(target, body\) \{([\s\S]*?)\n\}\n\nasync function syncReplyStatuses/)?.[1] ?? "";
  const replyRequest = queueReply.match(/const request = \{([\s\S]*?)\n  \};/)?.[1] ?? "";

  assert.match(queueReply, /crypto\.subtle\.encrypt\([\s\S]*?JSON\.stringify\(\{ body, conversationTitle:/);
  assert.match(replyRequest, /replyEnvelope:/);
  assert.doesNotMatch(replyRequest, /^\s*body[,:]/m);
  assert.match(android, /\\"previewEnvelope\\":\$\{item\.envelope\}/);
  assert.match(server, /const envelopeJson = JSON\.stringify\(reply\.replyEnvelope\)/);
  assert.match(server, /JSON\.stringify\(message\.previewEnvelope\)/);
});

test("conversation send v3 binds mode, pair, device, target, timestamp, and profile", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const server = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  const android = await readFile(new URL("../../android-notification-probe/app/src/main/java/com/aurora/wechatrelay/probe/SyncProtocol.kt", import.meta.url), "utf8");
  const migration = await readFile(new URL("../scripts/migrations/004-conversation-send-capability.sql", import.meta.url), "utf8");

  for (const source of [app, server, android]) {
    assert.match(source, /AWR1\|I2A\|3\|CONVERSATION_SEND\|/);
  }
  assert.match(server, /reply\.v === 3 && dbInteger\(target\.conversation_send_capable\) !== 1/);
  assert.match(server, /messages\.id = \? AND messages\.device_id = \? AND devices\.pair_id = \?/);
  assert.match(migration, /conversation_send_capable TINYINT\(1\) NOT NULL DEFAULT 0/);
});

test("contact send v4 keeps the contact target and plaintext inside the reply envelope", async () => {
  const vectorUrl = new URL("../../../packages/crypto-test-vectors/contact-send-v4.json", import.meta.url);
  const vector = JSON.parse(await readFile(vectorUrl, "utf8")) as {
    version: number;
    key: string;
    plaintext: string;
    request: { targetContactSnapshotId: string; targetMessageId?: unknown; replyEnvelope: { aad: string; iv: string; ct: string } };
  };
  const key = await crypto.subtle.importKey("raw", Buffer.from(vector.key, "base64url"), "AES-GCM", false, ["decrypt"]);
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: Buffer.from(vector.request.replyEnvelope.iv, "base64url"),
      additionalData: Buffer.from(vector.request.replyEnvelope.aad, "utf8"),
    },
    key,
    Buffer.from(vector.request.replyEnvelope.ct, "base64url"),
  );
  const server = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  const migration = await readFile(new URL("../scripts/migrations/007-contact-send-replies.sql", import.meta.url), "utf8");

  assert.equal(vector.version, 4);
  assert.equal(vector.request.targetMessageId, undefined);
  assert.match(vector.request.replyEnvelope.aad, /^AWR1\|I2A\|4\|CONTACT_SEND\|/);
  assert.deepEqual(JSON.parse(Buffer.from(plaintext).toString("utf8")), JSON.parse(vector.plaintext));
  assert.match(vector.plaintext, /"body"/);
  assert.match(vector.plaintext, /"conversationTitle"/);
  assert.doesNotMatch(vector.plaintext, /contactName/);
  assert.match(server, /targetContactSnapshotId/);
  assert.match(server, /contact_snapshot_id IS NULL/);
  assert.match(server, /\/api\/v1\/android\/replies\/v4/);
  assert.match(migration, /MODIFY target_message_id .* NULL/);
  assert.match(migration, /ADD COLUMN contact_snapshot_id/);
  assert.match(migration, /replies_exactly_one_target/);
});

test("Accessibility gate schedules one guarded PIN selector chain", async () => {
  const service = await readFile(new URL("../../android-notification-probe/app/src/main/java/com/aurora/wechatrelay/probe/LockscreenAccessibilityReplyService.kt", import.meta.url), "utf8");
  const gateCallback = service.match(/fun onGateDismissRequested\(\) \{([\s\S]*?)\n        \}/)?.[1] ?? "";
  const eventHandler = service.match(/override fun onAccessibilityEvent\(event: AccessibilityEvent\?\) \{([\s\S]*?)\n    \}/)?.[1] ?? "";

  assert.doesNotMatch(service, /DEVICE_NOT_LOCKED/);
  assert.match(gateCallback, /active\.gateDismissRequested = true/);
  assert.match(eventHandler, /SystemUiPackage/);
  assert.ok(gateCallback.indexOf("active.gateDismissRequested = true") < gateCallback.indexOf("service.schedulePinTick(active)"));
  assert.match(service, /SystemUiPackage -> if \(active\.phase == Phase\.Unlocking\) schedulePinTick\(active\)/);
  assert.match(service, /if \(!handler\.hasCallbacks\(pinTick\)\) handler\.postDelayed\(pinTick, PinDigitDelayMillis\)/);
  assert.doesNotMatch(eventHandler, /Phase\.ReadyToClick/);
  assert.match(service, /active\.phase == Phase\.SelectingResult\) processWechatWindow\(active\)/);
});

test("Android pairing commits credentials before isolating old queue state", async () => {
  const store = await readFile(new URL("../../android-notification-probe/app/src/main/java/com/aurora/wechatrelay/probe/SyncStore.kt", import.meta.url), "utf8");
  const savePairing = store.match(/fun savePairing\([\s\S]*?\n    \}\n\n    private fun wrap/)?.[0] ?? "";

  assert.match(store, /data class SyncQueueItem\([\s\S]*?val deviceId: String/);
  assert.match(store, /SELECT id, deviceId, seq, createdAt, envelope, '' AS assetsJson, wechatUserId, replyCapable, conversationSendCapable, state FROM sync_queue WHERE deviceId = :deviceId/);
  assert.match(store, /SELECT substr\(assetsJson, :start, :count\) FROM sync_queue WHERE deviceId = :deviceId AND id = :id/);
  assert.match(store, /UPDATE sync_queue SET state = :state WHERE id = :id AND deviceId = :deviceId/);
  assert.ok(savePairing.indexOf(".commit()") < savePairing.indexOf("queue.clear(replacedDeviceId)"));
});

test("Android pairing always clears decoded keys and conditionally releases its gate", async () => {
  const network = await readFile(new URL("../../android-notification-probe/app/src/main/java/com/aurora/wechatrelay/probe/SyncNetwork.kt", import.meta.url), "utf8");
  const pair = network.match(/fun pair\(context: Context, code: PairingCode\): Boolean \{([\s\S]*?)\n    \}\n\n    fun enqueue/)?.[1] ?? "";

  assert.match(pair, /finally \{\s+if \(acquired\) pairingGate\.release\(\)\s+clearPairingKeys\(code\)\s+\}$/);
});

test("replacement pairing creates the new server pair before updating its subscription", async () => {
  const appUrl = new URL("../public/app.js", import.meta.url);
  const app = await readFile(appUrl, "utf8");
  const pairing = app.match(/async function createPairing\(\) \{([\s\S]*?)\n\}\n\nasync function copyPairing/)?.[1] ?? "";

  assert.match(pairing, /const subscription = await ensurePushSubscription\(\)/);
  assert.doesNotMatch(pairing, /await subscribe\(\)/);
  assert.match(pairing, /if \(previousPairId\) \{\s+assertFeature\(ackToken, "ACK_TOKEN_MISSING"\)/);
  assert.ok(pairing.indexOf('assertFeature(ackToken, "ACK_TOKEN_MISSING")') < pairing.indexOf("await updateSubscription(subscription)"));
  assert.match(pairing, /headers: \{ "X-AWR-Ack-Token": ackToken \}/);
  assert.ok(pairing.indexOf('api("/api/v1/pairings"') < pairing.indexOf("await updateSubscription(subscription, ackToken, pairing.pairId)"));
  assert.ok(pairing.indexOf("elements.pairingCode.hidden = false") < pairing.indexOf("await updateSubscription(subscription, ackToken, pairing.pairId)"));
});
