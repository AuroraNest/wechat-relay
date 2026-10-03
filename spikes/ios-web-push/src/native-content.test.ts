import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";
import { validateNativeAssetUpload, validateNativeAssets, validateNativeAssetSlots, validateNativeAssetMetadata, validateNativeContent, validateNativePreview, type NativeAsset } from "./native-content.js";

const context = { id: randomUUID(), deviceId: "native-device-0001", seq: 1, createdAt: 1788148800000, wechatUserId: 999 as const };
const contentAAD = `AWR1|A2I_CONTENT|6|${context.id}|${context.deviceId}|${context.seq}|${context.createdAt}|999`;
function sealed(kid: string, aad: string, bytes = 32) {
  return { alg: "A256GCM" as const, kid, aad, iv: Buffer.alloc(12).toString("base64url"), ct: Buffer.alloc(bytes).toString("base64url") };
}
const original: NativeAsset = { id: randomUUID(), kind: "audio", mimeType: "audio/silk", byteLength: 1024, role: "original" };
const playback: NativeAsset = { id: randomUUID(), kind: "audio", mimeType: "audio/wav", byteLength: 2048, role: "playback", derivedFrom: original.id };

test("native content accepts 1 MiB, rejects overflow and cross-profile AAD", () => {
  const value = sealed("phase2-content", contentAAD, 1024 * 1024 + 16);
  assert.deepEqual(validateNativeContent(value, context), value);
  assert.throws(() => validateNativeContent({ ...value, ct: Buffer.alloc(1024 * 1024 + 17).toString("base64url") }, context), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeContent(value, { ...context, wechatUserId: 0 }), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeContent({ ...value, plaintext: "forbidden" }, context), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeContent(value, { ...context, id: context.id.toUpperCase() }), /INVALID_MESSAGE/);
});

test("native preview preserves existing AAD with a 600-byte plaintext ceiling", () => {
  const value = sealed("phase1", `AWR1|A2I|${context.id}|${context.deviceId}|1|${context.createdAt}|999`, 616);
  validateNativePreview(value, context);
  assert.throws(() => validateNativePreview({ ...value, ct: Buffer.alloc(617).toString("base64url") }, context), /INVALID_NATIVE_ENVELOPE/);
});

test("native v7 content and assets cannot be replayed as v6", () => {
  const context7 = { ...context, nativeVersion: 7 };
  const content = sealed("phase2-content", contentAAD.replace("|6|", "|7|"));
  assert.deepEqual(validateNativeContent(content, context7), content);
  assert.throws(() => validateNativeContent(content, context), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeContent(content, { ...context, nativeVersion: 8, pairId: randomUUID() }), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeContent(content, { ...context, v: 7, nativeVersion: 6 }), /INVALID_NATIVE_VERSION/);
  const aad = `AWR1|A2I_ASSET|7|${context.id}|${original.id}|${context.deviceId}|1|${context.createdAt}|999|audio|audio/silk|1024|original|`;
  const asset = { envelope: sealed("phase2-asset", aad, 1040) };
  validateNativeAssetUpload(asset, context7, original);
  assert.throws(() => validateNativeAssetUpload(asset, context, original), /INVALID_NATIVE_ENVELOPE/);
});

test("v8 slots preserve unknown metadata and authenticate pair, version and resolution", () => {
  const slot = { id: original.id, kind: original.kind, role: original.role };
  const derived = { id: playback.id, kind: playback.kind, role: playback.role, derivedFrom: original.id };
  assert.deepEqual(validateNativeAssetSlots([slot, derived]), [slot, derived]);
  for (const value of [[original], [slot, slot], [derived], [{ ...slot, mimeType: "audio/silk" }], [{ ...slot, byteLength: 0 }]])
    assert.throws(() => validateNativeAssetSlots(value), /INVALID_NATIVE_ASSETS/);
  assert.deepEqual(validateNativeAssetMetadata(playback, derived), playback);
  assert.throws(() => validateNativeAssetMetadata({ ...original, kind: "file" }, slot), /NATIVE_ASSET_CONFLICT/);
  assert.throws(() => validateNativeAssetMetadata({ ...original, byteLength: 0 }, slot), /INVALID_NATIVE_ASSETS/);
  const context8 = { ...context, nativeVersion: 8, pairId: randomUUID() };
  const content = sealed("phase2-content", contentAAD.replace("|6|", `|8|${context8.pairId}|`));
  validateNativeContent(content, context8);
  for (const wrong of [{ ...context8, pairId: randomUUID() }, { ...context8, nativeVersion: 7 }, { ...context, nativeVersion: 8 }])
    assert.throws(() => validateNativeContent(content, wrong), /INVALID_NATIVE_ENVELOPE/);
  const aad = `AWR1|A2I_ASSET|8|${context8.pairId}|${context.id}|${playback.id}|${context.deviceId}|1|${context.createdAt}|999|audio|audio/wav|2048|playback|${original.id}`;
  const upload = { metadata: playback, envelope: sealed("phase2-asset", aad, playback.byteLength + 16) };
  validateNativeAssetUpload(upload, context8, playback);
  assert.throws(() => validateNativeAssetUpload(upload, { ...context8, pairId: randomUUID() }, playback), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeAssetUpload({ envelope: upload.envelope }, context8, playback), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeAssetUpload(upload, { ...context8, nativeVersion: 7 }, playback), /INVALID_NATIVE_ENVELOPE/);
});

test("native declarations require measurable bounded originals and separate playback provenance", () => {
  assert.deepEqual(validateNativeAssets([original, playback]), [original, playback]);
  assert.deepEqual(validateNativeAssets([]), []);
  for (const assets of [
    [original, original], [playback], [{ ...original, byteLength: 0 }], [{ ...original, byteLength: 8388609 }],
    [{ ...original, byteLength: 1.5 }], [{ ...original, name: "private filename" }],
    [{ ...original, derivedFrom: playback.id }], [original, { ...playback, kind: "video" }],
    Array.from({ length: 9 }, () => ({ ...original, id: randomUUID() })),
  ]) assert.throws(() => validateNativeAssets(assets), /INVALID_NATIVE_ASSETS/);
});

test("asset upload binds the entire immutable declaration and exact plaintext length", () => {
  const aad = `AWR1|A2I_ASSET|6|${context.id}|${playback.id}|${context.deviceId}|1|${context.createdAt}|999|audio|audio/wav|2048|playback|${original.id}`;
  const value = sealed("phase2-asset", aad, playback.byteLength + 16);
  assert.deepEqual(validateNativeAssetUpload({ envelope: value }, context, playback), value);
  for (const candidate of [
    { envelope: { ...value, ct: Buffer.alloc(2048 + 15).toString("base64url") } },
    { envelope: { ...value, iv: Buffer.alloc(11).toString("base64url") } },
    { envelope: { ...value, aad: aad.replace("audio/wav", "audio/mp4") } },
    { envelope: value, name: "private filename" },
  ]) assert.throws(() => validateNativeAssetUpload(candidate, context, playback), /INVALID_NATIVE_ENVELOPE/);
  assert.throws(() => validateNativeAssetUpload({ envelope: value }, { ...context, id: randomUUID() }, playback), /INVALID_NATIVE_ENVELOPE/);
});

test("native envelope base64url is canonical and rejects JSON type confusion", () => {
  const value = sealed("phase2-content", contentAAD, 17);
  for (const ct of [null, {}, [], 123, value.ct + "=", value.ct.slice(0, -1) + "B"])
    assert.throws(() => validateNativeContent({ ...value, ct }, context), /INVALID_NATIVE_ENVELOPE/);
});
