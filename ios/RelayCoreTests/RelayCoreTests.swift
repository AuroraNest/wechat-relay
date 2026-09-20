import Foundation
import Testing
@testable import RelayCore

@Suite(.serialized)
struct RelayCoreTests {
    private let key = Data((0...31).map(UInt8.init))
    private let messageID = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0")!
    private let deviceID = "device-id-0000001"

    private func previewEnvelope(profile: Int) throws -> RelayEncryptedEnvelope {
        let aad = "AWR1|A2I|\(messageID.uuidString.lowercased())|\(deviceID)|1|1788148800000|\(profile)"
        let data = try JSONEncoder().encode(RelayPreview(sender: "Phase 0", body: "cross-language AES-GCM"))
        return try RelayCrypto.encrypt(data, key: key, kid: "phase1", aad: aad)
    }

    @Test func decryptsCrossLanguageAESGCMFixture() throws {
        let envelope = RelayEncryptedEnvelope(
            kid: "phase1",
            iv: "ICEiIyQlJicoKSor",
            aad: "AWR1|A2I|019d2f1a-7b4c-7d10-8c21-1c77be6a91b0|phase0-ios|1|1788148800000",
            ct: "qRjVFQL8f3w4RmCeqXmHnPB5zrCl4gyKFdRWMCr4MXoGo4ll4EJAu3OYTtMdWjKPoWfO8-jDVNOXIwf47l4m21dtWNg"
        )
        let plaintext = try RelayCrypto.decrypt(envelope, key: key, expectedKid: "phase1", maximumCiphertextBytes: 4_096)
        #expect(try JSONDecoder().decode(RelayPreview.self, from: plaintext) == RelayPreview(sender: "Phase 0", body: "cross-language AES-GCM"))
    }

    @Test func rejectsTamperedOrCrossBoundPreview() throws {
        let valid = try previewEnvelope(profile: 0)
        let replacement = valid.ct.last == "A" ? "B" : "A"
        let envelope = RelayEncryptedEnvelope(kid: "phase1", iv: valid.iv, aad: valid.aad, ct: String(valid.ct.dropLast()) + replacement)
        let message = try RelayMessage(messageId: messageID, deviceId: deviceID, seq: 1, createdAt: 1_788_148_800_000, wechatUserId: 0, replyCapable: false, conversationSendCapable: false, previewEnvelope: envelope, assets: [], receivedAt: 1_788_148_800_000)
        #expect(throws: (any Error).self) { try RelayCrypto.decryptPreview(message, messageKey: key) }

        let crossBound = RelayEncryptedEnvelope(kid: "phase1", iv: "ICEiIyQlJicoKSor", aad: "AWR1|A2I|019d2f1a-7b4c-7d10-8c21-1c77be6a91b0|other-device-name|1|1788148800000", ct: "qRjVFQL8f3w4RmCeqXmHnPB5zrCl4gyKFdRWMCr4MXoGo4ll4EJAu3OYTtMdWjKPoWfO8-jDVNOXIwf47l4m21dtWNg")
        let crossBoundMessage = try RelayMessage(messageId: messageID, deviceId: deviceID, seq: 1, createdAt: 1_788_148_800_000, wechatUserId: 0, replyCapable: false, conversationSendCapable: false, previewEnvelope: crossBound, assets: [], receivedAt: 1_788_148_800_000)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptPreview(crossBoundMessage, messageKey: key) }
    }

    @Test func validatesAssetAADProfileBinding() throws {
        let assetID = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1")!
        let metadata = try RelayAssetMetadata(id: assetID, kind: .image, mimeType: "image/png", width: 1, height: 1)
        let message = try RelayMessage(messageId: messageID, deviceId: deviceID, seq: 1, createdAt: 1_788_148_800_000, wechatUserId: 999, replyCapable: false, conversationSendCapable: false, previewEnvelope: try previewEnvelope(profile: 999), assets: [metadata], receivedAt: 1_788_148_800_000)
        let json = "{\"id\":\"\(assetID.uuidString.lowercased())\",\"kind\":\"image\",\"mimeType\":\"image/png\",\"width\":1,\"height\":1,\"envelope\":{\"alg\":\"A256GCM\",\"kid\":\"phase1-asset\",\"iv\":\"ICEiIyQlJicoKSor\",\"aad\":\"AWR1|A2I_ASSET|\(assetID.uuidString.lowercased())|\(deviceID)|1|1788148800000|0\",\"ct\":\"qRjVFQL8f3w4RmCeqXmHnPB5zrCl4gyKFdRWMCr4MXoGo4ll4EJAu3OYTtMdWjKPoWfO8-jDVNOXIwf47l4m21dtWNg\"}}"
        let asset = try JSONDecoder().decode(RelayAsset.self, from: Data(json.utf8))
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptAsset(asset, for: message, messageKey: key) }
    }

    @Test func makesUUIDv7AndV3ReplyWithPairBinding() throws {
        let time = Date(timeIntervalSince1970: 1_788_148_800)
        let uuid = try UUIDv7.make(now: time)
        #expect(uuid.uuidString.lowercased().split(separator: "-")[2].first == "7")
        #expect(uuid.uuidString.lowercased().split(separator: "-")[3].first.map { ["8", "9", "a", "b"].contains(String($0)) } == true)

        let message = try RelayMessage(messageId: messageID, deviceId: deviceID, seq: 1, createdAt: 1_788_148_800_000, wechatUserId: 999, replyCapable: true, conversationSendCapable: true, previewEnvelope: try previewEnvelope(profile: 999), assets: [], receivedAt: 1_788_148_800_000)
        let session = try RelaySession(origin: URL(string: "https://relay.example.com")!, ackToken: "token", pairId: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0", messageKey: key, replyKey: key)
        let reply = try RelayCrypto.makeReply(session: session, target: message, body: "hello", now: time)
        #expect(reply.v == 3)
        #expect(reply.replyEnvelope.aad.contains("|CONVERSATION_SEND|\(session.pairId)|"))
        #expect(throws: (any Error).self) { try RelayCrypto.makeReply(session: session, target: message, body: String(repeating: "x", count: 1_001), now: time) }
    }

    @Test func serializesReplyLowercaseAndAcceptsResponsePath() async throws {
        let id = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0")!
        let target = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1")!
        let reply = RelayReplyRequest(v: 2, id: id, targetMessageId: target, deviceId: deviceID, wechatUserId: 0, createdAt: 1_788_148_800_000, replyEnvelope: RelayEncryptedEnvelope(kid: "phase1-reply", iv: "ICEiIyQlJicoKSor", aad: "AWR1|I2A|\(id.uuidString.lowercased())|\(deviceID)|\(target.uuidString.lowercased())|1788148800000|0", ct: "qRjVFQL8f3w4RmCeqXmHnPB5zrCl4gyKFdRWMCr4MXoGo4ll4EJAu3OYTtMdWjKPoWfO8-jDVNOXIwf47l4m21dtWNg"))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReplyURLProtocol.self]
        ReplyURLProtocol.reset()
        let api = try RelayAPI(origin: URL(string: "https://relay.example.com")!, configuration: configuration)
        let session = try RelaySession(origin: URL(string: "https://relay.example.com")!, ackToken: "ack", pairId: id.uuidString.lowercased(), messageKey: key, replyKey: key)
        let result = try await api.submitReply(session: session, request: reply)
        #expect(result.replyId == id)
        let captured = try #require(ReplyURLProtocol.capturedRequest())
        #expect(captured.url?.path == "/api/v1/replies")
        #expect(captured.value(forHTTPHeaderField: "Origin") == "https://relay.example.com")
        let body = try #require(ReplyURLProtocol.capturedBody())
        let json = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        #expect(json["id"] as? String == id.uuidString.lowercased())
        #expect(json["targetMessageId"] as? String == target.uuidString.lowercased())
    }

    @Test func rejectsOversizedResponseBeforeBodyCollection() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReplyURLProtocol.self]
        ReplyURLProtocol.reset()
        ReplyURLProtocol.configure(body: Data(repeating: 0x61, count: 2_048), headers: ["Content-Length": "2048"])
        let api = try RelayAPI(origin: URL(string: "https://relay.example.com")!, maximumResponseBytes: 1_024, configuration: configuration)
        let session = try RelaySession(origin: URL(string: "https://relay.example.com")!, ackToken: "ack", pairId: messageID.uuidString.lowercased(), messageKey: key, replyKey: key)
        await #expect(throws: RelayError.responseTooLarge) {
            _ = try await api.deviceStatus(session: session)
        }
    }

    @Test func encodesNilPushTokenAsJSONNull() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReplyURLProtocol.self]
        ReplyURLProtocol.reset()
        let api = try RelayAPI(origin: URL(string: "https://relay.example.com")!, configuration: configuration)
        let session = try RelaySession(origin: URL(string: "https://relay.example.com")!, ackToken: "ack", pairId: messageID.uuidString.lowercased(), messageKey: key, replyKey: key)
        try await api.updatePush(session: session, token: nil, environment: .sandbox, previewEnabled: false)
        let request = try #require(ReplyURLProtocol.capturedRequest())
        #expect(request.url?.path == "/api/v1/ios/push")
        let body = try #require(ReplyURLProtocol.capturedBody())
        let json = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        #expect(json["deviceToken"] is NSNull)
        #expect(json["environment"] as? String == "sandbox")
        #expect(json["previewEnabled"] as? Bool == false)
    }
}

