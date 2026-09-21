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

const adminUser = process.env.MYSQL_TEST_ADMIN_USER;
const port = Number(process.env.AWR_TEST_PORT ?? 18_081);
const origin = `http://127.0.0.1:${port}`;
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
  await expectError(`/api/v1/replies/${replyId}`, sessionB, b.pairId, "REPLY_NOT_FOUND");

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

  await stop(server);
  server = startServer(database, dataDir, captureFile, apnsCaptureFile, apnsKeyPath);
  await waitUntilReady(server);
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

async function applyMigrations(admin: Connection, database: string): Promise<void> {
  await admin.query(`USE \`${database}\``);
  for (const name of ["001-baseline.sql", "002-multi-pair-browser-sessions.sql", "003-message-reply-capability.sql", "004-conversation-send-capability.sql", "005-relay-policy.sql", "006-contacts-snapshots.sql", "007-contact-send-replies.sql"])
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
  v: 1 | 2;
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
  v: 1 | 2 = 1,
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
