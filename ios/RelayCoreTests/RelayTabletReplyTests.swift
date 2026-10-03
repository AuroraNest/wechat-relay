import CryptoKit
import Foundation
import Testing
@testable import RelayCore

@Suite(.serialized)
struct RelayTabletReplyTests {
    private let messageID = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0")!
    private let pairID = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b2"
    private let deviceID = "tablet-device-0001"

    private func session() throws -> RelaySession {
        try RelaySession(origin: URL(string: "https://example.invalid")!, ackToken: "fixture", pairId: pairID,
            messageKey: Data(repeating: 1, count: 32), replyKey: Data(repeating: 2, count: 32))
    }

    private func message(version: Int = 7, capable: Bool = true) throws -> RelayMessage {
        let preview = try RelayCrypto.encrypt(Data("{\"sender\":\"Same name\",\"body\":\"fixture\"}".utf8), key: session().messageKey,
            kid: "phase1", aad: "AWR1|A2I|\(messageID.uuidString.lowercased())|\(deviceID)|1|1788148800000|0")
        return try RelayMessage(messageId: messageID, deviceId: deviceID, seq: 1, createdAt: 1_788_148_800_000,
            wechatUserId: 0, replyCapable: capable, conversationSendCapable: false, previewEnvelope: preview, assets: [],
            receivedAt: 1_788_148_800_000, hasNativeContent: true, nativeVersion: version)
    }

    private func content(talker: String = "wxid_fixture") throws -> RelayNativeContent {
        let value: [String: Any] = ["v": 1, "conversationId": talker, "conversationName": "Same name", "accountFingerprint": String(repeating: "a", count: 64),
            "senderId": talker, "senderName": "Same name", "kind": "text", "text": "fixture", "attachments": []]
        return try JSONDecoder().decode(RelayNativeContent.self, from: JSONSerialization.data(withJSONObject: value))
    }

    @Test func oldServerDoesNotAdvertiseTabletReplies() throws {
        let status = try JSONDecoder().decode(RelayDeviceStatus.self, from: Data("{\"paired\":true,\"deviceId\":\"tablet-device-0001\",\"lastSeenAt\":null,\"serverTime\":1,\"pushConfigured\":false,\"pushRegistered\":false}".utf8))
        #expect(!status.tabletRepliesAvailable)
        #expect(!status.tabletContactSendAvailable)
    }

