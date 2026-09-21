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

    @Test func decryptsNodeContactsFixtureAndRejectsTamperedMetadata() throws {
        let vector = try contactsVector()
        #expect(vector.version == 1)
        let payload = try RelayCrypto.decryptContacts(vector.snapshot, messageKey: vector.key)
        #expect(vector.plaintext == "{\"v\":1,\"contacts\":[{\"name\":\"测试好友 A\"},{\"name\":\"Example B\"}]}")
        let expected = try RelayContactsPayload(v: 1, contacts: [RelayContact(name: "测试好友 A"), RelayContact(name: "Example B")])
        #expect(payload == expected)

        let crossProfile = try RelayContactSnapshot(v: vector.snapshot.v, id: vector.snapshot.id, deviceId: vector.snapshot.deviceId, wechatUserId: 0, capturedAt: vector.snapshot.capturedAt, contactsEnvelope: vector.snapshot.contactsEnvelope)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptContacts(crossProfile, messageKey: vector.key) }

        let duplicatePlaintext = Data("{\"v\":1,\"contacts\":[{\"name\":\"same\"},{\"name\":\"same\"}]}".utf8)
        let duplicateID = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1")!
        let duplicateAAD = "AWR1|A2I_CONTACTS|1|\(duplicateID.uuidString.lowercased())|\(deviceID)|1788148800001|0"
        let duplicateEnvelope = try RelayCrypto.encrypt(duplicatePlaintext, key: key, kid: "phase1-contacts", aad: duplicateAAD)
        let duplicateSnapshot = try RelayContactSnapshot(v: 1, id: duplicateID, deviceId: deviceID, wechatUserId: 0, capturedAt: 1_788_148_800_001, contactsEnvelope: duplicateEnvelope)
        #expect(throws: RelayError.invalidResponse) { try RelayCrypto.decryptContacts(duplicateSnapshot, messageKey: key) }
    }

    @Test func requestsContactsWithAckSessionHeaders() async throws {
        let vector = try contactsVector()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReplyURLProtocol.self]
        ReplyURLProtocol.reset()
        ReplyURLProtocol.configure(body: try JSONEncoder().encode(ContactsResponse(snapshots: [vector.snapshot])), headers: ["Content-Type": "application/json"])
        let api = try RelayAPI(origin: URL(string: "https://relay.example.com")!, configuration: configuration)
        let session = try RelaySession(origin: URL(string: "https://relay.example.com")!, ackToken: "ack", pairId: messageID.uuidString.lowercased(), messageKey: key, replyKey: key)
        let snapshots = try await api.contacts(session: session)
        #expect(snapshots == [vector.snapshot])
        let request = try #require(ReplyURLProtocol.capturedRequest())
        #expect(request.url?.path == "/api/v1/ios/contacts")
        #expect(request.value(forHTTPHeaderField: "X-AWR-Ack-Token") == "ack")
        #expect(request.value(forHTTPHeaderField: "X-AWR-Pair-Id") == session.pairId)
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

    @Test func parsesBoundedStreamHintsAcrossLineEndings() throws {
        var parser = RelayStreamParser()
        var events: [RelayStreamEvent] = []
        let source = ": keepalive\r\n\r\nevent: ready\r\ndata: 0\r\n\r\ndata: 7\n\nevent: reply\rdata: \(messageID.uuidString.lowercased())\r\revent: future\ndata: ignored\n\n"
        for byte in source.utf8 {
            if let event = try parser.append(byte) { events.append(event) }
        }
        #expect(events == [.ready(0), .message(7), .reply(messageID)])
        for invalid in ["data: -1\n\n", "data: 9007199254740992\n\n", "event: reply\ndata: invalid\n\n"] {
            #expect(throws: RelayError.invalidResponse) {
                var parser = RelayStreamParser()
                for byte in invalid.utf8 { _ = try parser.append(byte) }
            }
        }
        #expect(throws: RelayError.responseTooLarge) {
            var parser = RelayStreamParser()
            for byte in String(repeating: "x", count: 1_025).utf8 { _ = try parser.append(byte) }
        }
        #expect(throws: RelayError.responseTooLarge) {
            var parser = RelayStreamParser()
            for byte in String(repeating: "data: 1\n: comment\n", count: 400).utf8 { _ = try parser.append(byte) }
        }
    }

    @Test func streamsAuthenticatedHintsAndTreatsEOFAsDisconnect() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReplyURLProtocol.self]
        ReplyURLProtocol.reset()
        ReplyURLProtocol.configure(body: Data("event: ready\ndata: 5\n\nevent: reply\ndata: \(messageID.uuidString)\n\n".utf8), headers: ["Content-Type": "text/event-stream"], status: 200)
        let api = try RelayAPI(origin: URL(string: "https://relay.example.com")!, configuration: configuration)
        let session = try RelaySession(origin: URL(string: "https://relay.example.com")!, ackToken: "ack", pairId: messageID.uuidString.lowercased(), messageKey: key, replyKey: key)
        let recorder = StreamRecorder()
        await #expect(throws: RelayError.invalidResponse) {
            try await api.streamEvents(session: session, afterSeq: 4) { await recorder.append($0) }
        }
        #expect(await recorder.events == [.ready(5), .reply(messageID)])
        let request = try #require(ReplyURLProtocol.capturedRequest())
        #expect(request.url?.path == "/api/v1/ios/events")
        #expect(request.url?.query == "afterSeq=4")
        #expect(request.value(forHTTPHeaderField: "X-AWR-Ack-Token") == "ack")
        #expect(request.value(forHTTPHeaderField: "X-AWR-Pair-Id") == session.pairId)
        #expect(request.value(forHTTPHeaderField: "Origin") == "https://relay.example.com")
        ReplyURLProtocol.configure(body: Data(), headers: [:], status: 401)
        await #expect(throws: RelayError.httpStatus(401)) {
            try await api.streamEvents(session: session, afterSeq: 4) { _ in }
        }
        ReplyURLProtocol.configure(body: Data(), headers: ["Content-Type": "text/html"], status: 200)
        await #expect(throws: RelayError.invalidResponse) {
            try await api.streamEvents(session: session, afterSeq: 4) { _ in }
        }
    }

    @Test func cancelsOpenStreamWhenForegroundTaskStops() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReplyURLProtocol.self]
        ReplyURLProtocol.reset()
        ReplyURLProtocol.configure(body: Data("event: ready\ndata: 0\n\n".utf8), headers: ["Content-Type": "text/event-stream"], status: 200, holdOpen: true)
        let api = try RelayAPI(origin: URL(string: "https://relay.example.com")!, configuration: configuration)
        let session = try RelaySession(origin: URL(string: "https://relay.example.com")!, ackToken: "ack", pairId: messageID.uuidString.lowercased(), messageKey: key, replyKey: key)
        let recorder = StreamRecorder()
        let task = Task {
            try await api.streamEvents(session: session, afterSeq: 0) { await recorder.append($0) }
        }
        for _ in 0..<100 {
            if await !recorder.events.isEmpty { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        task.cancel()
        await #expect(throws: (any Error).self) { try await task.value }
        #expect(await recorder.events == [.ready(0)])
    }

    private func contactsVector() throws -> ContactsVector {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("packages/crypto-test-vectors/contacts-v1.json"))
        return try JSONDecoder().decode(ContactsVector.self, from: data)
    }
}

