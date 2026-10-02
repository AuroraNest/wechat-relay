import type { PreviewEnvelope } from "./protocol.js";

export const NATIVE_ASSET_TTL_MS = 7 * 24 * 60 * 60 * 1000;
export const NATIVE_PAIR_QUOTA_BYTES = 256 * 1024 * 1024;
export const MAX_NATIVE_ASSET_BODY_BYTES = 12 * 1024 * 1024;
const MAX_ASSET_BYTES = 8 * 1024 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export interface NativeAsset {
  id: string;
  kind: "image" | "sticker" | "audio" | "video" | "file";
  mimeType: string;
  byteLength: number;
  role: "original" | "playback";
  derivedFrom?: string;
}

export interface NativeMessageContext {
  id: string;
  deviceId: string;
  seq: number;
  createdAt: number;
  wechatUserId: 0 | 999;
}

function object(value: unknown, error: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(error);
  return value as Record<string, unknown>;
}

function decoded(value: unknown, max: number, error: string): Buffer {
  if (typeof value !== "string" || value.length > Math.ceil(max * 4 / 3) || !/^[A-Za-z0-9_-]+$/.test(value))
    throw new Error(error);
  const bytes = Buffer.from(value, "base64url");
  if (!bytes.length || bytes.length > max || bytes.toString("base64url") !== value) throw new Error(error);
  return bytes;
}

function envelope(value: unknown, kid: string, aad: string, max: number, exact?: number): PreviewEnvelope {
  const e = object(value, "INVALID_NATIVE_ENVELOPE");
  if (Object.keys(e).sort().join(",") !== "aad,alg,ct,iv,kid" || e.alg !== "A256GCM" || e.kid !== kid || e.aad !== aad)
    throw new Error("INVALID_NATIVE_ENVELOPE");
  if (decoded(e.iv, 12, "INVALID_NATIVE_ENVELOPE").length !== 12) throw new Error("INVALID_NATIVE_ENVELOPE");
  const ciphertext = decoded(e.ct, max, "INVALID_NATIVE_ENVELOPE");
  if (ciphertext.length < 17 || (exact !== undefined && ciphertext.length !== exact)) throw new Error("INVALID_NATIVE_ENVELOPE");
  return { alg: "A256GCM", kid, aad, iv: e.iv as string, ct: e.ct as string };
}

export function validateNativeContent(value: unknown, context: NativeMessageContext): PreviewEnvelope {
  if (!UUID.test(context.id)) throw new Error("INVALID_MESSAGE");
  return envelope(value, "phase2-content", `AWR1|A2I_CONTENT|6|${context.id}|${context.deviceId}|${context.seq}|${context.createdAt}|${context.wechatUserId}`, 1024 * 1024 + 16);
}

export function validateNativePreview(value: unknown, context: NativeMessageContext): void {
  envelope(value, "phase1", `AWR1|A2I|${context.id}|${context.deviceId}|${context.seq}|${context.createdAt}|${context.wechatUserId}`, 600 + 16);
}

export function validateNativeAssets(value: unknown): NativeAsset[] {
  if (!Array.isArray(value) || value.length > 8) throw new Error("INVALID_NATIVE_ASSETS");
  const ids = new Set<string>();
  const assets = value.map((entry): NativeAsset => {
    const a = object(entry, "INVALID_NATIVE_ASSETS");
    const allowed = ["id", "kind", "mimeType", "byteLength", "role", "derivedFrom"];
    if (Object.keys(a).some((key) => !allowed.includes(key)) || typeof a.id !== "string" || !UUID.test(a.id) || ids.has(a.id) ||
        typeof a.kind !== "string" || !["image", "sticker", "audio", "video", "file"].includes(a.kind) ||
        typeof a.mimeType !== "string" || a.mimeType.length > 128 || !/^[a-zA-Z0-9!#$&^_.+-]+\/[a-zA-Z0-9!#$&^_.+-]+$/.test(a.mimeType) ||
        typeof a.byteLength !== "number" || !Number.isSafeInteger(a.byteLength) || a.byteLength < 1 || a.byteLength > MAX_ASSET_BYTES ||
        (a.role !== "original" && a.role !== "playback") ||
        (a.role === "original" && a.derivedFrom !== undefined) ||
        (a.role === "playback" && (typeof a.derivedFrom !== "string" || !UUID.test(a.derivedFrom))))
      throw new Error("INVALID_NATIVE_ASSETS");
    ids.add(a.id);
    return a as unknown as NativeAsset;
  });
  for (const asset of assets) {
    if (asset.role === "playback" && !assets.some((original) => original.id === asset.derivedFrom && original.role === "original" && original.kind === asset.kind))
      throw new Error("INVALID_NATIVE_ASSETS");
  }
  return assets;
}

export function validateNativeAssetUpload(value: unknown, context: NativeMessageContext, asset: NativeAsset): PreviewEnvelope {
  const body = object(value, "INVALID_NATIVE_ENVELOPE");
  if (Object.keys(body).join(",") !== "envelope") throw new Error("INVALID_NATIVE_ENVELOPE");
  const aad = `AWR1|A2I_ASSET|6|${context.id}|${asset.id}|${context.deviceId}|${context.seq}|${context.createdAt}|${context.wechatUserId}|${asset.kind}|${asset.mimeType}|${asset.byteLength}|${asset.role}|${asset.derivedFrom ?? ""}`;
  return envelope(body.envelope, "phase2-asset", aad, asset.byteLength + 16, asset.byteLength + 16);
}