private final class ReplyURLProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var request: URLRequest?
    nonisolated(unsafe) private static var body: Data?
    nonisolated(unsafe) private static var responseBody = Data("{\"replyId\":\"019d2f1a-7b4c-7d10-8c21-1c77be6a91b0\",\"status\":\"QUEUED\"}".utf8)
    nonisolated(unsafe) private static var responseHeaders = ["Content-Type": "application/json"]

    static func reset() {
        lock.lock()
        request = nil
        body = nil
        responseBody = Data("{\"replyId\":\"019d2f1a-7b4c-7d10-8c21-1c77be6a91b0\",\"status\":\"QUEUED\"}".utf8)
        responseHeaders = ["Content-Type": "application/json"]
        lock.unlock()
    }

    static func configure(body: Data, headers: [String: String]) {
        lock.lock()
        responseBody = body
        responseHeaders = headers
        lock.unlock()
    }

    static func capturedRequest() -> URLRequest? {
        lock.lock()
        defer { lock.unlock() }
        return request
    }

    static func capturedBody() -> Data? {
        lock.lock()
        defer { lock.unlock() }
        return body
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        Self.request = request
        if let stream = request.httpBodyStream {
            stream.open()
            var bytes = Data()
            let bufferSize = 1_024
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
            defer { buffer.deallocate(); stream.close() }
            while stream.hasBytesAvailable {
                let count = stream.read(buffer, maxLength: bufferSize)
                if count <= 0 { break }
                bytes.append(buffer, count: count)
            }
            Self.body = bytes
        } else {
            Self.body = request.httpBody
        }
        let responseBody = Self.responseBody
        let responseHeaders = Self.responseHeaders
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: 201, httpVersion: "HTTP/1.1", headerFields: responseHeaders)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: responseBody)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