private struct ContactsResponse: Encodable { let snapshots: [RelayContactSnapshot] }

private actor StreamRecorder {
    var events: [RelayStreamEvent] = []
    func append(_ event: RelayStreamEvent) { events.append(event) }
}

private struct ContactsVector: Decodable {
    let version: Int
    let key: Data
    let plaintext: String
    let snapshot: RelayContactSnapshot

    enum CodingKeys: String, CodingKey { case version, key, plaintext, snapshot }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        version = try values.decode(Int.self, forKey: .version)
        let encodedKey = try values.decode(String.self, forKey: .key)
        var paddedKey = encodedKey.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        paddedKey.append(String(repeating: "=", count: (4 - paddedKey.count % 4) % 4))
        guard let key = Data(base64Encoded: paddedKey) else { throw RelayError.invalidResponse }
        self.key = key
        plaintext = try values.decode(String.self, forKey: .plaintext)
        snapshot = try values.decode(RelayContactSnapshot.self, forKey: .snapshot)
    }
}

private final class ReplyURLProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var request: URLRequest?
    nonisolated(unsafe) private static var body: Data?
    nonisolated(unsafe) private static var responseBody = Data("{\"replyId\":\"019d2f1a-7b4c-7d10-8c21-1c77be6a91b0\",\"status\":\"QUEUED\"}".utf8)
    nonisolated(unsafe) private static var responseHeaders = ["Content-Type": "application/json"]
    nonisolated(unsafe) private static var responseStatus = 201
    nonisolated(unsafe) private static var holdOpen = false

    static func reset() {
        lock.lock()
        request = nil
        body = nil
        responseBody = Data("{\"replyId\":\"019d2f1a-7b4c-7d10-8c21-1c77be6a91b0\",\"status\":\"QUEUED\"}".utf8)
        responseHeaders = ["Content-Type": "application/json"]
        responseStatus = 201
        holdOpen = false
        lock.unlock()
    }

    static func configure(body: Data, headers: [String: String], status: Int = 201, holdOpen: Bool = false) {
        lock.lock()
        responseBody = body
        responseHeaders = headers
        responseStatus = status
        Self.holdOpen = holdOpen
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
        let responseStatus = Self.responseStatus
        let holdOpen = Self.holdOpen
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: responseStatus, httpVersion: "HTTP/1.1", headerFields: responseHeaders)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: responseBody)
        if !holdOpen { client?.urlProtocolDidFinishLoading(self) }
    }

    override func stopLoading() {}
}
