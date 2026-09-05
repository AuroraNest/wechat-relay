export const MAX_PUSH_BYTES = 2_800;

export interface PreviewEnvelope {
  alg: "A256GCM";
  kid: string;
  iv: string;
  ct: string;
  aad: string;
}

export interface TestPushRequest {
  messageId: string;
  previewEnvelope: PreviewEnvelope;
  delaySeconds?: number;
  context?: TestContext;
}

export interface TestContext {
  scenario: string;
  network: "wifi" | "cellular" | "transition" | "unknown";
  screenState: "foreground" | "background" | "killed" | "locked" | "unknown";
  iosVersion: string;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;

export function validateTestPushRequest(value: unknown): TestPushRequest {
  if (!value || typeof value !== "object") throw new Error("INVALID_REQUEST");
  const request = value as Partial<TestPushRequest>;
  const envelope = request.previewEnvelope;

  if (!request.messageId || !UUID_PATTERN.test(request.messageId)) throw new Error("INVALID_MESSAGE_ID");
  if (!envelope || envelope.alg !== "A256GCM") throw new Error("INVALID_ENVELOPE");
  if (!envelope.kid || envelope.kid.length > 64) throw new Error("INVALID_KID");
  if (!BASE64URL_PATTERN.test(envelope.iv) || !BASE64URL_PATTERN.test(envelope.ct)) {
    throw new Error("INVALID_CIPHERTEXT");
  }
  if (!envelope.aad.startsWith(`AWR1|A2I|${request.messageId}|`)) throw new Error("INVALID_AAD");

  const delaySeconds = request.delaySeconds ?? 0;
  if (!Number.isInteger(delaySeconds) || delaySeconds < 0 || delaySeconds > 86_400) {
    throw new Error("INVALID_DELAY");
  }

  const context = validateContext(request.context);
  return { messageId: request.messageId, previewEnvelope: envelope, delaySeconds, context };
}

function validateContext(value: TestContext | undefined): TestContext {
  const context = value ?? { scenario: "unspecified", network: "unknown", screenState: "unknown", iosVersion: "unknown" };
  const networks = new Set(["wifi", "cellular", "transition", "unknown"]);
  const screenStates = new Set(["foreground", "background", "killed", "locked", "unknown"]);
  if (!networks.has(context.network) || !screenStates.has(context.screenState)) throw new Error("INVALID_CONTEXT");
  if (!context.scenario || context.scenario.length > 80 || !context.iosVersion || context.iosVersion.length > 32) {
    throw new Error("INVALID_CONTEXT");
  }
  return context;
}

export function buildDeclarativePayload(origin: string, request: TestPushRequest): string {
  const payload = JSON.stringify({
    web_push: 8030,
    notification: {
      title: "工作微信",
      body: "收到一条工作微信测试消息, 点击查看",
      navigate: `${origin}/?messageId=${encodeURIComponent(request.messageId)}`,
      tag: `wechat:${request.messageId}`,
      data: {
        v: 1,
        messageId: request.messageId,
        previewEnvelope: request.previewEnvelope
      }
    },
    mutable: true
  });

  const bytes = Buffer.byteLength(payload);
  if (bytes > MAX_PUSH_BYTES) throw new Error(`PAYLOAD_TOO_LARGE:${bytes}`);
  return payload;
}
