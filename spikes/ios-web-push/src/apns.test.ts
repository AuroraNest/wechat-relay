import assert from "node:assert/strict";
import { generateKeyPairSync, verify } from "node:crypto";
import type { OutgoingHttpHeaders } from "node:http2";
import test from "node:test";
import {
  ApnsSender,
  DEFAULT_APNS_TOPIC,
  MAX_APNS_PAYLOAD_BYTES,
  buildNativePushPayload,
  normalizeDeviceToken,
  type ApnsResponse,
  type ApnsTransport,
  type NativePushDestination,
  type NativeRelayMessage,
} from "./apns.js";

const message: NativeRelayMessage = {
  pairId: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0",
  messageId: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1",
  deviceId: "AAECAwQFBgcICQoLDA0ODw",
  seq: 7,
  createdAt: 1_788_148_800_000,
  wechatUserId: 999,
  replyCapable: true,
  conversationSendCapable: false,
  previewEnvelope: {
    alg: "A256GCM",
    kid: "phase1",
    iv: "AAECAwQFBgcICQoL",
    ct: "AAECAwQFBgcICQoLDA0ODw",
    aad: "AWR1|A2I|message|device|7|1788148800000|999",
  },
};

test("native APNs payload carries ciphertext metadata only when preview is enabled", () => {
  const enabled = JSON.parse(buildNativePushPayload(message, true)) as Record<string, unknown>;
  assert.ok(Buffer.byteLength(JSON.stringify(enabled)) <= MAX_APNS_PAYLOAD_BYTES);
  assert.deepEqual(enabled.aps, {
    alert: { title: "工作微信", body: "收到新消息" },
    "mutable-content": 1,
    sound: "default",
    category: "RELAY_MESSAGE",
  });
  assert.equal(enabled.previewEnabled, true);
  assert.deepEqual(enabled.previewEnvelope, message.previewEnvelope);
  assert.equal("assets" in enabled, false);

  const disabled = JSON.parse(buildNativePushPayload({
    ...message,
    replyCapable: false,
    conversationSendCapable: false,
  }, false)) as Record<string, unknown>;
  assert.deepEqual(disabled.aps, {
    alert: { title: "工作微信", body: "收到新消息" },
    "mutable-content": 1,
    sound: "default",
  });
  assert.equal(disabled.previewEnabled, false);
  assert.equal("previewEnvelope" in disabled, false);

  const conversationAction = JSON.parse(buildNativePushPayload({
    ...message,
    replyCapable: false,
    conversationSendCapable: true,
  }, false)) as { aps: { category?: string } };
  assert.equal(conversationAction.aps.category, "RELAY_MESSAGE");
  assert.throws(
    () => buildNativePushPayload({
      ...message,
      previewEnvelope: { ...message.previewEnvelope, ct: "A".repeat(4_096) },
    }, true),
    /PAYLOAD_TOO_LARGE/,
  );
});

test("APNs sender signs ES256 JWT and uses only allowlisted Apple origins", async () => {
  const keys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const transport = new CaptureTransport({ statusCode: 200 });
  const sender = new ApnsSender({
    teamId: "TEAMID1234",
    keyId: "KEYID12345",
    privateKey: keys.privateKey,
    topic: DEFAULT_APNS_TOPIC,
  }, transport);
  const destination: NativePushDestination = {
    kind: "apns",
    deviceToken: "a".repeat(64),
    environment: "sandbox",
    previewEnabled: false,
  };
  const now = 1_788_148_800_000;
  const result = await sender.send(destination, message, now);
  assert.equal(result.statusCode, 200);
  assert.equal(result.invalidToken, false);
  assert.equal(transport.origin, "https://api.sandbox.push.apple.com");
  assert.equal(transport.headers?.[":path"], `/3/device/${"a".repeat(64)}`);
  assert.equal(transport.headers?.["apns-topic"], DEFAULT_APNS_TOPIC);
  assert.equal(transport.headers?.["apns-push-type"], "alert");
  assert.equal(transport.headers?.["apns-priority"], "10");
  assert.equal(transport.headers?.["apns-expiration"], String(now / 1_000 + 86_400));

  const authorization = String(transport.headers?.authorization);
  const jwt = authorization.slice("bearer ".length);
  const [header, claims, signature] = jwt.split(".");
  assert.deepEqual(JSON.parse(Buffer.from(header!, "base64url").toString("utf8")), {
    alg: "ES256",
    kid: "KEYID12345",
  });
  assert.deepEqual(JSON.parse(Buffer.from(claims!, "base64url").toString("utf8")), {
    iss: "TEAMID1234",
    iat: now / 1_000,
  });
  assert.equal(verify("sha256", Buffer.from(`${header}.${claims}`), {
    key: keys.publicKey,
    dsaEncoding: "ieee-p1363",
  }, Buffer.from(signature!, "base64url")), true);
});

test("APNs invalid-token responses are distinguished from retryable failures", async () => {
  const keys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const sender = new ApnsSender({
    teamId: "TEAMID1234",
    keyId: "KEYID12345",
    privateKey: keys.privateKey,
    topic: DEFAULT_APNS_TOPIC,
  }, new CaptureTransport({ statusCode: 410, reason: "Unregistered" }));
  const result = await sender.send({
    kind: "apns",
    deviceToken: "b".repeat(64),
    environment: "production",
    previewEnabled: true,
  }, message);
  assert.equal(result.invalidToken, true);
});

test("APNs device tokens have one canonical representation", () => {
  assert.equal(normalizeDeviceToken("AB".repeat(32)), "ab".repeat(32));
  assert.equal(normalizeDeviceToken(null), null);
  assert.throws(() => normalizeDeviceToken("not-a-token"), /INVALID_DEVICE_TOKEN/);
});

class CaptureTransport implements ApnsTransport {
  origin?: string;
  headers?: OutgoingHttpHeaders;

  constructor(private readonly response: ApnsResponse) {}

  async request(
    origin: string,
    headers: OutgoingHttpHeaders,
    _payload: string,
    _timeoutMs: number,
  ): Promise<ApnsResponse> {
    this.origin = origin;
    this.headers = headers;
    return this.response;
  }
}
