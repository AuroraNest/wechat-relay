import { createPrivateKey, sign, type KeyObject } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  connect,
  constants,
  type ClientHttp2Session,
  type IncomingHttpHeaders,
  type OutgoingHttpHeaders,
} from "node:http2";
import type { PreviewEnvelope } from "./protocol.js";

export const DEFAULT_APNS_TOPIC = "com.auroramaple.wechatrelay";
export const MAX_APNS_PAYLOAD_BYTES = 4_096;
const APNS_TIMEOUT_MS = 10_000;
const APNS_RESPONSE_LIMIT = 8_192;
const APNS_TOKEN_MAX_AGE_SECONDS = 50 * 60;

export type ApnsEnvironment = "sandbox" | "production";

export interface NativePushDestination {
  kind: "apns";
  deviceToken: string | null;
  environment: ApnsEnvironment;
  previewEnabled: boolean;
}

export interface NativeRelayMessage {
  pairId: string;
  messageId: string;
  deviceId: string;
  seq: number;
  createdAt: number;
  wechatUserId: 0 | 999;
  replyCapable: boolean;
  conversationSendCapable: boolean;
  previewEnvelope: PreviewEnvelope;
}

export interface ApnsConfig {
  teamId: string;
  keyId: string;
  privateKey: KeyObject;
  topic: string;
}

export interface ApnsResponse {
  statusCode: number;
  reason?: string;
}

export interface ApnsTransport {
  request(
    origin: string,
    headers: OutgoingHttpHeaders,
    payload: string,
    timeoutMs: number,
  ): Promise<ApnsResponse>;
}

export interface ApnsDeliveryResult extends ApnsResponse {
  invalidToken: boolean;
}

export async function loadApnsConfig(
  environment: NodeJS.ProcessEnv = process.env,
): Promise<ApnsConfig | undefined> {
  const teamId = environment.APNS_TEAM_ID;
  const keyId = environment.APNS_KEY_ID;
  const privateKeyPath = environment.APNS_PRIVATE_KEY_PATH;
  const configured = [teamId, keyId, privateKeyPath].filter(Boolean).length;
  if (configured === 0) return undefined;
  if (configured !== 3) throw new Error("APNS_CONFIG_INCOMPLETE");
  if (!/^[A-Z0-9]{10}$/.test(teamId!) || !/^[A-Z0-9]{10}$/.test(keyId!))
    throw new Error("APNS_CONFIG_INVALID");
  const topic = environment.APNS_TOPIC ?? DEFAULT_APNS_TOPIC;
  if (!/^[A-Za-z0-9.-]{1,255}$/.test(topic))
    throw new Error("APNS_CONFIG_INVALID");
  const privateKey = createPrivateKey(await readFile(privateKeyPath!, "utf8"));
  if (
    privateKey.asymmetricKeyType !== "ec" ||
    privateKey.asymmetricKeyDetails?.namedCurve !== "prime256v1"
  )
    throw new Error("APNS_CONFIG_INVALID");
  return { teamId: teamId!, keyId: keyId!, privateKey, topic };
}

export function validateApnsEnvironment(value: unknown): ApnsEnvironment {
  if (value !== "sandbox" && value !== "production")
    throw new Error("INVALID_APNS_ENVIRONMENT");
  return value;
}

export function normalizeDeviceToken(value: unknown): string | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    !/^[0-9a-fA-F]{32,200}$/.test(value) ||
    value.length % 2 !== 0
  )
    throw new Error("INVALID_DEVICE_TOKEN");
  return value.toLowerCase();
}

export function buildNativePushPayload(
  message: NativeRelayMessage,
  previewEnabled: boolean,
): string {
  const actionable = message.replyCapable || message.conversationSendCapable;
  const payload = JSON.stringify({
    aps: {
      alert: { title: "工作微信", body: "收到新消息" },
      "mutable-content": 1,
      sound: "default",
      ...(actionable ? { category: "RELAY_MESSAGE" } : {}),
    },
    v: 1,
    pairId: message.pairId,
    messageId: message.messageId,
    deviceId: message.deviceId,
    seq: message.seq,
    createdAt: message.createdAt,
    wechatUserId: message.wechatUserId,
    replyCapable: message.replyCapable,
    conversationSendCapable: message.conversationSendCapable,
    previewEnabled,
    ...(previewEnabled ? { previewEnvelope: message.previewEnvelope } : {}),
  });
  if (Buffer.byteLength(payload) > MAX_APNS_PAYLOAD_BYTES)
    throw new Error("PAYLOAD_TOO_LARGE");
  return payload;
}

export class ApnsSender {
  private readonly transport: ApnsTransport;
  private cachedAuthorization?: { value: string; issuedAt: number };

