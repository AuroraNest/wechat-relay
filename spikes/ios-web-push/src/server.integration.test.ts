import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import {
  createCipheriv,
  createDecipheriv,
  createHash,
  generateKeyPairSync,
  randomBytes,
  randomUUID,
  sign,
  type KeyObject,
} from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import mysql, { type Connection } from "mysql2/promise";
import webpush from "web-push";
import type { NativeAsset, NativeAssetSlot } from "./native-content.js";

const adminUser = process.env.MYSQL_TEST_ADMIN_USER;
const port = Number(process.env.AWR_TEST_PORT ?? 18_081);
const origin = `http://0.0.0.0:${port}`;
const testToken = "integration-test-token";
const secondTestToken = "integration-test-token-b";
const storageKey = Buffer.alloc(32, 7);
const invalidApnsToken = "f".repeat(64);

test("MySQL backend isolates browser sessions, pairs, profiles, SSE, replies, acks, and Push", { skip: !adminUser }, async (context) => {
  const database = `awr_test_${randomBytes(8).toString("hex")}`;
  const dataDir = await mkdtemp(join(tmpdir(), "awr-mysql-"));
  const captureFile = join(dataDir, "push-capture.jsonl");
  const apnsCaptureFile = join(dataDir, "apns-capture.jsonl");
  const apnsKeyPath = join(dataDir, "AuthKey_TEST.p8");
  const apnsKeys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  await writeFile(
    apnsKeyPath,
    apnsKeys.privateKey.export({ type: "pkcs8", format: "pem" }),
    { mode: 0o600 },
  );
  const admin = await mysql.createConnection({
    host: process.env.MYSQL_TEST_ADMIN_HOST ?? "127.0.0.1",
    port: Number(process.env.MYSQL_TEST_ADMIN_PORT ?? 3306),
    user: adminUser!,
    password: process.env.MYSQL_TEST_ADMIN_PASSWORD ?? "",
    multipleStatements: true,
  });
  let server: ChildProcess | undefined;
  context.after(async () => {
    if (server) await stop(server);
    await admin.query(`DROP DATABASE IF EXISTS \`${database}\``);
    await admin.end();
    await rm(dataDir, { recursive: true, force: true });
  });

  await admin.query(`CREATE DATABASE \`${database}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin`);
  await applyMigrations(admin, database);
  const db = await mysql.createConnection({
    host: process.env.MYSQL_TEST_ADMIN_HOST ?? "127.0.0.1",
    port: Number(process.env.MYSQL_TEST_ADMIN_PORT ?? 3306),
    user: adminUser!,
    password: process.env.MYSQL_TEST_ADMIN_PASSWORD ?? "",
    database,
  });
  context.after(() => db.end());

  const legacyPairId = randomUUID();
  const legacyAckToken = randomBytes(32).toString("base64url");
  await db.execute(
    "INSERT INTO pairings(pair_id, secret_hash, expires_at, consumed_at) VALUES (?, ?, ?, ?)",
    [legacyPairId, randomBytes(32), Date.now() + 600_000, Date.now()],
  );
  await writeLegacySubscription(dataDir, legacyAckToken, legacyPairId);

  server = startServer(database, dataDir, captureFile, apnsCaptureFile, apnsKeyPath);
  await waitUntilReady(server);
  assert.equal((await fetch(`${origin}/healthz`)).status, 200);
  assert.equal((await fetch(`${origin}/api/status`, {
    headers: { ...browserHeaders(legacyAckToken, legacyPairId), "X-AWR-Test-Token": testToken },
  })).status, 200);
  assert.ok(await readFile(join(dataDir, "subscription.json")));
  const [[legacyCount]] = await db.query<({ count: number } & mysql.RowDataPacket)[]>(
    "SELECT COUNT(*) AS count FROM browser_sessions WHERE ack_token_hash = ? AND pair_id = ?",
    [createHash("sha256").update(legacyAckToken).digest(), legacyPairId],
  );
  assert.equal(Number(legacyCount?.count), 1);

  const sessionA = await createSubscription("a");
  const sessionB = await createSubscription("b", secondTestToken);
  assert.notEqual(sessionA, sessionB);
  const a = await pairDevice(sessionA);
  const b = await pairDevice(sessionB, secondTestToken);
  assert.notEqual(a.pairId, b.pairId);
  assert.notEqual(a.deviceId, b.deviceId);
  await assert.rejects(
    db.execute(
      "INSERT INTO replies(id, pair_id, target_message_id, contact_snapshot_id, device_id, wechat_user_id, created_at, envelope_json, status, status_at, queued_at) VALUES (?, ?, NULL, NULL, ?, 0, ?, '{}', 'QUEUED', ?, ?)",
      [randomUUID(), a.pairId, a.deviceId, Date.now(), Date.now(), Date.now()],
    ),
  );
  await assert.rejects(
    db.execute(
      "INSERT INTO replies(id, pair_id, target_message_id, contact_snapshot_id, device_id, wechat_user_id, created_at, envelope_json, status, status_at, queued_at) VALUES (?, ?, ?, NULL, ?, 0, ?, '{}', 'QUEUED', ?, ?)",
      [randomUUID(), a.pairId, randomUUID(), a.deviceId, Date.now(), Date.now(), Date.now()],
    ),
  );

  const missingIosOrigin = await fetch(`${origin}/api/v1/ios/sessions`, {
    method: "POST",
    headers: {
      "X-AWR-Test-Token": testToken,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  assert.equal(missingIosOrigin.status, 400);
  assert.deepEqual(await missingIosOrigin.json(), { error: "INVALID_ORIGIN" });
  const nativeSession = await createIosSession();
  const native = await pairDevice(nativeSession);
  const initialNativeStatus = await iosStatus(nativeSession, native.pairId);
  assert.equal(initialNativeStatus.paired, true);
  assert.equal(initialNativeStatus.deviceId, native.deviceId);
  assert.equal(initialNativeStatus.lastSeenAt, null);
  assert.equal(initialNativeStatus.pushConfigured, true);
  assert.equal(initialNativeStatus.pushRegistered, false);
  const missingIosEventsOrigin = await fetch(`${origin}/api/v1/ios/events?afterSeq=0`, {
    headers: browserHeaders(nativeSession, native.pairId),
  });
  assert.equal(missingIosEventsOrigin.status, 400);
  assert.deepEqual(await missingIosEventsOrigin.json(), { error: "INVALID_ORIGIN" });
  const pwaIosEvents = await fetch(`${origin}/api/v1/ios/events?afterSeq=0`, {
    headers: browserHeaders(sessionA, a.pairId, true),
  });
  assert.equal(pwaIosEvents.status, 400);
  assert.deepEqual(await pwaIosEvents.json(), { error: "INVALID_IOS_SESSION" });
  const nativeMessage = await uploadMessage(native, {
    v: 5,
    seq: 1,
    wechatUserId: 999,
    replyCapable: true,
    conversationSendCapable: true,
  });
  const initialEvents = await openIosEvents(nativeSession, native.pairId, 0);
  assert.deepEqual(await initialEvents.next(), { event: "ready", data: "1" });
  await initialEvents.close();

  const nativeSessionB = await createIosSession(secondTestToken);
  const nativeB = await pairDevice(nativeSessionB, secondTestToken);
  const nativeReplyEvents = await openIosEvents(nativeSession, native.pairId, 1);
  const nativeBReplyEvents = await openIosEvents(nativeSessionB, nativeB.pairId, 0);
  assert.deepEqual(await nativeReplyEvents.next(), { event: "ready", data: "1" });
  assert.deepEqual(await nativeBReplyEvents.next(), { event: "ready", data: "0" });
  const nativeReplyId = randomUUID();
  const nativeReply = await submitReply(nativeSession, native, nativeMessage.id, 2, 999, nativeReplyId);
  assert.deepEqual(nativeReply, { replyId: nativeReplyId, status: "QUEUED" });
  assert.deepEqual(await nativeReplyEvents.next(), { event: "reply", data: nativeReplyId });
  assert.equal(await Promise.race([
    nativeBReplyEvents.next().then(() => "data"),
    delay(150).then(() => "timeout"),
  ]), "timeout");
  const nativeClaim = await fetch(`${origin}/api/v1/android/replies`, {
    headers: signedHeaders(native, "", "GET", "/api/v1/android/replies"),
  });
  await assertResponseStatus(nativeClaim, 200);
  assert.deepEqual(await nativeReplyEvents.next(), { event: "reply", data: nativeReplyId });
  await acknowledgeReply(native, nativeReplyId, "SENT_TO_WECHAT");
  assert.deepEqual(await nativeReplyEvents.next(), { event: "reply", data: nativeReplyId });
  await nativeReplyEvents.close();
  await nativeBReplyEvents.close();

  const contactSnapshot = contactsSnapshot(nativeB, 0, Date.now(), 2);
  const contactSnapshotBody = JSON.stringify(contactSnapshot);
  await assertResponseStatus(
    await postContacts(contactSnapshotBody, signedHeaders(nativeB, contactSnapshotBody, "POST", "/api/v1/android/contacts")),
    201,
  );
  const [[nativeBMessageCount]] = await db.query<(mysql.RowDataPacket & { count: string | number })[]>(
    "SELECT COUNT(*) AS count FROM messages JOIN devices ON devices.device_id = messages.device_id WHERE devices.pair_id = ?",
    [nativeB.pairId],
  );
  assert.equal(Number(nativeBMessageCount?.count), 0);
  const nativeBContacts = await fetch(`${origin}/api/v1/ios/contacts`, {
    headers: browserHeaders(nativeSessionB, nativeB.pairId),
  });
  await assertResponseStatus(nativeBContacts, 200);
  assert.deepEqual(
    (await nativeBContacts.json() as { snapshots: Array<{ id: string; v: number }> }).snapshots.map((snapshot) => ({ id: snapshot.id, v: snapshot.v })),
    [{ id: contactSnapshot.id, v: 2 }],
  );

  const contactReplyEvents = await openIosEvents(nativeSessionB, nativeB.pairId, 0);
  assert.deepEqual(await contactReplyEvents.next(), { event: "ready", data: "0" });
  const legacyPoll = fetch(`${origin}/api/v1/android/replies`, {
    headers: signedHeaders(nativeB, "", "GET", "/api/v1/android/replies"),
  });
  await delay(50);
  const contactReplyId = randomUUID();
  const contactReplyCreatedAt = Date.now();
  const contactReplyEnvelope = {
    alg: "A256GCM" as const,
    kid: "phase1-reply" as const,
    iv: randomBytes(12).toString("base64url"),
    aad: `AWR1|I2A|4|CONTACT_SEND|${nativeB.pairId}|${contactReplyId}|${nativeB.deviceId}|${contactSnapshot.id}|${contactReplyCreatedAt}|0`,
    ct: randomBytes(32).toString("base64url"),
  };
  const contactReply = await submitContactReply(
    nativeSessionB,
    nativeB.pairId,
    nativeB,
    contactSnapshot.id,
    0,
    contactReplyId,
    contactReplyCreatedAt,
    contactReplyEnvelope,
  );
  assert.deepEqual(contactReply, { replyId: contactReplyId, status: "QUEUED" });
  assert.deepEqual(await contactReplyEvents.next(), { event: "reply", data: contactReplyId });
  const legacyPollBody = await (await legacyPoll).json() as { reply: null };
  assert.equal(legacyPollBody.reply, null);
  const v4Poll = await fetch(`${origin}/api/v1/android/replies/v4`, {
    headers: signedHeaders(nativeB, "", "GET", "/api/v1/android/replies/v4"),
  });
  await assertResponseStatus(v4Poll, 200);
  const v4Claim = await v4Poll.json() as { reply: Record<string, unknown> };
  assert.deepEqual(
    {
      v: v4Claim.reply.v,
      id: v4Claim.reply.id,
      pairId: v4Claim.reply.pairId,
      targetContactSnapshotId: v4Claim.reply.targetContactSnapshotId,
      deviceId: v4Claim.reply.deviceId,
      wechatUserId: v4Claim.reply.wechatUserId,
    },
    {
      v: 4,
      id: contactReplyId,
      pairId: nativeB.pairId,
      targetContactSnapshotId: contactSnapshot.id,
      deviceId: nativeB.deviceId,
      wechatUserId: 0,
    },
  );
  assert.equal("targetMessageId" in v4Claim.reply, false);
  assert.deepEqual(await contactReplyEvents.next(), { event: "reply", data: contactReplyId });
  await acknowledgeReply(nativeB, contactReplyId, "CONTACT_SNAPSHOT_STALE");
  assert.deepEqual(await contactReplyEvents.next(), { event: "reply", data: contactReplyId });
  const contactReplyStatus = await browserJson<Record<string, unknown>>(
    `/api/v1/replies/${contactReplyId}`,
    nativeSessionB,
    nativeB.pairId,
  );
  assert.equal(contactReplyStatus.targetContactSnapshotId, contactSnapshot.id);
  assert.equal("targetMessageId" in contactReplyStatus, false);
  await contactReplyEvents.close();

  for (const status of ["AUTOMATION_NOT_READY", "WECHAT_WINDOW_TIMEOUT"]) {
    const failedReply = await submitContactReply(nativeSessionB, nativeB.pairId, nativeB, contactSnapshot.id, 0) as { replyId: string };
    await acknowledgeReply(nativeB, failedReply.replyId, status);
    const result = await browserJson<Record<string, unknown>>(`/api/v1/replies/${failedReply.replyId}`, nativeSessionB, nativeB.pairId);
    assert.equal(result.status, status);
  }

  const replacementContactSnapshot = contactsSnapshot(nativeB, 0, Date.now() + 1, 2);
  const replacementContactBody = JSON.stringify(replacementContactSnapshot);
  await assertResponseStatus(
    await postContacts(replacementContactBody, signedHeaders(nativeB, replacementContactBody, "POST", "/api/v1/android/contacts")),
    201,
  );
  assert.deepEqual(
    await submitContactReply(
      nativeSessionB,
      nativeB.pairId,
      nativeB,
      contactSnapshot.id,
      0,
      contactReplyId,
      contactReplyCreatedAt,
      contactReplyEnvelope,
    ),
    { replyId: contactReplyId, status: "CONTACT_SNAPSHOT_STALE" },
  );
  assert.deepEqual(
    await submitContactReply(nativeSessionB, nativeB.pairId, nativeB, contactSnapshot.id, 0),
    { error: "TARGET_CONTACT_SNAPSHOT_NOT_FOUND" },
  );
  const v1ContactSnapshot = contactsSnapshot(nativeB, 999, Date.now(), 1);
  const v1ContactBody = JSON.stringify(v1ContactSnapshot);
  await assertResponseStatus(
    await postContacts(v1ContactBody, signedHeaders(nativeB, v1ContactBody, "POST", "/api/v1/android/contacts")),
    201,
  );
  assert.deepEqual(
    await submitContactReply(nativeSessionB, nativeB.pairId, nativeB, v1ContactSnapshot.id, 999),
    { error: "TARGET_CONTACT_SNAPSHOT_NOT_FOUND" },
  );
  assert.deepEqual(
    await submitContactReply(nativeSessionB, nativeB.pairId, nativeB, replacementContactSnapshot.id, 999),
    { error: "TARGET_CONTACT_SNAPSHOT_NOT_FOUND" },
  );
  assert.deepEqual(
    await submitContactReply(nativeSession, native.pairId, nativeB, replacementContactSnapshot.id, 0),
    { error: "TARGET_CONTACT_SNAPSHOT_NOT_FOUND" },
  );
  const [[pendingNativeOutbox]] = await db.query<(mysql.RowDataPacket & { attempts: string | number; sent_at: string | number | null })[]>(
    "SELECT attempts, sent_at FROM push_outbox WHERE message_id = ?",
    [nativeMessage.id],
  );
  assert.equal(Number(pendingNativeOutbox?.attempts), 0);
  assert.equal(pendingNativeOutbox?.sent_at, null);
  await updateIosPush(nativeSession, native.pairId, "a".repeat(64), "production", false);
  await waitFor(async () =>
    (await apnsCaptureLines(apnsCaptureFile)).some(
      (entry) => entry.payload.messageId === nativeMessage.id,
    ),
  );
  const nativeCapture = (await apnsCaptureLines(apnsCaptureFile)).find(
    (entry) => entry.payload.messageId === nativeMessage.id,
  )!;
  assert.equal(nativeCapture.origin, "https://api.push.apple.com");
  assert.equal(nativeCapture.topic, "com.auroramaple.wechatrelay");
  assert.equal(nativeCapture.pushType, "alert");
  assert.equal(nativeCapture.payload.previewEnabled, false);
  assert.equal("previewEnvelope" in nativeCapture.payload, false);
  assert.equal((nativeCapture.payload.aps as Record<string, unknown>).category, "RELAY_MESSAGE");
  const activeNativeStatus = await iosStatus(nativeSession, native.pairId);
  assert.equal(activeNativeStatus.pushRegistered, true);
  assert.equal(typeof activeNativeStatus.lastSeenAt, "number");

  const offlineCapturedAt = Date.now() - 3 * 86_400_000;
  const contacts0 = contactsSnapshot(native, 0, offlineCapturedAt);
  const contacts0Body = JSON.stringify(contacts0);
  const contacts0Headers = signedHeaders(native, contacts0Body, "POST", "/api/v1/android/contacts");
  const firstContacts = await postContacts(contacts0Body, contacts0Headers);
  assert.equal(firstContacts.status, 201);
  assert.deepEqual(await firstContacts.json(), { id: contacts0.id, idempotent: false });
  const replayedContacts = await postContacts(contacts0Body, contacts0Headers);
  assert.equal(replayedContacts.status, 400);
  assert.deepEqual(await replayedContacts.json(), { error: "REPLAYED_NONCE" });
  const retryContacts = await postContacts(
    contacts0Body,
    signedHeaders(native, contacts0Body, "POST", "/api/v1/android/contacts"),
  );
  assert.equal(retryContacts.status, 200);
  assert.deepEqual(await retryContacts.json(), { id: contacts0.id, idempotent: true });

  const newerContacts0 = contactsSnapshot(native, 0, offlineCapturedAt + 1);
  const newerContacts0Body = JSON.stringify(newerContacts0);
  await assertResponseStatus(
    await postContacts(newerContacts0Body, signedHeaders(native, newerContacts0Body, "POST", "/api/v1/android/contacts")),
    201,
  );
  const staleContacts = contactsSnapshot(native, 0, offlineCapturedAt);
  const staleContactsBody = JSON.stringify(staleContacts);
  const staleContactsResponse = await postContacts(staleContactsBody, signedHeaders(native, staleContactsBody, "POST", "/api/v1/android/contacts"));
  assert.equal(staleContactsResponse.status, 409);
  assert.deepEqual(await staleContactsResponse.json(), { error: "CONTACTS_SNAPSHOT_STALE" });

  const contacts999 = contactsSnapshot(native, 999, Date.now());
  const contacts999Body = JSON.stringify(contacts999);
  await assertResponseStatus(
    await postContacts(contacts999Body, signedHeaders(native, contacts999Body, "POST", "/api/v1/android/contacts")),
    201,
  );
  const conflictingId = contactsSnapshot(native, 0, Date.now());
  conflictingId.id = contacts999.id;
  conflictingId.contactsEnvelope.aad = `AWR1|A2I_CONTACTS|1|${conflictingId.id}|${native.deviceId}|${conflictingId.capturedAt}|${conflictingId.wechatUserId}`;
  const conflictingIdBody = JSON.stringify(conflictingId);
  const conflictingIdResponse = await postContacts(conflictingIdBody, signedHeaders(native, conflictingIdBody, "POST", "/api/v1/android/contacts"));
  assert.equal(conflictingIdResponse.status, 409);
  assert.deepEqual(await conflictingIdResponse.json(), { error: "CONTACTS_ID_CONFLICT" });
  const badAad = contactsSnapshot(native, 999, Date.now());
  badAad.contactsEnvelope.aad = "AWR1|A2I_CONTACTS|1|wrong";
  const badAadBody = JSON.stringify(badAad);
  const badAadResponse = await postContacts(badAadBody, signedHeaders(native, badAadBody, "POST", "/api/v1/android/contacts"));
  assert.equal(badAadResponse.status, 400);
  assert.deepEqual(await badAadResponse.json(), { error: "INVALID_CONTACTS_ENVELOPE" });
  const badProfile = { ...contactsSnapshot(native, 0, Date.now()), wechatUserId: 1 };
  const badProfileBody = JSON.stringify(badProfile);
  const badProfileResponse = await postContacts(badProfileBody, signedHeaders(native, badProfileBody, "POST", "/api/v1/android/contacts"));
  assert.equal(badProfileResponse.status, 400);
  assert.deepEqual(await badProfileResponse.json(), { error: "INVALID_CONTACTS_SNAPSHOT" });
  const overLimit = contactsSnapshot(native, 999, Date.now());
  overLimit.contactsEnvelope.ct = randomBytes(1_048_576 + 17).toString("base64url");
  const overLimitBody = JSON.stringify(overLimit);
  const overLimitResponse = await postContacts(overLimitBody, signedHeaders(native, overLimitBody, "POST", "/api/v1/android/contacts"));
  assert.equal(overLimitResponse.status, 400);
  assert.deepEqual(await overLimitResponse.json(), { error: "INVALID_CONTACTS_ENVELOPE" });

  const contactsResponse = await fetch(`${origin}/api/v1/ios/contacts`, {
    headers: browserHeaders(nativeSession, native.pairId),
  });
  await assertResponseStatus(contactsResponse, 200);
  const storedContacts = await contactsResponse.json() as { snapshots: Array<{ id: string; wechatUserId: number; contactsEnvelope: { ct: string } }> };
  assert.deepEqual(
    storedContacts.snapshots.map((snapshot) => ({ id: snapshot.id, wechatUserId: snapshot.wechatUserId })),
    [
      { id: newerContacts0.id, wechatUserId: 0 },
      { id: contacts999.id, wechatUserId: 999 },
    ],
  );
  const contactsRows = await db.query<(mysql.RowDataPacket & { envelope_json: string; body_hash: Buffer })[]>(
    "SELECT envelope_json, body_hash FROM contacts_snapshots WHERE device_id = ? ORDER BY wechat_user_id",
    [native.deviceId],
  );
  assert.equal(contactsRows[0].length, 2);
  assert.equal(contactsRows[0][0]!.envelope_json.includes("Alice"), false);
  assert.equal(contactsRows[0][0]!.body_hash.length, 32);
  const webContacts = await fetch(`${origin}/api/v1/ios/contacts`, {
    headers: browserHeaders(sessionA, a.pairId),
  });
  assert.equal(webContacts.status, 400);
  assert.deepEqual(await webContacts.json(), { error: "INVALID_IOS_SESSION" });
  const crossPairContacts = await fetch(`${origin}/api/v1/ios/contacts`, {
    headers: browserHeaders(nativeSession, a.pairId),
  });
  assert.equal(crossPairContacts.status, 400);
  assert.deepEqual(await crossPairContacts.json(), { error: "PAIRING_MISMATCH" });

  await updateIosPush(nativeSession, native.pairId, invalidApnsToken, "sandbox", true);
  const nativeMessageEvents = await openIosEvents(nativeSession, native.pairId, 1);
  assert.deepEqual(await nativeMessageEvents.next(), { event: "ready", data: "1" });
  const rejectedNativeMessage = await uploadMessage(native, {
    v: 4,
    seq: 2,
    wechatUserId: 0,
    replyCapable: false,
  });
  assert.deepEqual(await nativeMessageEvents.next(), { event: "message", data: "2" });
  await nativeMessageEvents.close();
  const reconnectEvents = await openIosEvents(nativeSession, native.pairId, 1);
  assert.deepEqual(await reconnectEvents.next(), { event: "ready", data: "2" });
  await reconnectEvents.close();
  await waitFor(async () => !(await iosStatus(nativeSession, native.pairId)).pushRegistered);
  const nativeMessages = await fetch(`${origin}/api/v1/messages`, {
    headers: browserHeaders(nativeSession, native.pairId, true),
  });
  assert.equal(nativeMessages.status, 200);
  const [[rejectedOutbox]] = await db.query<(mysql.RowDataPacket & { attempts: string | number; sent_at: string | number | null })[]>(
    "SELECT attempts, sent_at FROM push_outbox WHERE message_id = ?",
    [rejectedNativeMessage.id],
  );
  assert.equal(Number(rejectedOutbox?.attempts), 1);
  assert.equal(rejectedOutbox?.sent_at, null);

  const replacementNative = await pairDevice(nativeSession);
  assert.notEqual(replacementNative.deviceId, native.deviceId);
  const oldDeviceSnapshot = contactsSnapshot(native, 0, Date.now());
  const oldDeviceSnapshotBody = JSON.stringify(oldDeviceSnapshot);
  await assertResponseStatus(
    await postContacts(oldDeviceSnapshotBody, signedHeaders(native, oldDeviceSnapshotBody, "POST", "/api/v1/android/contacts")),
    201,
  );
  const replacementContacts = await fetch(`${origin}/api/v1/ios/contacts`, {
    headers: browserHeaders(nativeSession, replacementNative.pairId),
  });
  await assertResponseStatus(replacementContacts, 200);
  assert.deepEqual(await replacementContacts.json(), { snapshots: [] });

  const defaultPolicy = await browserJson<{ enabled: boolean; scheduleEnabled: boolean; weekdays: number[] }>("/api/v1/relay-policy", sessionB, b.pairId);
  assert.equal(defaultPolicy.enabled, true);
  assert.equal(defaultPolicy.scheduleEnabled, false);
  assert.deepEqual(defaultPolicy.weekdays, [1, 2, 3, 4, 5]);
  const policyPoll = fetch(`${origin}/api/v1/android/replies`, {
    headers: signedHeaders(b, "", "GET", "/api/v1/android/replies"),
  });
  await delay(50);
  const pausedPolicy = await updateRelayPolicy(sessionB, b.pairId, false, true);
  assert.equal(pausedPolicy.active, false);
  const polled = await (await policyPoll).json() as { reply: null; relayPolicy: { enabled: boolean } };
  assert.equal(polled.reply, null);
  assert.equal(polled.relayPolicy.enabled, false);
  await uploadMessage(b, { v: 4, seq: 1, wechatUserId: 999, replyCapable: false, expectedDropped: true });
  assert.equal((await browserJson<{ enabled: boolean }>("/api/v1/relay-policy", sessionA, a.pairId)).enabled, true);
  await updateRelayPolicy(sessionB, b.pairId, true, false);

  assert.equal((await fetch(`${origin}/api/status`, {
    headers: { "X-AWR-Test-Token": testToken },
  })).status, 400);
  await expectError("/api/status", sessionA, b.pairId, "PAIRING_MISMATCH", {
    "X-AWR-Test-Token": testToken,
  });
  assert.equal((await fetch(`${origin}/api/status`, {
    headers: { ...browserHeaders(sessionA, a.pairId), "X-AWR-Test-Token": testToken },
  })).status, 200);
  const deniedTestPush = await fetch(`${origin}/api/test-push`, {
    method: "POST",
    headers: {
      Origin: origin,
      "X-AWR-Test-Token": testToken,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  assert.equal(deniedTestPush.status, 400);
  assert.deepEqual(await deniedTestPush.json(), { error: "UNAUTHORIZED" });
  const testPushId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0";
  const testPush = await fetch(`${origin}/api/test-push`, {
    method: "POST",
    headers: {
      ...browserHeaders(sessionA, a.pairId, true),
      "X-AWR-Test-Token": testToken,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messageId: testPushId,
      previewEnvelope: {
        alg: "A256GCM",
        kid: "phase1",
        iv: randomBytes(12).toString("base64url"),
        aad: `AWR1|A2I|${testPushId}|test`,
        ct: randomBytes(32).toString("base64url"),
      },
    }),
  });
  assert.equal(testPush.status, 202, await testPush.text());

  const updateA = await fetch(`${origin}/api/subscription`, {
    method: "POST",
    headers: {
      ...browserHeaders(sessionA, a.pairId, true),
      "X-AWR-Test-Token": testToken,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(subscription("a-updated")),
  });
  await assertResponseStatus(updateA, 200);
  assert.equal(((await updateA.json()) as { ackToken: string }).ackToken, sessionA);
  const [sessionRows] = await db.query<(mysql.RowDataPacket & { subscription_envelope: string; pair_id: string })[]>(
    "SELECT subscription_envelope, pair_id FROM browser_sessions WHERE pair_id IN (?, ?) ORDER BY pair_id",
    [a.pairId, b.pairId],
  );
  assert.equal(sessionRows.length, 2);
  assert.equal(sessionRows.some((row) => row.subscription_envelope.includes("push.example.invalid")), false);
  const decryptedSubscriptions = sessionRows.map((row) => decryptStoredSubscription(row.subscription_envelope));
  assert.equal(decryptedSubscriptions.some((stored) => stored.endpoint.endsWith("/a-updated")), true);
  assert.equal(decryptedSubscriptions.some((stored) => stored.endpoint.endsWith("/b")), true);

  await uploadMessage(a, { v: 4, seq: 1, wechatUserId: 0, expectedError: "INVALID_REPLY_CAPABILITY" });
  await uploadMessage(a, { v: 5, seq: 1, wechatUserId: 0, replyCapable: true, expectedError: "INVALID_CONVERSATION_SEND_CAPABILITY" });
  await uploadMessage(a, { v: 5, seq: 1, wechatUserId: 0, replyCapable: false, conversationSendCapable: true, expectedError: "INVALID_CONVERSATION_SEND_CAPABILITY" });
  const a0 = await uploadMessage(a, { v: 1, seq: 1, wechatUserId: 0 });
  const assetId = randomUUID();
  for (const invalid of [{ assetWidth: 16385 }, { assetWidth: 9000, assetHeight: 9000 }, { assetBytes: 8 * 1024 * 1024 + 17 }]) {
    await uploadMessage(a, { v: 4, seq: 2, wechatUserId: 999, replyCapable: false, assetId, ...invalid, expectedError: "INVALID_ASSET" });
  }
  const a999 = await uploadMessage(a, { v: 5, seq: 2, wechatUserId: 999, replyCapable: true, conversationSendCapable: true, assetId, assetWidth: 960, assetHeight: 1280, assetBytes: 8 * 1024 * 1024 + 16 });
  const b999 = await uploadMessage(b, { v: 4, seq: 1, wechatUserId: 999, replyCapable: false });
  await waitFor(async () => {
    const ids = new Set((await captureLines(captureFile)).map((entry) => entry.messageId));
    return ids.has(a0.id) && ids.has(a999.id) && ids.has(b999.id);
  });

  const listA = await browserJson<{ messages: Array<{ messageId: string; wechatUserId: number; replyCapable: boolean }> }>(
    "/api/v1/messages",
    sessionA,
    a.pairId,
  );
  assert.deepEqual(new Set(listA.messages.map((message) => message.messageId)), new Set([a0.id, a999.id]));
  assert.deepEqual(new Set(listA.messages.map((message) => message.wechatUserId)), new Set([0, 999]));
  assert.equal(listA.messages.find((message) => message.messageId === a0.id)?.replyCapable, false);
  assert.equal(listA.messages.find((message) => message.messageId === a999.id)?.replyCapable, true);
  const listB = await browserJson<{ messages: Array<{ messageId: string }> }>(
    "/api/v1/messages",
    sessionB,
    b.pairId,
  );
  assert.deepEqual(listB.messages.map((message) => message.messageId), [b999.id]);
  await expectError("/api/v1/messages", sessionA, b.pairId, "PAIRING_MISMATCH");

  const ownAsset = await fetch(`${origin}/api/v1/assets/${assetId}`, { headers: browserHeaders(sessionA, a.pairId) });
  assert.equal(ownAsset.status, 200);
  const storedAsset = await ownAsset.json() as { width: number; height: number; envelope: { ct: string } };
  assert.equal(storedAsset.width, 960);
  assert.equal(storedAsset.height, 1280);
  assert.equal(Buffer.from(storedAsset.envelope.ct, "base64url").length, 8 * 1024 * 1024 + 16);
  const otherAsset = await fetch(`${origin}/api/v1/assets/${assetId}`, { headers: browserHeaders(sessionB, b.pairId) });
  assert.equal(otherAsset.status, 404);
  await expectError(`/api/v1/assets/${assetId}`, sessionA, b.pairId, "PAIRING_MISMATCH");

  const sse = await fetch(`${origin}/api/v1/messages/stream?afterSeq=1`, {
    headers: browserHeaders(sessionB, b.pairId),
  });
  assert.equal(sse.status, 200);
  const reader = sse.body!.getReader();
  const pendingSse = reader.read();
  await uploadMessage(a, { v: 4, seq: 3, wechatUserId: 999, replyCapable: false });
  assert.equal(await Promise.race([pendingSse.then(() => "data"), delay(150).then(() => "timeout")]), "timeout");
  await uploadMessage(b, { v: 4, seq: 2, wechatUserId: 999, replyCapable: false });
  const sseChunk = await pendingSse;
  assert.match(Buffer.from(sseChunk.value!).toString("utf8"), /data: 2/);
  await reader.cancel();

  const unsupportedReply = await submitReply(sessionA, a, a0.id, 1, 0);
  assert.deepEqual(unsupportedReply, { error: "REPLY_UNSUPPORTED" });
  const replyId = randomUUID();
  const createdAt = Date.now();
  const reply = await submitReply(sessionA, a, a999.id, 2, 999, replyId, createdAt);
  assert.deepEqual(reply, { replyId, status: "QUEUED" });
  const crossReply = await submitReply(sessionB, b, a999.id, 2, 999);
  assert.deepEqual(crossReply, { error: "TARGET_MESSAGE_NOT_FOUND" });
  const claim = await fetch(`${origin}/api/v1/android/replies`, {
    headers: signedHeaders(a, "", "GET", "/api/v1/android/replies"),
  });
  assert.equal(claim.status, 200);
  const claimed = (await claim.json()) as { reply: { v: number; id: string; deviceId: string; wechatUserId: number } };
  assert.deepEqual(
    { v: claimed.reply.v, id: claimed.reply.id, deviceId: claimed.reply.deviceId, wechatUserId: claimed.reply.wechatUserId },
    { v: 2, id: replyId, deviceId: a.deviceId, wechatUserId: 999 },
  );
  await acknowledgeReply(a, replyId, "SENT_TO_WECHAT");

  const profileMismatch = await submitReply(sessionA, a, a999.id, 3, 0);
  assert.deepEqual(profileMismatch, { error: "PROFILE_MISMATCH" });
  const spoofedDevice = await submitReply(sessionA, { ...a, deviceId: b.deviceId }, a999.id, 3, 999);
  assert.deepEqual(spoofedDevice, { error: "TARGET_MESSAGE_NOT_FOUND" });
  const crossConversationSend = await submitReply(sessionB, b, a999.id, 3, 999);
  assert.deepEqual(crossConversationSend, { error: "TARGET_MESSAGE_NOT_FOUND" });
  const quickReplyOnly = await uploadMessage(a, { v: 4, seq: 4, wechatUserId: 999, replyCapable: true });
  const unsupportedConversationSend = await submitReply(sessionA, a, quickReplyOnly.id, 3, 999);
  assert.deepEqual(unsupportedConversationSend, { error: "CONVERSATION_SEND_UNSUPPORTED" });

  const sendId = randomUUID();
  const sendCreatedAt = Date.now();
  const conversationSend = await submitReply(sessionA, a, a999.id, 3, 999, sendId, sendCreatedAt);
  assert.deepEqual(conversationSend, { replyId: sendId, status: "QUEUED" });
  const sendClaim = await fetch(`${origin}/api/v1/android/replies`, {
    headers: signedHeaders(a, "", "GET", "/api/v1/android/replies"),
  });
  await assertResponseStatus(sendClaim, 200);
  const claimedSend = (await sendClaim.json()) as { reply: { v: number; pairId: string; id: string; deviceId: string; wechatUserId: number } };
  assert.deepEqual(
    { v: claimedSend.reply.v, pairId: claimedSend.reply.pairId, id: claimedSend.reply.id, deviceId: claimedSend.reply.deviceId, wechatUserId: claimedSend.reply.wechatUserId },
    { v: 3, pairId: a.pairId, id: sendId, deviceId: a.deviceId, wechatUserId: 999 },
  );
  const crossPairReply = await fetch(`${origin}/api/v1/replies/${replyId}`, { headers: browserHeaders(sessionB, b.pairId) });
  await assertResponseStatus(crossPairReply, 404);
  assert.deepEqual(await crossPairReply.json(), { error: "REPLY_NOT_FOUND" });

  const crossAck = await fetch(`${origin}/api/acks`, {
    method: "POST",
    headers: { ...browserHeaders(sessionB, b.pairId, true), "Content-Type": "application/json" },
    body: JSON.stringify({ messageId: a999.id, type: "OPENED" }),
  });
  assert.equal(crossAck.status, 400);
  const ownAck = await fetch(`${origin}/api/acks`, {
    method: "POST",
    headers: { ...browserHeaders(sessionA, a.pairId, true), "Content-Type": "application/json" },
    body: JSON.stringify({ messageId: a999.id, type: "OPENED" }),
  });
  assert.equal(ownAck.status, 202, await ownAck.text());

  const captures = await captureLines(captureFile);
  const testPushNavigation = new URL(captures.find((entry) => entry.messageId === testPushId)!.payload.notification.navigate);
  assert.equal(new URLSearchParams(testPushNavigation.hash.slice(1)).get("pairId"), a.pairId);
  const captureA = captures.find((entry) => entry.messageId === a999.id)!;
  const captureB = captures.find((entry) => entry.messageId === b999.id)!;
  assert.equal(captureA.pairId, a.pairId);
  assert.equal(captureB.pairId, b.pairId);
  assert.notEqual(captureA.sessionId, captureB.sessionId);
  const dataA = captureA.payload.notification.data as Record<string, unknown>;
  assert.deepEqual({ pairId: dataA.pairId, wechatUserId: dataA.wechatUserId }, { pairId: a.pairId, wechatUserId: 999 });
  const navigateA = new URL(captureA.payload.notification.navigate);
  const fragmentA = new URLSearchParams(navigateA.hash.slice(1));
  assert.equal(fragmentA.get("pairId"), a.pairId);
  assert.equal(fragmentA.get("wechatUserId"), "999");

  const revokedEvents = await openIosEvents(nativeSessionB, nativeB.pairId, 0);
  assert.deepEqual(await revokedEvents.next(), { event: "ready", data: "0" });
  await db.execute(
    "UPDATE browser_sessions SET invalidated_at = ?, updated_at = ? WHERE ack_token_hash = ?",
    [Date.now(), Date.now(), createHash("sha256").update(nativeSessionB).digest()],
  );
  assert.equal(await Promise.race([
    revokedEvents.next(),
    delay(25_000).then(() => assert.fail("IOS_SSE_REVOCATION_TIMEOUT")),
  ]), undefined);

  const goneSubscription = await fetch(`${origin}/api/subscription`, {
    method: "POST",
    headers: {
      ...browserHeaders(sessionB, b.pairId, true),
      "X-AWR-Test-Token": testToken,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(subscription("gone")),
  });
  await assertResponseStatus(goneSubscription, 200);
  await uploadMessage(b, { v: 4, seq: 3, wechatUserId: 999, replyCapable: false });
  await waitFor(async () => {
    const [[row]] = await db.query<(mysql.RowDataPacket & { invalidated_at: string | number | null })[]>(
      "SELECT invalidated_at FROM browser_sessions WHERE pair_id = ?",
      [b.pairId],
    );
    return row?.invalidated_at !== null && row?.invalidated_at !== undefined;
  });
  const invalidatedB = await fetch(`${origin}/api/v1/messages`, {
    headers: browserHeaders(sessionB, b.pairId),
  });
  assert.equal(invalidatedB.status, 400);
  assert.deepEqual(await invalidatedB.json(), { error: "UNAUTHORIZED" });
  assert.equal((await fetch(`${origin}/api/status`, {
    headers: {
      ...browserHeaders(sessionA, a.pairId),
      "X-AWR-Test-Token": testToken,
    },
  })).status, 200);

  const nativePersisted = await testNativeContent(db, sessionA, a);
  const slotsPersisted = await testNativeSlots(db, sessionA, a);
  await testTabletReplies(db, sessionA, a);
  await stop(server);
  server = startServer(database, dataDir, captureFile, apnsCaptureFile, apnsKeyPath);
  await waitUntilReady(server);
  await nativePersisted();
  await slotsPersisted();
  const persisted = await browserJson<{ messages: Array<{ messageId: string }> }>(
    "/api/v1/messages",
    sessionA,
    a.pairId,
  );
  assert.equal(persisted.messages.some((message) => message.messageId === a999.id), true);

  const replacement = await pairDevice(sessionA);
  const replacementMessage = await uploadMessage(replacement, { v: 4, seq: 1, wechatUserId: 0, replyCapable: true });
  const replacementList = await browserJson<{ messages: Array<{ messageId: string }> }>(
    "/api/v1/messages",
    sessionA,
    replacement.pairId,
  );
  assert.deepEqual(replacementList.messages.map((message) => message.messageId), [replacementMessage.id]);
  await expectError("/api/v1/messages", sessionA, a.pairId, "PAIRING_MISMATCH");
});

interface DeviceFixture {
  pairId: string;
  deviceId: string;
  privateKey: KeyObject;
}

async function testTabletReplies(db: Connection, foreignSession: string, foreignDevice: DeviceFixture): Promise<void> {
  const session = await createIosSession();
  const device = await pairDevice(session);
  const sealed = (kid: string, aad: string) => ({ alg: "A256GCM", kid, aad, iv: randomBytes(12).toString("base64url"), ct: randomBytes(32).toString("base64url") });
  const targets = [];
  for (const version of [6, 7, 8]) {
    const id = randomUUID(), createdAt = Date.now(), seq = version - 5;
    const value = { v: version, id, deviceId: device.deviceId, seq, createdAt, wechatUserId: 0,
      replyCapable: version >= 7, conversationSendCapable: false, assets: [],
      ...(version === 8 ? { nativeAssetSlots: [] } : { nativeAssets: [] }),
      previewEnvelope: sealed("phase1", `AWR1|A2I|${id}|${device.deviceId}|${seq}|${createdAt}|0`),
      contentEnvelope: sealed("phase2-content", `AWR1|A2I_CONTENT|${version}|${version === 8 ? `${device.pairId}|` : ""}${id}|${device.deviceId}|${seq}|${createdAt}|0`) };
    const body = JSON.stringify(value), path = "/api/v1/android/messages";
    await assertResponseStatus(await fetch(origin + path, { method: "POST", headers: signedHeaders(device, body, "POST", path), body }), 202);
    targets.push(value);
  }
  const statusResponse = await fetch(origin + "/api/v1/ios/status", { headers: browserHeaders(session, device.pairId, true) });
  await assertResponseStatus(statusResponse, 200);
  const status = await statusResponse.json() as { tabletRepliesAvailable: boolean; tabletContactSendAvailable: boolean };
  assert.equal(status.tabletRepliesAvailable, true);
  assert.equal(status.tabletContactSendAvailable, true);
  const messages = await browserJson<{ messages: Array<{ nativeVersion: number; replyCapable: boolean }> }>("/api/v1/messages", session, device.pairId);
  assert.deepEqual(messages.messages.map((value) => [value.nativeVersion, value.replyCapable]), [[8, true], [7, true], [6, false]]);
  function command(version: 5 | 6, targetMessageId: string) {
    const id = randomUUID(), createdAt = Date.now();
    return { v: version, id, deviceId: device.deviceId, targetMessageId, createdAt, wechatUserId: 0,
      replyEnvelope: sealed(version === 6 ? "phase2-reply-bootstrap" : "phase1-reply",
        `AWR1|I2A|${version}|${version === 6 ? "REPLY_KEY_BOOTSTRAP" : "TABLET_SEND"}|${device.pairId}|${id}|${device.deviceId}|${targetMessageId}|${createdAt}|0`) };
  }
  const post = (value: object, token = session, pairId = device.pairId) => fetch(origin + "/api/v1/replies", {
    method: "POST", headers: { ...browserHeaders(token, pairId, true), "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
  const bootstrap = command(6, targets[0]!.id);
  const missingBootstrap = await fetch(origin + `/api/v1/replies/${bootstrap.id}`, { headers: browserHeaders(session, device.pairId) });
  await assertResponseStatus(missingBootstrap, 404);
  assert.deepEqual(await missingBootstrap.json(), { error: "REPLY_NOT_FOUND" });
  assert.equal(missingBootstrap.headers.get("Cache-Control"), "no-store");
  await assertResponseStatus(await fetch(origin + `/api/v1/replies/${bootstrap.id}`), 400);
  await assertResponseStatus(await post(bootstrap), 201);
  const ownBootstrap = await fetch(origin + `/api/v1/replies/${bootstrap.id}`, { headers: browserHeaders(session, device.pairId) });
  await assertResponseStatus(ownBootstrap, 200);
  const foreignBootstrap = await fetch(origin + `/api/v1/replies/${bootstrap.id}`, { headers: browserHeaders(foreignSession, foreignDevice.pairId) });
  await assertResponseStatus(foreignBootstrap, 404);
  assert.deepEqual(await foreignBootstrap.json(), { error: "REPLY_NOT_FOUND" });
  await assertResponseStatus(await post(bootstrap), 200);
  await assertResponseStatus(await post(command(5, targets[0]!.id)), 400);
  await assertResponseStatus(await post(command(6, targets[0]!.id), foreignSession, foreignDevice.pairId), 400);
  // A legacy poll must not consume the tablet bootstrap, even before the new worker is installed.
  const abort = new AbortController();
  const oldPath = "/api/v1/android/replies/v4";
  const legacy = fetch(origin + oldPath, { headers: signedHeaders(device, "", "GET", oldPath), signal: abort.signal }).catch(() => undefined);
  await new Promise((resolve) => setTimeout(resolve, 100));
  abort.abort();
  await legacy;
  const [[queued]] = await db.query<(mysql.RowDataPacket & { status: string })[]>("SELECT status FROM replies WHERE id = ?", [bootstrap.id]);
  assert.equal(queued!.status, "QUEUED");
  const poll = async () => {
    const path = "/api/v1/android/replies/tablet";
    const response = await fetch(origin + path, { headers: signedHeaders(device, "", "GET", path) });
    await assertResponseStatus(response, 200);
    return await response.json() as { reply: { id: string; v: number; pairId: string } };
  };
  assert.equal((await poll()).reply.id, bootstrap.id);
  assert.equal((await poll()).reply.id, bootstrap.id);
  const ack = (id: string, status: string) => {
    const path = `/api/v1/android/replies/${id}/ack`, body = JSON.stringify({ status });
    return fetch(origin + path, { method: "POST", headers: signedHeaders(device, body, "POST", path), body });
  };
  await assertResponseStatus(await ack(bootstrap.id, "SENT_TO_WECHAT"), 400);
  await assertResponseStatus(await ack(bootstrap.id, "REPLY_KEY_INSTALLED"), 202);
  await assertResponseStatus(await ack(bootstrap.id, "REPLY_KEY_INSTALLED"), 202);
  const reply = command(5, targets[1]!.id);
  await assertResponseStatus(await post(reply), 201);
  assert.deepEqual((await poll()).reply.id, reply.id);
  await assertResponseStatus(await ack(reply.id, "SEND_UNCONFIRMED"), 202);
  await assertResponseStatus(await ack(reply.id, "SENT_TO_WECHAT"), 400);
  const reply8 = command(5, targets[2]!.id);
  await assertResponseStatus(await post(reply8), 201);
  assert.deepEqual((await poll()).reply.id, reply8.id);
  await assertResponseStatus(await ack(reply8.id, "SEND_UNCONFIRMED"), 202);
  await testTabletContactReplies(db, foreignSession, foreignDevice);
}

async function testTabletContactReplies(db: Connection, foreignSession: string, foreignDevice: DeviceFixture): Promise<void> {
  // A contacts-only device must be able to install its key and send without a message target.
  const session = await createIosSession();
  const device = await pairDevice(session);
  const capturedAt = Date.now();
  const upload = (snapshot: ContactsSnapshotFixture) => {
    const body = JSON.stringify(snapshot);
    return postContacts(body, signedHeaders(device, body, "POST", "/api/v1/android/contacts"));
  };
  const snapshot = contactsSnapshot(device, 0, capturedAt, 3);
  await assertResponseStatus(await upload(snapshot), 201);
  await assertResponseStatus(await upload(snapshot), 200);
  await assertResponseStatus(await upload(contactsSnapshot(device, 999, capturedAt, 3)), 400);
  const invalidAad = contactsSnapshot(device, 0, capturedAt + 1, 3);
  invalidAad.contactsEnvelope.aad = invalidAad.contactsEnvelope.aad.replace("|3|", "|2|");
  await assertResponseStatus(await upload(invalidAad), 400);
  const listed = await browserJson<{ snapshots: ContactsSnapshotFixture[] }>("/api/v1/ios/contacts", session, device.pairId);
  assert.deepEqual(listed.snapshots, [snapshot]);
  function command(version: 4 | 7 | 8, targetContactSnapshotId = snapshot.id, targetDevice = device, pairId = device.pairId, profile = 0) {
    const id = randomUUID(), createdAt = Date.now();
    const action = version === 8 ? "TABLET_CONTACT_REPLY_KEY_BOOTSTRAP" : version === 7 ? "TABLET_CONTACT_SEND" : "CONTACT_SEND";
    return { v: version, id, deviceId: targetDevice.deviceId, targetContactSnapshotId, createdAt, wechatUserId: profile,
      replyEnvelope: { alg: "A256GCM", kid: version === 8 ? "phase2-reply-bootstrap" : "phase1-reply",
        aad: `AWR1|I2A|${version}|${action}|${pairId}|${id}|${targetDevice.deviceId}|${targetContactSnapshotId}|${createdAt}|${profile}`,
        iv: randomBytes(12).toString("base64url"), ct: randomBytes(32).toString("base64url") } };
  }
  const post = (value: object, token = session, pairId = device.pairId) => fetch(origin + "/api/v1/replies", {
    method: "POST", headers: { ...browserHeaders(token, pairId, true), "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
  await assertResponseStatus(await post(command(4)), 400);
  await assertResponseStatus(await post(command(7, snapshot.id, foreignDevice)), 400);
  await assertResponseStatus(await post(command(7, snapshot.id, device, foreignDevice.pairId), foreignSession, foreignDevice.pairId), 400);
  await assertResponseStatus(await post(command(7, snapshot.id, device, device.pairId, 999)), 400);
  await assertResponseStatus(await post(command(8, snapshot.id, device, device.pairId, 999)), 400);
  await assertResponseStatus(await post({ ...command(7), targetMessageId: randomUUID() }), 400);
  const invalidKid = command(7);
  invalidKid.replyEnvelope.kid = "phase2-reply-bootstrap";
  await assertResponseStatus(await post(invalidKid), 400);
  const oversized = command(7);
  oversized.replyEnvelope.ct = randomBytes(8193).toString("base64url");
  await assertResponseStatus(await post(oversized), 400);
  const wrongAad = command(8);
  wrongAad.replyEnvelope.aad = wrongAad.replyEnvelope.aad.replace("TABLET_CONTACT_REPLY_KEY_BOOTSTRAP", "REPLY_KEY_BOOTSTRAP");
  await assertResponseStatus(await post(wrongAad), 400);
  const bootstrap = command(8);
  await assertResponseStatus(await post(bootstrap), 201);
  await assertResponseStatus(await post(bootstrap), 200);
  const send = command(7);
  // Tablet contact payloads retain the tablet ciphertext limit, above the phone v4 limit.
  send.replyEnvelope.ct = randomBytes(8192).toString("base64url");
  await assertResponseStatus(await post(send), 201);
  await assertResponseStatus(await post(send), 200);
  await assertResponseStatus(await post({ ...send, replyEnvelope: { ...send.replyEnvelope, ct: randomBytes(32).toString("base64url") } }), 400);
  for (const path of ["/api/v1/android/replies", "/api/v1/android/replies/v4"]) {
    const abort = new AbortController();
    const legacy = fetch(origin + path, { headers: signedHeaders(device, "", "GET", path), signal: abort.signal }).catch(() => undefined);
    await new Promise((resolve) => setTimeout(resolve, 100));
    abort.abort();
    await legacy;
  }
  const [queued] = await db.query<(mysql.RowDataPacket & { id: string; status: string })[]>("SELECT id, status FROM replies WHERE device_id = ?", [device.deviceId]);
  assert.deepEqual(queued.map((row) => row.status), ["QUEUED", "QUEUED"]);
  const poll = async () => {
    const path = "/api/v1/android/replies/tablet";
    const response = await fetch(origin + path, { headers: signedHeaders(device, "", "GET", path) });
    await assertResponseStatus(response, 200);
    return (await response.json() as { reply: { id: string; v: number; pairId: string; targetContactSnapshotId: string; targetMessageId?: string } }).reply;
  };
  const ack = (id: string, status: string, targetDevice = device) => {
    const path = `/api/v1/android/replies/${id}/ack`, body = JSON.stringify({ status });
    return fetch(origin + path, { method: "POST", headers: signedHeaders(targetDevice, body, "POST", path), body });
  };
  await assertResponseStatus(await ack(bootstrap.id, "REPLY_KEY_INSTALLED"), 400);
  const claimed = await poll();
  assert.deepEqual([claimed.id, claimed.v, claimed.pairId, claimed.targetContactSnapshotId, claimed.targetMessageId], [bootstrap.id, 8, device.pairId, snapshot.id, undefined]);
  assert.deepEqual(await poll(), claimed);
  await assertResponseStatus(await ack(bootstrap.id, "SEND_UNCONFIRMED"), 400);
  await assertResponseStatus(await ack(bootstrap.id, "SENT_TO_WECHAT"), 400);
  await assertResponseStatus(await ack(bootstrap.id, "REPLY_KEY_INSTALLED", foreignDevice), 400);
  await assertResponseStatus(await ack(bootstrap.id, "REPLY_KEY_INSTALLED"), 202);
  await assertResponseStatus(await ack(bootstrap.id, "REPLY_KEY_INSTALLED"), 202);
  await assertResponseStatus(await ack(send.id, "SEND_UNCONFIRMED"), 400);
  const claimedSend = await poll();
  assert.deepEqual([claimedSend.id, claimedSend.v, claimedSend.pairId, claimedSend.targetContactSnapshotId], [send.id, 7, device.pairId, snapshot.id]);
  assert.deepEqual(await poll(), claimedSend);
  await assertResponseStatus(await ack(send.id, "REPLY_KEY_INSTALLED"), 400);
  await assertResponseStatus(await ack(send.id, "CONTACT_SNAPSHOT_STALE"), 202);
  await assertResponseStatus(await ack(send.id, "CONTACT_SNAPSHOT_STALE"), 202);
  await assertResponseStatus(await ack(send.id, "SENT_TO_WECHAT"), 400);
  const replacement = contactsSnapshot(device, 0, capturedAt + 2, 3);
  await assertResponseStatus(await upload(replacement), 201);
  await assertResponseStatus(await post(command(7)), 400);
  await assertResponseStatus(await post(command(8)), 400);
  const currentSend = command(7, replacement.id);
  await assertResponseStatus(await post(currentSend), 201);
  assert.equal((await poll()).id, currentSend.id);
  await assertResponseStatus(await ack(currentSend.id, "SEND_UNCONFIRMED"), 202);
  await assertResponseStatus(await ack(currentSend.id, "SEND_UNCONFIRMED"), 202);
  await assertResponseStatus(await ack(currentSend.id, "SENT_TO_WECHAT"), 400);
  for (const version of [1, 2] as const) {
    const legacy = contactsSnapshot(device, 0, capturedAt + version + 2, version);
    await assertResponseStatus(await upload(legacy), 201);
    await assertResponseStatus(await post(command(7, legacy.id)), 400);
    await assertResponseStatus(await post(command(8, legacy.id)), 400);
    if (version === 2) {
      const phoneSend = command(4, legacy.id);
      await assertResponseStatus(await post(phoneSend), 201);
      const path = "/api/v1/android/replies/tablet", abort = new AbortController();
      const tablet = fetch(origin + path, { headers: signedHeaders(device, "", "GET", path), signal: abort.signal }).catch(() => undefined);
      await new Promise((resolve) => setTimeout(resolve, 100));
      abort.abort();
      await tablet;
      const [[queuedPhone]] = await db.query<(mysql.RowDataPacket & { status: string })[]>("SELECT status FROM replies WHERE id = ?", [phoneSend.id]);
      assert.equal(queuedPhone!.status, "QUEUED");
      const phonePath = "/api/v1/android/replies/v4";
      const phoneResponse = await fetch(origin + phonePath, { headers: signedHeaders(device, "", "GET", phonePath) });
      await assertResponseStatus(phoneResponse, 200);
      const phoneClaim = await phoneResponse.json() as { reply: { id: string; v: number } };
      assert.deepEqual([phoneClaim.reply.id, phoneClaim.reply.v], [phoneSend.id, 4]);
    }
  }
}

async function testNativeContent(db: Connection, foreignSession: string, foreignDevice: DeviceFixture): Promise<() => Promise<void>> {
  const session = await createIosSession();
  const device = await pairDevice(session);
  const original: NativeAsset = { id: randomUUID(), kind: "audio", mimeType: "audio/silk", byteLength: 8 * 1024 * 1024, role: "original" };
  const playback: NativeAsset = { id: randomUUID(), kind: "audio", mimeType: "audio/wav", byteLength: 2048, role: "playback", derivedFrom: original.id };
  function message(seq: number, nativeAssets: NativeAsset[], profile = 999) {
    const id = randomUUID();
    const createdAt = Date.now();
    const sealed = (kid: string, aad: string) => ({ alg: "A256GCM", kid, aad, iv: randomBytes(12).toString("base64url"), ct: randomBytes(32).toString("base64url") });
    return {
      v: 6, id, deviceId: device.deviceId, seq, createdAt, wechatUserId: profile,
      replyCapable: false, conversationSendCapable: false, assets: [], nativeAssets,
      previewEnvelope: sealed("phase1", `AWR1|A2I|${id}|${device.deviceId}|${seq}|${createdAt}|${profile}`),
      contentEnvelope: sealed("phase2-content", `AWR1|A2I_CONTENT|6|${id}|${device.deviceId}|${seq}|${createdAt}|${profile}`),
    };
  }
  const first = message(1, [original, playback]);
  const post = (value: object) => {
    const body = JSON.stringify(value);
    return fetch(`${origin}/api/v1/android/messages`, { method: "POST", headers: signedHeaders(device, body, "POST", "/api/v1/android/messages"), body });
  };
  const get = (path: string, token = session, pairId = device.pairId) => fetch(`${origin}${path}`, { headers: browserHeaders(token, pairId) });
  await assertResponseStatus(await post(first), 202);
  const repeated = await post(first);
  assert.deepEqual(await repeated.json(), { id: first.id, idempotent: true });
  await assertResponseStatus(await post({ ...first, replyCapable: true }), 400);
  await assertResponseStatus(await post({ ...first, conversationSendCapable: true }), 400);
  await assertResponseStatus(await post({ ...first, contentEnvelope: { ...first.contentEnvelope, ct: randomBytes(32).toString("base64url") } }), 400);
  const sequenceCollision = await post(message(1, [], 0));
  assert.deepEqual(await sequenceCollision.json(), { error: "INVALID_SEQUENCE" });
  await updateRelayPolicy(session, device.pairId, false, false);
  assert.deepEqual(await (await post(first)).json(), { id: first.id, idempotent: true });
  await updateRelayPolicy(session, device.pairId, true, false);

  const list = await browserJson<{ messages: Array<Record<string, unknown>> }>("/api/v1/messages", session, device.pairId);
  assert.equal(list.messages[0]!.hasNativeContent, true);
  assert.deepEqual(list.messages[0]!.nativeAssets, [original, playback]);
  assert.equal(list.messages[0]!.contentEnvelope, undefined);
  assert.equal(list.messages[0]!.replyCapable, false);
  assert.equal(list.messages[0]!.conversationSendCapable, false);
  const contentPath = `/api/v1/ios/messages/${first.id}/content`;
  assert.deepEqual(await (await get(contentPath)).json(), { contentEnvelope: first.contentEnvelope });
  await assertResponseStatus(await get(contentPath, foreignSession, foreignDevice.pairId), 404);
  await assertResponseStatus(await get(`/api/v1/ios/messages/${randomUUID()}/content`), 404);
  const originalPath = `/api/v1/ios/assets/${original.id}`;
  const pending = await get(originalPath);
  assert.equal(pending.status, 409);
  assert.deepEqual(await pending.json(), { error: "ASSET_PENDING" });
  await assertResponseStatus(await get(originalPath, foreignSession, foreignDevice.pairId), 404);
  await assertResponseStatus(await get(`/api/v1/ios/assets/${randomUUID()}`), 404);

  function assetEnvelope(asset: NativeAsset) {
    return { alg: "A256GCM", kid: "phase2-asset", iv: randomBytes(12).toString("base64url"),
      aad: `AWR1|A2I_ASSET|6|${first.id}|${asset.id}|${device.deviceId}|1|${first.createdAt}|999|${asset.kind}|${asset.mimeType}|${asset.byteLength}|${asset.role}|${asset.derivedFrom ?? ""}`,
      ct: randomBytes(asset.byteLength + 16).toString("base64url") };
  }
  const originalEnvelope = assetEnvelope(original);
  const playbackEnvelope = assetEnvelope(playback);
  // The existing foreign fixture is a PWA session, which cannot subscribe to native iOS events.
  const foreignNativeSession = await createIosSession(secondTestToken);
  const foreignNativeDevice = await pairDevice(foreignNativeSession, secondTestToken);
  const ownStream = await openIosEvents(session, device.pairId, 1);
  const foreignStream = await openIosEvents(foreignNativeSession, foreignNativeDevice.pairId, 0);
  assert.equal((await ownStream.next())?.event, "ready");
  assert.equal((await foreignStream.next())?.event, "ready");
  const foreignHint = foreignStream.next();
  const upload = (asset: NativeAsset, envelope: object, signedDevice = device, messageId = first.id) => {
    const path = `/api/v1/android/messages/${messageId}/assets/${asset.id}`;
    const body = JSON.stringify({ envelope });
    return fetch(`${origin}${path}`, { method: "POST", headers: signedHeaders(signedDevice, body, "POST", path), body });
  };
  await assertResponseStatus(await upload(original, originalEnvelope, foreignDevice), 404);
  await assertResponseStatus(await upload(original, originalEnvelope, device, randomUUID()), 404);
  await assertResponseStatus(await upload(original, { ...originalEnvelope, ct: randomBytes(1041).toString("base64url") }), 400);
  const uploaded = await upload(original, originalEnvelope);
  assert.equal(uploaded.status, 201);
  assert.deepEqual(await uploaded.json(), { id: original.id, idempotent: false });
  assert.deepEqual(await ownStream.next(), { event: "asset-ready", data: original.id });
  // The hint is observable only after the payload transaction commits.
  assert.equal((await get(originalPath)).status, 200);
  assert.equal(await Promise.race([foreignHint.then(() => "hint"), delay(150).then(() => "timeout")]), "timeout");
  await ownStream.close();
  await foreignStream.close();
  await foreignHint;
  const retry = await upload(original, originalEnvelope);
  assert.equal(retry.status, 200);
  assert.deepEqual(await retry.json(), { id: original.id, idempotent: true });
  const conflicting = await upload(original, assetEnvelope(original));
  assert.equal(conflicting.status, 409);
  assert.deepEqual(await conflicting.json(), { error: "NATIVE_ASSET_CONFLICT" });
  assert.deepEqual(await (await get(originalPath)).json(), { ...original, envelope: originalEnvelope });
  const concurrentUploads = await Promise.all([upload(playback, playbackEnvelope), upload(playback, playbackEnvelope)]);
  assert.deepEqual(concurrentUploads.map((response) => response.status).sort(), [200, 201]);
  await assertResponseStatus(await post(message(2, [original])), 409);

  // Reserve almost the full quota without allocating ciphertext for pending declarations.
  const large = (): NativeAsset => ({ id: randomUUID(), kind: "file", mimeType: "application/octet-stream", byteLength: 8 * 1024 * 1024, role: "original" });
  for (let seq = 2; seq <= 5; seq++)
    await assertResponseStatus(await post(message(seq, Array.from({ length: seq === 5 ? 6 : 8 }, large))), 202);
  const racers = await Promise.all([6, 7].map((seq) => post(message(seq, [{ ...large(), byteLength: 5 * 1024 * 1024 }]))));
  assert.equal(racers.filter((response) => response.status === 202).length, 1);
  assert.equal(racers.filter((response) => response.status === 409 || response.status === 400).length, 1);
  const quotaRejected = await post(message(8, [large()]));
  assert.equal(quotaRejected.status, 409);
  assert.deepEqual(await quotaRejected.json(), { error: "NATIVE_QUOTA_EXCEEDED" });
  const [[reserved]] = await db.query<(mysql.RowDataPacket & { total: string })[]>(
    "SELECT SUM(byte_length) AS total FROM native_assets JOIN messages ON messages.id = native_assets.message_id WHERE messages.device_id = ?", [device.deviceId]);
  assert.ok(Number(reserved!.total) <= 256 * 1024 * 1024);

  await db.execute("UPDATE native_assets SET expires_at = ? WHERE id = ?", [Date.now() - 1, original.id]);
  await assertResponseStatus(await post(message(8, [])), 202);
  const expired = await get(originalPath);
  assert.equal(expired.status, 410);
  assert.deepEqual(await expired.json(), { error: "ASSET_EXPIRED" });
  await assertResponseStatus(await upload(original, originalEnvelope), 410);
  assert.deepEqual(await (await post(first)).json(), { id: first.id, idempotent: true });
  const [[removed]] = await db.query<(mysql.RowDataPacket & { count: number })[]>("SELECT COUNT(*) AS count FROM native_asset_payloads WHERE asset_id = ?", [original.id]);
  assert.equal(Number(removed!.count), 0);

  return async () => {
    assert.deepEqual(await (await get(contentPath)).json(), { contentEnvelope: first.contentEnvelope });
    assert.deepEqual(await (await get(`/api/v1/ios/assets/${playback.id}`)).json(), { ...playback, envelope: playbackEnvelope });
    await assertResponseStatus(await get(originalPath), 410);
  };
}

async function testNativeSlots(db: Connection, foreignSession: string, foreignDevice: DeviceFixture): Promise<() => Promise<void>> {
  const session = await createIosSession(), device = await pairDevice(session);
  const slots: NativeAssetSlot[] = [
    { id: randomUUID(), kind: "image", role: "original" },
    { id: randomUUID(), kind: "file", role: "original" },
  ];
  const sealed = (kid: string, aad: string, size = 32) => ({ alg: "A256GCM", kid, aad,
    iv: randomBytes(12).toString("base64url"), ct: randomBytes(size).toString("base64url") });
  function message(seq: number, declarations: NativeAssetSlot[] = slots, signer = device) {
    const id = randomUUID(), createdAt = Date.now();
    return { v: 8, id, deviceId: signer.deviceId, seq, createdAt, wechatUserId: 999,
      replyCapable: true, conversationSendCapable: false, nativeAssetSlots: declarations,
      previewEnvelope: sealed("phase1", `AWR1|A2I|${id}|${signer.deviceId}|${seq}|${createdAt}|999`),
      contentEnvelope: sealed("phase2-content", `AWR1|A2I_CONTENT|8|${signer.pairId}|${id}|${signer.deviceId}|${seq}|${createdAt}|999`) };
  }
  const post = (value: object, signer = device) => {
    const path = "/api/v1/android/messages", body = JSON.stringify(value);
    return fetch(origin + path, { method: "POST", headers: signedHeaders(signer, body, "POST", path), body });
  };
  const get = (path: string, token = session, pair = device.pairId) => fetch(origin + path, { headers: browserHeaders(token, pair) });
  const first = message(1);
  const plaintext = { body: "mixed record text arrives first", attachments: slots.map((slot, itemIndex) => ({ ...slot, itemIndex })) };
  const key = randomBytes(32), iv = Buffer.from(first.contentEnvelope.iv, "base64url");
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(first.contentEnvelope.aad));
  first.contentEnvelope.ct = Buffer.concat([cipher.update(JSON.stringify(plaintext)), cipher.final(), cipher.getAuthTag()]).toString("base64url");
  await assertResponseStatus(await post({ ...first, nativeAssets: [] }), 400);
  await assertResponseStatus(await post({ ...first, conversationSendCapable: true }), 400);
  await assertResponseStatus(await post({ ...first, contentEnvelope: { ...first.contentEnvelope, aad: first.contentEnvelope.aad.replace(device.pairId, foreignDevice.pairId) } }), 400);
  await assertResponseStatus(await post({ ...first, v: 7, nativeAssets: [], nativeAssetSlots: undefined }), 400);
  await assertResponseStatus(await post(first), 202);
  assert.deepEqual(await (await post(first)).json(), { id: first.id, idempotent: true });
  const contentPath = `/api/v1/ios/messages/${first.id}/content`;
  const initial = await (await get(contentPath)).json() as { contentEnvelope: typeof first.contentEnvelope; nativeAssets: NativeAsset[]; nativeAssetSlots: NativeAssetSlot[] };
  assert.deepEqual(initial.nativeAssetSlots, slots);
  assert.deepEqual(initial.nativeAssets, []);
  const contentBytes = Buffer.from(initial.contentEnvelope.ct, "base64url");
  const decipher = createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAAD(Buffer.from(initial.contentEnvelope.aad));
  decipher.setAuthTag(contentBytes.subarray(-16));
  assert.deepEqual(JSON.parse(Buffer.concat([decipher.update(contentBytes.subarray(0, -16)), decipher.final()]).toString()), plaintext);
  for (const slot of slots) await assertResponseStatus(await get(`/api/v1/ios/assets/${slot.id}`), 409);
  await assertResponseStatus(await get(contentPath, foreignSession, foreignDevice.pairId), 404);
  await assertResponseStatus(await get(`/api/v1/ios/assets/${slots[0]!.id}`, foreignSession, foreignDevice.pairId), 404);
  function uploadBody(target: ReturnType<typeof message>, metadata: NativeAsset) {
    return { metadata, envelope: sealed("phase2-asset", `AWR1|A2I_ASSET|8|${device.pairId}|${target.id}|${metadata.id}|${device.deviceId}|${target.seq}|${target.createdAt}|999|${metadata.kind}|${metadata.mimeType}|${metadata.byteLength}|${metadata.role}|${metadata.derivedFrom ?? ""}`, metadata.byteLength + 16) };
  }
  const upload = (target: ReturnType<typeof message>, assetId: string, value: object | string, signer = device) => {
    const path = `/api/v1/android/messages/${target.id}/assets/${assetId}`, body = typeof value === "string" ? value : JSON.stringify(value);
    return fetch(origin + path, { method: "POST", headers: signedHeaders(signer, body, "POST", path), body });
  };
  const metadata: NativeAsset = { ...slots[0]!, mimeType: "image/png", byteLength: 123 };
  const body = uploadBody(first, metadata);
  await assertResponseStatus(await upload(first, metadata.id, body, foreignDevice), 404);
  await assertResponseStatus(await upload(first, metadata.id, { ...body, envelope: { ...body.envelope, aad: body.envelope.aad.replace(`|8|${device.pairId}|`, "|7|") } }), 400);
  await assertResponseStatus(await upload(first, metadata.id, { ...body, envelope: { ...body.envelope, aad: body.envelope.aad.replace(device.pairId, foreignDevice.pairId) } }), 400);
  const stream = await openIosEvents(session, device.pairId, 1);
  assert.equal((await stream.next())?.event, "ready");
  await assertResponseStatus(await upload(first, metadata.id, body), 201);
  assert.deepEqual(await stream.next(), { event: "asset-ready", data: metadata.id });
  await stream.close();
  await assertResponseStatus(await upload(first, metadata.id, JSON.stringify(body)), 200);
  await assertResponseStatus(await upload(first, metadata.id, JSON.stringify(body, null, 2)), 409);
  await assertResponseStatus(await upload(first, metadata.id, { ...body, metadata: { ...metadata, byteLength: 124 } }), 409);
  await assertResponseStatus(await upload(first, metadata.id, uploadBody(first, { ...metadata, mimeType: "image/jpeg" })), 409);
  await assertResponseStatus(await upload(first, metadata.id, uploadBody(first, metadata)), 409);
  const resolved = await (await get(contentPath)).json() as typeof initial;
  assert.deepEqual(resolved.contentEnvelope, initial.contentEnvelope);
  assert.deepEqual(resolved.nativeAssetSlots, slots);
  assert.deepEqual(resolved.nativeAssets, [metadata]);
  const list = await browserJson<{ messages: Array<{ seq: number; nativeAssetSlots: NativeAssetSlot[]; nativeAssets: NativeAsset[] }> }>("/api/v1/messages", session, device.pairId);
  assert.equal(list.messages.length, 1);
  assert.equal(list.messages[0]!.seq, 1);
  assert.deepEqual(list.messages[0]!.nativeAssets, [metadata]);
  assert.deepEqual(list.messages[0]!.nativeAssetSlots, slots);
  await assertResponseStatus(await get(`/api/v1/ios/assets/${slots[1]!.id}`), 409);
  await assertResponseStatus(await post(message(2, [slots[0]!])), 409);
  // A playback slot can resolve before its immutable original slot.
  const source: NativeAssetSlot = { id: randomUUID(), kind: "audio", role: "original" };
  const playback: NativeAssetSlot = { id: randomUUID(), kind: "audio", role: "playback", derivedFrom: source.id };
  const audio = message(2, [source, playback]);
  await assertResponseStatus(await post(audio), 202);
  const playbackMetadata: NativeAsset = { ...playback, mimeType: "audio/wav", byteLength: 8 };
  await assertResponseStatus(await upload(audio, playback.id, uploadBody(audio, playbackMetadata)), 201);
  await assertResponseStatus(await get(`/api/v1/ios/assets/${source.id}`), 409);
  // v7 and v8 share one ID reservation namespace, including simultaneous different devices.
  const legacyId = randomUUID();
  const legacy = (seq: number, assets: NativeAsset[], signer = device) => {
    const base = message(seq, [], signer);
    return { ...base, v: 7, nativeAssetSlots: undefined, nativeAssets: assets,
      contentEnvelope: { ...base.contentEnvelope, aad: base.contentEnvelope.aad.replace(`|8|${signer.pairId}|`, "|7|") } };
  };
  await assertResponseStatus(await post(legacy(3, [metadata])), 409);
  await assertResponseStatus(await post(legacy(3, [{ ...metadata, id: legacyId }])), 202);
  await assertResponseStatus(await post(message(4, [{ ...slots[0]!, id: legacyId }])), 409);
  const rivalSession = await createIosSession(), rival = await pairDevice(rivalSession);
  const shared = { ...slots[0]!, id: randomUUID() };
  const collisions = await Promise.all([post(message(4, [shared])), post(legacy(1, [{ ...metadata, id: shared.id }], rival), rival)]);
  assert.deepEqual(collisions.map((response) => response.status).sort(), [202, 409]);
  // Unknown slots reserve no bytes; each actual resolution checks the quota under the device lock.
  for (let seq = 5; seq <= 8; seq++) {
    const assets: NativeAsset[] = Array.from({ length: seq === 8 ? 7 : 8 }, () => ({ id: randomUUID(), kind: "file", role: "original", mimeType: "application/octet-stream", byteLength: 8 * 1024 * 1024 }));
    await assertResponseStatus(await post(legacy(seq, assets)), 202);
  }
  const quotaSlots: NativeAssetSlot[] = Array.from({ length: 2 }, () => ({ id: randomUUID(), kind: "file", role: "original" }));
  const quotaMessage = message(9, quotaSlots);
  await assertResponseStatus(await post(quotaMessage), 202);
  const races = await Promise.all(quotaSlots.map((slot) => {
    const asset: NativeAsset = { ...slot, mimeType: "application/octet-stream", byteLength: 5 * 1024 * 1024 };
    return upload(quotaMessage, slot.id, uploadBody(quotaMessage, asset));
  }));
  assert.deepEqual(races.map((response) => response.status).sort(), [201, 409]);
  const [[total]] = await db.query<(mysql.RowDataPacket & { bytes: string })[]>("SELECT SUM(byte_length) AS bytes FROM native_assets JOIN messages ON messages.id = native_assets.message_id WHERE messages.device_id = ?", [device.deviceId]);
  assert.ok(Number(total!.bytes) <= 256 * 1024 * 1024);
  await db.execute("UPDATE native_asset_slots SET expires_at = ? WHERE id = ?", [Date.now() - 1, slots[1]!.id]);
  await assertResponseStatus(await get(`/api/v1/ios/assets/${slots[1]!.id}`), 410);
  const expiredMetadata: NativeAsset = { ...slots[1]!, mimeType: "application/pdf", byteLength: 2 };
  await assertResponseStatus(await upload(first, slots[1]!.id, uploadBody(first, expiredMetadata)), 410);
  return async () => {
    assert.deepEqual(await (await get(contentPath)).json(), resolved);
    assert.deepEqual(await (await get(`/api/v1/ios/assets/${metadata.id}`)).json(), { ...metadata, envelope: body.envelope });
    await assertResponseStatus(await get(`/api/v1/ios/assets/${slots[1]!.id}`), 410);
  };
}

async function applyMigrations(admin: Connection, database: string): Promise<void> {
  await admin.query(`USE \`${database}\``);
  for (const name of ["001-baseline.sql", "002-multi-pair-browser-sessions.sql", "003-message-reply-capability.sql", "004-conversation-send-capability.sql", "005-relay-policy.sql", "006-contacts-snapshots.sql", "007-contact-send-replies.sql", "008-native-content.sql", "009-native-asset-slots.sql"])
    await admin.query(await readFile(new URL(`../scripts/migrations/${name}`, import.meta.url), "utf8"));
}

function startServer(
  database: string,
  dataDir: string,
  captureFile: string,
  apnsCaptureFile: string,
  apnsKeyPath: string,
): ChildProcess {
  const vapid = webpush.generateVAPIDKeys();
  return spawn(process.execPath, [new URL("./server.js", import.meta.url).pathname], {
    env: {
      ...process.env,
      NODE_ENV: "test",
      PORT: String(port),
      PUBLIC_ORIGIN: origin,
      PUBLIC_DIR: new URL("../public", import.meta.url).pathname,
      DATA_DIR: dataDir,
      TEST_TOKEN: testToken,
      TEST_TOKENS: secondTestToken,
      STORAGE_KEY: storageKey.toString("base64url"),
      VAPID_PUBLIC_KEY: vapid.publicKey,
      VAPID_PRIVATE_KEY: vapid.privateKey,
      BUILD_VERSION: "integration-test",
      MYSQL_HOST: process.env.MYSQL_TEST_ADMIN_HOST ?? "127.0.0.1",
      MYSQL_PORT: process.env.MYSQL_TEST_ADMIN_PORT ?? "3306",
      MYSQL_DATABASE: database,
      MYSQL_USER: adminUser!,
      MYSQL_PASSWORD: process.env.MYSQL_TEST_ADMIN_PASSWORD ?? "",
      TEST_PUSH_CAPTURE_FILE: captureFile,
      TEST_PUSH_GONE_ENDPOINT_SUFFIX: "/gone",
      APNS_TEAM_ID: "TEAMID1234",
      APNS_KEY_ID: "KEYID12345",
      APNS_PRIVATE_KEY_PATH: apnsKeyPath,
      APNS_TOPIC: "com.auroramaple.wechatrelay",
      TEST_APNS_CAPTURE_FILE: apnsCaptureFile,
      TEST_APNS_INVALID_TOKEN: invalidApnsToken,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function createIosSession(token = testToken): Promise<string> {
  const response = await fetch(`${origin}/api/v1/ios/sessions`, {
    method: "POST",
    headers: {
      Origin: origin,
      "X-AWR-Test-Token": token,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  await assertResponseStatus(response, 201);
  return ((await response.json()) as { ackToken: string }).ackToken;
}

async function updateIosPush(
  ackToken: string,
  pairId: string,
  deviceToken: string | null,
  environment: "sandbox" | "production",
  previewEnabled: boolean,
): Promise<void> {
  const response = await fetch(`${origin}/api/v1/ios/push`, {
    method: "PUT",
    headers: {
      ...browserHeaders(ackToken, pairId, true),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ deviceToken, environment, previewEnabled }),
  });
  await assertResponseStatus(response, 200);
}

interface IosStatus {
  paired: boolean;
  deviceId: string | null;
  lastSeenAt: number | null;
  serverTime: number;
  pushConfigured: boolean;
  pushRegistered: boolean;
}

async function iosStatus(ackToken: string, pairId: string): Promise<IosStatus> {
  const response = await fetch(`${origin}/api/v1/ios/status`, {
    headers: browserHeaders(ackToken, pairId, true),
  });
  await assertResponseStatus(response, 200);
  return await response.json() as IosStatus;
}

async function openIosEvents(ackToken: string, pairId: string, afterSeq: number): Promise<{
  next: () => Promise<{ event: string; data: string } | undefined>;
  close: () => Promise<void>;
}> {
  const response = await fetch(`${origin}/api/v1/ios/events?afterSeq=${afterSeq}`, {
    headers: browserHeaders(ackToken, pairId, true),
  });
  await assertResponseStatus(response, 200);
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  return {
    next: async () => {
      while (true) {
        const boundary = buffer.indexOf("\n\n");
        if (boundary >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = /^event: ([^\n]+)$/m.exec(frame)?.[1];
          const data = /^data: ([^\n]+)$/m.exec(frame)?.[1];
          if (event && data !== undefined) return { event, data };
          continue;
        }
        const chunk = await reader.read();
        if (chunk.done) return undefined;
        buffer += decoder.decode(chunk.value, { stream: true });
      }
    },
    close: async () => { await reader.cancel(); },
  };
}

async function createSubscription(label: string, token = testToken): Promise<string> {
  const response = await fetch(`${origin}/api/subscription`, {
    method: "POST",
    headers: {
      Origin: origin,
      "X-AWR-Test-Token": token,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(subscription(label)),
  });
  await assertResponseStatus(response, 201);
  return ((await response.json()) as { ackToken: string }).ackToken;
}

function subscription(label: string): object {
  return {
    endpoint: `https://push.example.invalid/${label}`,
    keys: { p256dh: `p256dh-${label}`, auth: `auth-${label}` },
  };
}

function decryptStoredSubscription(value: string): { endpoint: string } {
  const wrapped = JSON.parse(value) as { iv: string; tag: string; ct: string };
  const decipher = createDecipheriv("aes-256-gcm", storageKey, Buffer.from(wrapped.iv, "base64url"));
  decipher.setAuthTag(Buffer.from(wrapped.tag, "base64url"));
  return JSON.parse(Buffer.concat([
    decipher.update(Buffer.from(wrapped.ct, "base64url")),
    decipher.final(),
  ]).toString("utf8")) as { endpoint: string };
}

async function pairDevice(ackToken: string, token = testToken): Promise<DeviceFixture> {
  const pairingResponse = await fetch(`${origin}/api/v1/pairings`, {
    method: "POST",
    headers: {
      Origin: origin,
      "X-AWR-Test-Token": token,
      "X-AWR-Ack-Token": ackToken,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  await assertResponseStatus(pairingResponse, 201);
  const pairing = (await pairingResponse.json()) as { pairId: string; pairSecret: string };
  const keys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const fixture = {
    pairId: pairing.pairId,
    deviceId: randomBytes(16).toString("base64url"),
    privateKey: keys.privateKey,
  };
  const response = await fetch(`${origin}/api/v1/android/pair`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pairId: pairing.pairId,
      pairSecret: pairing.pairSecret,
      deviceId: fixture.deviceId,
      publicKey: keys.publicKey.export({ type: "spki", format: "der" }).toString("base64url"),
    }),
  });
  assert.equal(response.status, 201, await response.text());
  return fixture;
}

async function uploadMessage(
  device: DeviceFixture,
  options: { v: 1 | 3 | 4 | 5; seq: number; wechatUserId: 0 | 999; replyCapable?: boolean; conversationSendCapable?: boolean; assetId?: string; assetWidth?: number; assetHeight?: number; assetBytes?: number; expectedError?: string; expectedDropped?: boolean },
): Promise<{ id: string }> {
  const id = randomUUID();
  const createdAt = Date.now();
  const suffix = options.v === 3 || options.v === 4 || options.v === 5 ? `|${options.wechatUserId}` : "";
  const previewEnvelope = {
    alg: "A256GCM",
    kid: "phase1",
    iv: randomBytes(12).toString("base64url"),
    aad: `AWR1|A2I|${id}|${device.deviceId}|${options.seq}|${createdAt}${suffix}`,
    ct: randomBytes(32).toString("base64url"),
  };
  const assets = options.assetId
    ? [{
        id: options.assetId,
        kind: "image",
        mimeType: "image/jpeg",
        width: options.assetWidth ?? 1,
        height: options.assetHeight ?? 1,
        envelope: {
          alg: "A256GCM",
          kid: "phase1-asset",
          iv: randomBytes(12).toString("base64url"),
          aad: `AWR1|A2I_ASSET|${options.assetId}|${device.deviceId}|${options.seq}|${createdAt}${suffix}`,
          ct: randomBytes(options.assetBytes ?? 32).toString("base64url"),
        },
      }]
    : undefined;
  const body = JSON.stringify({
    v: options.v,
    id,
    deviceId: device.deviceId,
    seq: options.seq,
    createdAt,
    ...(options.v === 3 || options.v === 4 || options.v === 5 ? { wechatUserId: options.wechatUserId } : {}),
    ...(options.v === 4 || options.v === 5 ? { replyCapable: options.replyCapable } : {}),
    ...(options.v === 5 ? { conversationSendCapable: options.conversationSendCapable } : {}),
    previewEnvelope,
    ...(assets ? { assets } : {}),
  });
  const response = await fetch(`${origin}/api/v1/android/messages`, {
    method: "POST",
    headers: signedHeaders(device, body, "POST", "/api/v1/android/messages"),
    body,
  });
  if (options.expectedError) {
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { error: options.expectedError });
  } else {
    if (options.expectedDropped) {
      assert.equal(response.status, 202);
      assert.deepEqual(await response.json(), { id, dropped: true });
    } else {
      assert.equal(response.status, 202, await response.text());
    }
  }
  return { id };
}

async function updateRelayPolicy(ackToken: string, pairId: string, enabled: boolean, scheduleEnabled: boolean): Promise<{ active: boolean }> {
  const response = await fetch(`${origin}/api/v1/relay-policy`, {
    method: "PUT",
    headers: { ...browserHeaders(ackToken, pairId, true), "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, scheduleEnabled, weekdays: [1, 2, 3, 4, 5], start: "09:30", end: "18:00", timezone: "Asia/Shanghai" }),
  });
  await assertResponseStatus(response, 200);
  return await response.json() as { active: boolean };
}

async function submitReply(
  ackToken: string,
  device: DeviceFixture,
  targetMessageId: string,
  v: 1 | 2 | 3,
  wechatUserId: 0 | 999,
  id = randomUUID(),
  createdAt = Date.now(),
): Promise<Record<string, unknown>> {
  const suffix = v === 2 ? `|${wechatUserId}` : "";
  const body = {
    v,
    id,
    targetMessageId,
    deviceId: device.deviceId,
    createdAt,
    ...(v !== 1 ? { wechatUserId } : {}),
    replyEnvelope: {
      alg: "A256GCM",
      kid: "phase1-reply",
      iv: randomBytes(12).toString("base64url"),
      aad: v === 3
        ? `AWR1|I2A|3|CONVERSATION_SEND|${device.pairId}|${id}|${device.deviceId}|${targetMessageId}|${createdAt}|${wechatUserId}`
        : `AWR1|I2A|${id}|${device.deviceId}|${targetMessageId}|${createdAt}${suffix}`,
      ct: randomBytes(32).toString("base64url"),
    },
  };
  const response = await fetch(`${origin}/api/v1/replies`, {
    method: "POST",
    headers: { ...browserHeaders(ackToken, device.pairId, true), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await response.json() as Record<string, unknown>;
}

async function acknowledgeReply(device: DeviceFixture, replyId: string, status: string): Promise<void> {
  const pathname = `/api/v1/android/replies/${replyId}/ack`;
  const body = JSON.stringify({ status });
  const response = await fetch(`${origin}${pathname}`, {
    method: "POST",
    headers: signedHeaders(device, body, "POST", pathname),
    body,
  });
  await assertResponseStatus(response, 202);
}

function signedHeaders(device: DeviceFixture, body: string, method: "GET" | "POST", pathname: string): Record<string, string> {
  const timestamp = String(Date.now());
  const nonce = randomBytes(16).toString("base64url");
  const canonical = `${method}\n${pathname}\n${timestamp}\n${nonce}\n${createHash("sha256").update(body).digest("hex")}`;
  return {
    "Content-Type": "application/json",
    "X-AWR-Device-Id": device.deviceId,
    "X-AWR-Timestamp": timestamp,
    "X-AWR-Nonce": nonce,
    "X-AWR-Signature": sign("sha256", Buffer.from(canonical), {
      key: device.privateKey,
      dsaEncoding: "der",
    }).toString("base64url"),
  };
}

interface ContactsSnapshotFixture {
  v: 1 | 2 | 3;
  id: string;
  deviceId: string;
  wechatUserId: 0 | 999;
  capturedAt: number;
  contactsEnvelope: {
    alg: "A256GCM";
    kid: "phase1-contacts";
    iv: string;
    aad: string;
    ct: string;
  };
}

function contactsSnapshot(
  device: DeviceFixture,
  wechatUserId: 0 | 999,
  capturedAt: number,
  v: 1 | 2 | 3 = 1,
): ContactsSnapshotFixture {
  const id = uuidV7();
  return {
    v,
    id,
    deviceId: device.deviceId,
    wechatUserId,
    capturedAt,
    contactsEnvelope: {
      alg: "A256GCM",
      kid: "phase1-contacts",
      iv: randomBytes(12).toString("base64url"),
      aad: `AWR1|A2I_CONTACTS|${v}|${id}|${device.deviceId}|${capturedAt}|${wechatUserId}`,
      ct: randomBytes(64).toString("base64url"),
    },
  };
}

async function submitContactReply(
  ackToken: string,
  pairId: string,
  device: DeviceFixture,
  targetContactSnapshotId: string,
  wechatUserId: 0 | 999,
  id = randomUUID(),
  createdAt = Date.now(),
  replyEnvelope?: { alg: "A256GCM"; kid: "phase1-reply"; iv: string; aad: string; ct: string },
): Promise<Record<string, unknown>> {
  const body = {
    v: 4,
    id,
    targetContactSnapshotId,
    deviceId: device.deviceId,
    wechatUserId,
    createdAt,
    replyEnvelope: replyEnvelope ?? {
      alg: "A256GCM",
      kid: "phase1-reply",
      iv: randomBytes(12).toString("base64url"),
      aad: `AWR1|I2A|4|CONTACT_SEND|${pairId}|${id}|${device.deviceId}|${targetContactSnapshotId}|${createdAt}|${wechatUserId}`,
      ct: randomBytes(32).toString("base64url"),
    },
  };
  const response = await fetch(`${origin}/api/v1/replies`, {
    method: "POST",
    headers: { ...browserHeaders(ackToken, pairId, true), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await response.json() as Record<string, unknown>;
}

function uuidV7(): string {
  const random = randomBytes(9).toString("hex");
  const timestamp = Date.now().toString(16).padStart(12, "0").slice(-12);
  return `${timestamp.slice(0, 8)}-${timestamp.slice(8)}-7${random.slice(0, 3)}-8${random.slice(3, 6)}-${random.slice(6)}`;
}

async function postContacts(body: string, headers: Record<string, string>): Promise<Response> {
  return fetch(`${origin}/api/v1/android/contacts`, {
    method: "POST",
    headers,
    body,
  });
}

function browserHeaders(ackToken: string, pairId: string, includeOrigin = false): Record<string, string> {
  return {
    "X-AWR-Ack-Token": ackToken,
    "X-AWR-Pair-Id": pairId,
    ...(includeOrigin ? { Origin: origin } : {}),
  };
}

async function browserJson<T>(path: string, ackToken: string, pairId: string): Promise<T> {
  const response = await fetch(`${origin}${path}`, { headers: browserHeaders(ackToken, pairId) });
  await assertResponseStatus(response, 200);
  return await response.json() as T;
}

async function expectError(
  path: string,
  ackToken: string,
  pairId: string,
  error: string,
  additionalHeaders: Record<string, string> = {},
): Promise<void> {
  const response = await fetch(`${origin}${path}`, {
    headers: { ...browserHeaders(ackToken, pairId), ...additionalHeaders },
  });
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error });
}

async function assertResponseStatus(response: Response, expected: number): Promise<void> {
  if (response.status !== expected)
    assert.fail(`expected HTTP ${expected}, received ${response.status}: ${await response.text()}`);
}

async function writeLegacySubscription(dataDir: string, ackToken: string, pairId: string): Promise<void> {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", storageKey, iv);
  const plaintext = JSON.stringify({
    subscription: subscription("legacy"),
    ackToken,
    savedAt: Date.now(),
    activePairId: pairId,
  });
  const ct = Buffer.concat([cipher.update(plaintext, "utf8"), cipher.final()]);
  await writeFile(join(dataDir, "subscription.json"), JSON.stringify({
    iv: iv.toString("base64url"),
    tag: cipher.getAuthTag().toString("base64url"),
    ct: ct.toString("base64url"),
  }), { mode: 0o600 });
}

async function captureLines(path: string): Promise<Array<{
  sessionId: string;
  pairId: string;
  messageId: string;
  payload: { notification: { navigate: string; data: Record<string, unknown> } };
}>> {
  const content = await readFile(path, "utf8").catch(() => "");
  return content.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function apnsCaptureLines(path: string): Promise<Array<{
  origin: string;
  topic: string;
  pushType: string;
  payload: Record<string, unknown>;
}>> {
  const content = await readFile(path, "utf8").catch(() => "");
  return content.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function waitUntilReady(server: ChildProcess): Promise<void> {
  let output = "";
  server.stdout?.on("data", (chunk) => { output += chunk; });
  server.stderr?.on("data", (chunk) => { output += chunk; });
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error(`SERVER_EXITED: ${output}`);
    try {
      if ((await fetch(`${origin}/readyz`)).ok) return;
    } catch {
      // Server is still starting.
    }
    await delay(50);
  }
  throw new Error(`SERVER_START_TIMEOUT: ${output}`);
}

async function waitFor(check: () => Promise<boolean>): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await check()) return;
    await delay(25);
  }
  throw new Error("CONDITION_TIMEOUT");
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function stop(process: ChildProcess): Promise<void> {
  if (process.exitCode !== null) return;
  process.kill("SIGTERM");
  await Promise.race([
    new Promise<void>((resolve) => process.once("exit", () => resolve())),
    delay(2_000),
  ]);
}