    @Test func tabletContactsRetainStableIdentityWithDuplicateNames() throws {
        let contacts = [try RelayContact(name: "Same name", conversationId: "wxid_a", alias: "alias_a"), try RelayContact(name: "Same name", conversationId: "wxid_b", alias: "")]
        let payload = try RelayContactsPayload(v: 3, contacts: contacts, accountFingerprint: String(repeating: "a", count: 64))
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(payload), key: session().messageKey, kid: "phase1-contacts",
            aad: "AWR1|A2I_CONTACTS|3|\(messageID.uuidString.lowercased())|\(deviceID)|1788148800000|0")
        let snapshot = try RelayContactSnapshot(v: 3, id: messageID, deviceId: deviceID, wechatUserId: 0, capturedAt: 1_788_148_800_000, contactsEnvelope: envelope)
        #expect(try RelayCrypto.decryptContacts(snapshot, messageKey: session().messageKey) == payload)
        #expect(contacts[0].id != contacts[1].id)
        #expect(throws: (any Error).self) { try RelayContactsPayload(v: 3, contacts: [contacts[0], contacts[0]], accountFingerprint: String(repeating: "a", count: 64)) }
        #expect(throws: (any Error).self) { try RelayContactSnapshot(v: 3, id: messageID, deviceId: deviceID, wechatUserId: 999, capturedAt: 1_788_148_800_000, contactsEnvelope: envelope) }
        let relabeled = try RelayContactSnapshot(v: 2, id: messageID, deviceId: deviceID, wechatUserId: 0, capturedAt: snapshot.capturedAt, contactsEnvelope: envelope)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptContacts(relabeled, messageKey: session().messageKey) }
        let legacy = try JSONDecoder().decode(RelayContact.self, from: Data("{\"name\":\"legacy\"}".utf8))
        #expect(legacy.conversationId == nil && legacy.alias == nil && legacy.id == "legacy")
    }

    @Test func contactSendAndBootstrapBindSnapshotWithoutAnyMessage() throws {
        let session = try session()
        let now = Date(timeIntervalSince1970: 1_788_148_800)
        let account = String(repeating: "a", count: 64)
        let contact = try RelayContact(name: "Same name", conversationId: "wxid_fixture", alias: "unique_alias")
        let command = try RelayCrypto.makeTabletContactSend(session: session, snapshotId: messageID, deviceId: deviceID, contact: contact, accountFingerprint: account, body: "hello 👋", now: now)
        #expect(command.v == 7 && command.targetMessageId == nil && command.targetContactSnapshotId == messageID && command.wechatUserId == 0)
        #expect(command.replyEnvelope.aad == "AWR1|I2A|7|TABLET_CONTACT_SEND|\(pairID)|\(command.id.uuidString.lowercased())|\(deviceID)|\(messageID.uuidString.lowercased())|1788148800000|0")
        let plaintext = try RelayCrypto.decrypt(command.replyEnvelope, key: session.replyKey, expectedKid: "phase1-reply", maximumCiphertextBytes: 8192)
        let object = try #require(JSONSerialization.jsonObject(with: plaintext) as? [String: String])
        #expect(object == ["body": "hello 👋", "conversationId": "wxid_fixture", "accountFingerprint": account])
        for invalid in [try RelayContact(name: "Same name", conversationId: "wxid_fixture", alias: ""), try RelayContact(name: "Group", conversationId: "room@chatroom", alias: "group_alias"), try RelayContact(name: "Legacy")] {
            #expect(throws: (any Error).self) { try RelayCrypto.makeTabletContactSend(session: session, snapshotId: messageID, deviceId: deviceID, contact: invalid, accountFingerprint: account, body: "hello") }
        }
        let bootstrap = try RelayCrypto.makeTabletContactReplyBootstrap(session: session, snapshotId: messageID, deviceId: deviceID, now: now)
        #expect(bootstrap.v == 8 && bootstrap.targetMessageId == nil && bootstrap.targetContactSnapshotId == messageID)
        #expect(bootstrap.replyEnvelope.aad == "AWR1|I2A|8|TABLET_CONTACT_REPLY_KEY_BOOTSTRAP|\(pairID)|\(bootstrap.id.uuidString.lowercased())|\(deviceID)|\(messageID.uuidString.lowercased())|1788148800000|0")
        let derived = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: session.messageKey), salt: Data(pairID.utf8), info: Data("AWR1|TABLET_REPLY_KEY_BOOTSTRAP|\(deviceID)".utf8), outputByteCount: 32)
        let key = derived.withUnsafeBytes { Data($0) }
        let installed = try RelayCrypto.decrypt(bootstrap.replyEnvelope, key: key, expectedKid: "phase2-reply-bootstrap", maximumCiphertextBytes: 8192)
        let installedObject = try #require(JSONSerialization.jsonObject(with: installed) as? [String: Any])
        #expect(installedObject["replyKey"] as? String == RelayCrypto.base64url(session.replyKey))
        #expect(installedObject["deviceId"] as? String == deviceID)
        #expect(installedObject["pairId"] as? String == pairID)
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decrypt(bootstrap.replyEnvelope, key: session.replyKey, expectedKid: "phase2-reply-bootstrap", maximumCiphertextBytes: 8192) }
    }

    @Test func replyGateMatchesEncryptedReplyEligibility() throws {
        #expect(try RelayCrypto.canReplyToTablet(target: message(version: 8), content: content()))
        #expect(try !RelayCrypto.canReplyToTablet(target: message(), content: nil))
        #expect(try !RelayCrypto.canReplyToTablet(target: message(version: 6, capable: false), content: content()))
        #expect(try !RelayCrypto.canReplyToTablet(target: message(), content: content(talker: "room@chatroom")))
        #expect(try !RelayCrypto.canReplyToTablet(target: message(capable: false), content: content()))
    }

    @Test func v6RemainsReadOnlyAndCannotBeSentThroughLegacyReply() throws {
        #expect(throws: (any Error).self) { try message(version: 6, capable: true) }
        #expect(throws: (any Error).self) { try RelayCrypto.makeReply(session: session(), target: message(), body: "fixture") }
        #expect(throws: (any Error).self) { try RelayCrypto.makeTabletReply(session: session(), target: message(version: 6, capable: false), content: content(), body: "fixture") }
    }

    @Test func v8RetainsOnlyTheExistingPrivateReplyPermission() throws {
        #expect(try RelayCrypto.makeTabletReply(session: session(), target: message(version: 8), content: content(), body: "fixture").v == 5)
        #expect(throws: (any Error).self) { try RelayCrypto.makeTabletReply(session: session(), target: message(version: 8, capable: false), content: content(), body: "fixture") }
        #expect(throws: (any Error).self) { try RelayCrypto.makeTabletReply(session: session(), target: message(version: 8), content: content(talker: "fixture@chatroom"), body: "fixture") }
    }

    @Test func tabletSendBindsStableRecipientAndAccountInsideReplyCiphertext() throws {
        let session = try session()
        let command = try RelayCrypto.makeTabletReply(session: session, target: message(), content: content(), body: "中文和 emoji 👋")
        #expect(command.v == 5)
        #expect(command.replyEnvelope.aad.contains("|TABLET_SEND|\(pairID)|"))
        let plaintext = try RelayCrypto.decrypt(command.replyEnvelope, key: session.replyKey, expectedKid: "phase1-reply", maximumCiphertextBytes: 8192)
        let object = try #require(JSONSerialization.jsonObject(with: plaintext) as? [String: String])
        #expect(object["conversationId"] == "wxid_fixture")
        #expect(object["accountFingerprint"] == String(repeating: "a", count: 64))
        #expect(object["body"] == "中文和 emoji 👋")
        #expect(throws: (any Error).self) { try RelayCrypto.makeTabletReply(session: session, target: message(), content: content(talker: "room@chatroom"), body: "fixture") }
    }

    @Test func bootstrapWorksForReadOnlyMessagesAndRetainsExistingReplyKey() throws {
        let session = try session()
        let command = try RelayCrypto.makeTabletReplyBootstrap(session: session, target: message(version: 6, capable: false))
        #expect(command.v == 6)
        #expect(command.replyEnvelope.kid == "phase2-reply-bootstrap")
        let derived = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: session.messageKey), salt: Data(pairID.utf8),
            info: Data("AWR1|TABLET_REPLY_KEY_BOOTSTRAP|\(deviceID)".utf8), outputByteCount: 32)
        let key = derived.withUnsafeBytes { Data($0) }
        #expect(key != session.messageKey && key != session.replyKey)
        let plaintext = try RelayCrypto.decrypt(command.replyEnvelope, key: key, expectedKid: "phase2-reply-bootstrap", maximumCiphertextBytes: 8192)
        let object = try #require(JSONSerialization.jsonObject(with: plaintext) as? [String: Any])
        #expect(object["replyKey"] as? String == RelayCrypto.base64url(session.replyKey))
        #expect(object["pairId"] as? String == pairID)
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decrypt(command.replyEnvelope, key: session.messageKey, expectedKid: "phase2-reply-bootstrap", maximumCiphertextBytes: 8192) }
    }

    @Test func v7ContentCannotBeRelabeledAsV6() throws {
        let session = try session()
        let target = try message()
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(content()), key: session.messageKey,
            kid: "phase2-content", aad: RelayCrypto.nativeContentAAD(target))
        #expect(try RelayCrypto.decryptNativeContent(envelope, for: target, messageKey: session.messageKey).conversationId == "wxid_fixture")
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeContent(envelope, for: message(version: 6, capable: false), messageKey: session.messageKey) }
    }
}
