import CryptoKit
import Foundation
import Testing
@testable import RelayCore

@Suite(.serialized)
struct RelayNativeContentTests {
    private let key = Data(repeating: 7, count: 32)
    private let messageID = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0")!
    private let assetID = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1")!
    private let pairID = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b2"

    private func message(id: UUID? = nil, assets: [RelayNativeAssetMetadata] = []) throws -> RelayMessage {
        let id = id ?? messageID
        let preview = try RelayCrypto.encrypt(JSONEncoder().encode(RelayPreview(sender: "同名会话", body: "摘要")), key: key, kid: "phase1", aad: "AWR1|A2I|\(id.uuidString.lowercased())|tablet-device-0001|1|1788148800000|0")
        return try RelayMessage(messageId: id, deviceId: "tablet-device-0001", seq: 1, createdAt: 1_788_148_800_000, wechatUserId: 0, replyCapable: false, conversationSendCapable: false, previewEnvelope: preview, assets: [], receivedAt: 1_788_148_800_000, hasNativeContent: true, nativeAssets: assets)
    }

    private func content(attachments: [RelayNativeContent.Attachment] = []) -> RelayNativeContent {
        RelayNativeContent(v: 1, conversationId: "stable-room@chatroom", conversationName: "同名会话", accountFingerprint: nil, senderId: "wxid_sender", senderName: "群内发送者", isOutgoing: nil, kind: .reference, text: String(repeating: "完整消息", count: 400), rawXML: "<msg><script>never execute</script></msg>", attachments: attachments, records: [RelayNativeContent.Record(senderName: "原发送者", kind: .record, text: "嵌套记录中的完整文本", rawXML: "<record><nested>原始记录</nested></record>")])
    }