  constructor(
    private readonly config: ApnsConfig,
    transport: ApnsTransport = new NodeHttp2ApnsTransport(),
  ) {
    this.transport = transport;
  }

  async send(
    destination: NativePushDestination,
    message: NativeRelayMessage,
    now = Date.now(),
  ): Promise<ApnsDeliveryResult> {
    if (!destination.deviceToken) throw new Error("APNS_DEVICE_TOKEN_MISSING");
    const payload = buildNativePushPayload(message, destination.previewEnabled);
    const status = await this.transport.request(
      apnsOrigin(destination.environment),
      {
        [constants.HTTP2_HEADER_METHOD]: "POST",
        [constants.HTTP2_HEADER_PATH]: `/3/device/${destination.deviceToken}`,
        authorization: `bearer ${this.authorization(now)}`,
        "apns-topic": this.config.topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": String(Math.floor(now / 1_000) + 86_400),
        "apns-collapse-id": message.messageId,
        "content-type": "application/json",
      },
      payload,
      APNS_TIMEOUT_MS,
    );
    return {
      ...status,
      invalidToken:
        status.statusCode === 410 ||
        status.reason === "BadDeviceToken" ||
        status.reason === "DeviceTokenNotForTopic" ||
        status.reason === "Unregistered",
    };
  }

  private authorization(now: number): string {
    const issuedAt = Math.floor(now / 1_000);
    if (
      this.cachedAuthorization &&
      issuedAt >= this.cachedAuthorization.issuedAt &&
      issuedAt - this.cachedAuthorization.issuedAt < APNS_TOKEN_MAX_AGE_SECONDS
    )
      return this.cachedAuthorization.value;
    const header = base64urlJson({ alg: "ES256", kid: this.config.keyId });
    const claims = base64urlJson({ iss: this.config.teamId, iat: issuedAt });
    const signingInput = `${header}.${claims}`;
    const signature = sign("sha256", Buffer.from(signingInput), {
      key: this.config.privateKey,
      dsaEncoding: "ieee-p1363",
    }).toString("base64url");
    const value = `${signingInput}.${signature}`;
    this.cachedAuthorization = { value, issuedAt };
    return value;
  }
}

export function apnsOrigin(environment: ApnsEnvironment): string {
  return environment === "production"
    ? "https://api.push.apple.com"
    : "https://api.sandbox.push.apple.com";
}

function base64urlJson(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

class NodeHttp2ApnsTransport implements ApnsTransport {
  private readonly sessions = new Map<string, ClientHttp2Session>();

  request(
    origin: string,
    headers: OutgoingHttpHeaders,
    payload: string,
    timeoutMs: number,
  ): Promise<ApnsResponse> {
    return new Promise((resolve, reject) => {
      const client = this.session(origin);
      let responseHeaders: IncomingHttpHeaders | undefined;
      let responseBody = "";
      let settled = false;
      const finish = (error?: Error, result?: ApnsResponse): void => {
        if (settled) return;
        settled = true;
        if (error) reject(error);
        else resolve(result!);
      };
      const request = client.request(headers);
      request.setEncoding("utf8");
      request.setTimeout(timeoutMs, () => {
        request.close(constants.NGHTTP2_CANCEL);
        finish(new Error("APNS_TIMEOUT"));
      });
      request.on("response", (value) => {
        responseHeaders = value;
      });
      request.on("data", (chunk: string) => {
        if (settled) return;
        if (
          Buffer.byteLength(responseBody) + Buffer.byteLength(chunk) >
          APNS_RESPONSE_LIMIT
        ) {
          request.close(constants.NGHTTP2_CANCEL);
          finish(new Error("APNS_RESPONSE_TOO_LARGE"));
          return;
        }
        responseBody += chunk;
      });
      request.once("error", (error) => finish(error));
      request.on("end", () => {
        const statusCode = Number(
          responseHeaders?.[constants.HTTP2_HEADER_STATUS] ?? 0,
        );
        let reason: string | undefined;
        if (responseBody) {
          try {
            const body = JSON.parse(responseBody) as { reason?: unknown };
            if (typeof body.reason === "string") reason = body.reason;
          } catch {
            // APNs failure bodies are optional and never logged.
          }
        }
        finish(undefined, reason ? { statusCode, reason } : { statusCode });
      });
      request.end(payload);
    });
  }

  private session(origin: string): ClientHttp2Session {
    const current = this.sessions.get(origin);
    if (current && !current.closed && !current.destroyed) return current;
    const client = connect(origin);
    this.sessions.set(origin, client);
    const discard = (): void => {
      if (this.sessions.get(origin) === client) this.sessions.delete(origin);
    };
    client.once("close", discard);
    client.on("error", discard);
    client.once("goaway", () => {
      discard();
      client.close();
    });
    client.setTimeout(60_000, () => client.close());
    client.unref();
    return client;
  }
}