    @Test func legacyMessagesDecodeWithoutNativeFields() throws {
        var object = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(message())) as? [String: Any])
        object.removeValue(forKey: "hasNativeContent")
        object.removeValue(forKey: "nativeAssets")
        let decoded = try JSONDecoder().decode(RelayMessage.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(!decoded.hasNativeContent)
        #expect(decoded.nativeAssets.isEmpty)
        #expect(try RelayCrypto.decryptPreview(decoded, messageKey: key).body == "摘要")
    }

    @Test func decryptsFullTextAndPreservesReferenceAndNestedRawXML() throws {
        let message = try message()
        let original = content()
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(original), key: key, kid: "phase2-content", aad: RelayCrypto.nativeContentAAD(message))
        let decoded = try RelayCrypto.decryptNativeContent(envelope, for: message, messageKey: key)
        #expect(decoded == original)
        #expect(decoded.text.utf8.count > 600)
        #expect(decoded.records?.first?.rawXML == "<record><nested>原始记录</nested></record>")
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decryptNativeContent(envelope, for: message, messageKey: Data(repeating: 8, count: 32)) }
        let other = try self.message(id: UUID())
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeContent(envelope, for: other, messageKey: key) }
    }

    @Test func missingOutgoingDirectionDefaultsToIncomingAndTrueIsPreserved() throws {
        var object = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(content())) as? [String: Any])
        object.removeValue(forKey: "isOutgoing")
        let incoming = try JSONDecoder().decode(RelayNativeContent.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(incoming.isOutgoing != true)
        object["isOutgoing"] = true
        let outgoing = try JSONDecoder().decode(RelayNativeContent.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(outgoing.isOutgoing == true)
    }

    @Test func nativeConversationIdentitySeparatesSourcePairAndOriginalIdentifier() {
        let tablet = RelaySource.tablet.nativeConversationID(pairID: pairID, profile: 0, conversationID: "wxid_a")
        let phone = RelaySource.phone.nativeConversationID(pairID: pairID, profile: 0, conversationID: "wxid_a")
        let anotherPair = RelaySource.tablet.nativeConversationID(pairID: UUID().uuidString, profile: 0, conversationID: "wxid_a")
        let sameName = RelaySource.tablet.nativeConversationID(pairID: pairID, profile: 0, conversationID: "wxid_b")
        let legacy = RelaySource.tablet.conversationID(pairID: pairID, profile: 0, name: "wxid_a")
        #expect(Set([tablet, phone, anotherPair, sameName, legacy]).count == 5)
        #expect(RelaySource.tablet.owns(tablet, pairID: pairID))
        #expect(!RelaySource.phone.owns(tablet, pairID: pairID))
    }

    @Test func decryptsOriginalBytesAndRejectsTamperedMetadataAndDigest() throws {
        let bytes = Data("#!SILK_V3-original-bytes".utf8)
        let metadata = try RelayNativeAssetMetadata(id: assetID, kind: .audio, mimeType: "audio/silk", byteLength: bytes.count, role: .original)
        let message = try message(assets: [metadata])
        let envelope = try RelayCrypto.encrypt(bytes, key: key, kid: "phase2-asset", aad: RelayCrypto.nativeAssetAAD(metadata, message: message))
        let asset = RelayNativeAsset(id: assetID, kind: .audio, mimeType: "audio/silk", byteLength: bytes.count, role: .original, derivedFrom: nil, envelope: envelope)
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        #expect(try RelayCrypto.decryptNativeAsset(asset, for: message, messageKey: key, sha256: digest) == bytes)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeAsset(asset, for: message, messageKey: key, sha256: String(repeating: "0", count: 64)) }
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decryptNativeAsset(asset, for: message, messageKey: Data(repeating: 8, count: 32)) }
        let tampered = RelayNativeAsset(id: assetID, kind: .audio, mimeType: "audio/mpeg", byteLength: bytes.count, role: .original, derivedFrom: nil, envelope: envelope)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeAsset(tampered, for: message, messageKey: key) }
        let shortEnvelope = try RelayCrypto.encrypt(bytes.dropLast(), key: key, kid: "phase2-asset", aad: RelayCrypto.nativeAssetAAD(metadata, message: message))
        let short = RelayNativeAsset(id: assetID, kind: .audio, mimeType: "audio/silk", byteLength: bytes.count, role: .original, derivedFrom: nil, envelope: shortEnvelope)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeAsset(short, for: message, messageKey: key) }
    }

    @Test func validatesOriginalAndPlaybackRelationshipsAndReadOnlyMessages() throws {
        let original = try RelayNativeAssetMetadata(id: assetID, kind: .audio, mimeType: "audio/silk", byteLength: 16, role: .original)
        let playback = try RelayNativeAssetMetadata(id: UUID(), kind: .audio, mimeType: "audio/mp4", byteLength: 32, role: .playback, derivedFrom: assetID)
        try RelayNativeAssetMetadata.validate([original, playback])
        #expect(throws: RelayError.invalidResponse) { try RelayNativeAssetMetadata.validate([playback]) }
        #expect(throws: RelayError.invalidResponse) { try RelayNativeAssetMetadata.validate([original, original]) }
        #expect(throws: (any Error).self) { try RelayNativeAssetMetadata(id: assetID, kind: .file, mimeType: "application/octet-stream", byteLength: 8_388_609, role: .original) }
        #expect(throws: (any Error).self) { try RelayNativeAssetMetadata(id: assetID, kind: .audio, mimeType: "audio/mp4", byteLength: 1, role: .playback) }
        var object = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(message(assets: [original]))) as? [String: Any])
        object["replyCapable"] = true
        #expect(throws: (any Error).self) { try JSONDecoder().decode(RelayMessage.self, from: JSONSerialization.data(withJSONObject: object)) }
        #expect(throws: RelayError.invalidResponse) { try content(attachments: [.init(assetId: UUID(), name: "unknown.txt", sha256: nil)]).validate(for: message(assets: [original])) }
    }

    @Test func recordAttachmentAssociationAcceptsValidIndexAndRejectsOutOfBounds() throws {
        let metadata = try RelayNativeAssetMetadata(id: assetID, kind: .image, mimeType: "image/jpeg", byteLength: 16, role: .original)
        let message = try message(assets: [metadata])
        let associated = content(attachments: [.init(assetId: assetID, name: "record-image.jpg", recordItemIndex: 0)])
        let decoded = try JSONDecoder().decode(RelayNativeContent.self, from: JSONEncoder().encode(associated))
        try decoded.validate(for: message)
        #expect(decoded.attachments.first?.recordItemIndex == 0)
        for index in [-1, 1, Int.max] {
            #expect(throws: RelayError.invalidResponse) {
                try content(attachments: [.init(assetId: assetID, name: "record-image.jpg", recordItemIndex: index)]).validate(for: message)
            }
        }
        var missingRecords = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(associated)) as? [String: Any])
        missingRecords.removeValue(forKey: "records")
        let invalid = try JSONDecoder().decode(RelayNativeContent.self, from: JSONSerialization.data(withJSONObject: missingRecords))
        #expect(throws: RelayError.invalidResponse) { try invalid.validate(for: message) }
        try content(attachments: [.init(assetId: assetID, name: "top-level.jpg")]).validate(for: message)
    }

    @Test func contentAndAssetAPIsKeepPairHeadersAndReportPendingExpiry() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeURLProtocol.self]
        let session = try RelaySession(origin: URL(string: "https://example.invalid")!, ackToken: "pair-scoped-test", pairId: pairID, messageKey: key, replyKey: key)
        let api = try RelayAPI(origin: session.origin, configuration: configuration)
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(content()), key: key, kid: "phase2-content", aad: RelayCrypto.nativeContentAAD(message()))
        NativeURLProtocol.configure(status: 200, data: try JSONEncoder().encode(ContentResponse(contentEnvelope: envelope)))
        #expect(try await api.nativeContent(session: session, messageID: messageID) == envelope)
        let request = try #require(NativeURLProtocol.captured())
        #expect(request.url?.path == "/api/v1/ios/messages/\(messageID.uuidString.lowercased())/content")
        #expect(request.value(forHTTPHeaderField: "X-AWR-Pair-Id") == pairID)
        #expect(request.value(forHTTPHeaderField: "X-AWR-Ack-Token") == "pair-scoped-test")
        NativeURLProtocol.configure(status: 409, data: Data("{\"error\":\"ASSET_PENDING\"}".utf8))
        await #expect(throws: RelayError.assetPending) { try await api.nativeAsset(session: session, id: assetID) }
        #expect(NativeURLProtocol.captured()?.url?.path == "/api/v1/ios/assets/\(assetID.uuidString.lowercased())")
        NativeURLProtocol.configure(status: 410, data: Data("{\"error\":\"ASSET_EXPIRED\"}".utf8))
        await #expect(throws: RelayError.assetExpired) { try await api.nativeAsset(session: session, id: assetID) }
        NativeURLProtocol.configure(status: 404, data: Data("{\"error\":\"NOT_FOUND\"}".utf8))
        await #expect(throws: RelayError.httpStatus(404)) { try await api.nativeAsset(session: session, id: assetID) }
    }

    private struct ContentResponse: Encodable { let contentEnvelope: RelayEncryptedEnvelope }

    @Test func canonicalManifestGETFillsCachedMessageWithoutANewSequence() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeURLProtocol.self]
        let api = try RelayAPI(origin: URL(string: "https://example.invalid")!, configuration: configuration)
        let session = try RelaySession(origin: URL(string: "https://example.invalid")!, ackToken: "pair-scoped-test", pairId: pairID, messageKey: key, replyKey: Data(repeating: 8, count: 32))
        let slot = RelayNativeAssetSlot(id: assetID, kind: .file, role: .original)
        let legacy = try message()
        let before = try RelayMessage(messageId: legacy.id, deviceId: legacy.deviceId, seq: legacy.seq, createdAt: legacy.createdAt, wechatUserId: legacy.wechatUserId, replyCapable: false, conversationSendCapable: false, previewEnvelope: legacy.previewEnvelope, assets: [], receivedAt: legacy.receivedAt, hasNativeContent: true, nativeVersion: 8, nativeAssetSlots: [slot])
        let body = content(attachments: [RelayNativeContent.Attachment(assetId: assetID, name: "original.bin", recordItemIndex: 0)])
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(body), key: key, kid: "phase2-content", aad: RelayCrypto.nativeContentAAD(before, pairID: pairID))
        func response(_ assets: [RelayNativeAssetMetadata]) throws -> Data {
            try JSONSerialization.data(withJSONObject: ["contentEnvelope": JSONSerialization.jsonObject(with: JSONEncoder().encode(envelope)), "nativeAssetSlots": JSONSerialization.jsonObject(with: JSONEncoder().encode([slot])), "nativeAssets": JSONSerialization.jsonObject(with: JSONEncoder().encode(assets))])
        }
        NativeURLProtocol.configure(status: 200, data: try response([]))
        let first = try await api.nativeManifest(session: session, messageID: before.id)
        let cached = try before.mergingNativeManifest(slots: #require(first.nativeAssetSlots), assets: #require(first.nativeAssets))
        #expect(try RelayCrypto.decryptNativeContent(first.contentEnvelope, for: cached, messageKey: key, pairID: pairID) == body)
        let metadata = try RelayNativeAssetMetadata(id: assetID, kind: .file, mimeType: "application/octet-stream", byteLength: 3, role: .original)
        NativeURLProtocol.configure(status: 200, data: try response([metadata]))
        let refreshed = try await api.nativeManifest(session: session, messageID: cached.id)
        let after = try cached.mergingNativeManifest(slots: #require(refreshed.nativeAssetSlots), assets: #require(refreshed.nativeAssets))
        #expect(after.id == cached.id)
        #expect(after.seq == cached.seq)
        #expect(after.nativeAssets == [metadata])
        #expect(first.contentEnvelope == refreshed.contentEnvelope)
        #expect(NativeURLProtocol.captured()?.value(forHTTPHeaderField: "X-AWR-Pair-Id") == pairID)
    }
}

private final class NativeURLProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var responseData = Data()
    nonisolated(unsafe) private static var responseStatus = 200
    nonisolated(unsafe) private static var lastRequest: URLRequest?
    static func configure(status: Int, data: Data) {
        lock.lock(); defer { lock.unlock() }
        responseStatus = status; responseData = data; lastRequest = nil
    }
    static func captured() -> URLRequest? { lock.lock(); defer { lock.unlock() }; return lastRequest }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        Self.lastRequest = request
        let status = Self.responseStatus
        let data = Self.responseData
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
