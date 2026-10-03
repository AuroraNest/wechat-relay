#if DEBUG
import CryptoKit
import Foundation
import RelayCore

enum TabletInputRegression {
    static let enabled = ProcessInfo.processInfo.arguments.contains("--tablet-input-regression")
    static let contactName = ProcessInfo.processInfo.arguments.contains("--tablet-input-missing-alias") ? "Missing Alias" : "Regression Friend"
    static let session = try! RelaySession(origin: URL(string: "https://tablet-input.invalid")!, ackToken: "regression-only",
        pairId: "019f1234-5678-7000-8000-123456789abc", messageKey: Data(repeating: 71, count: 32), replyKey: Data(repeating: 72, count: 32))
    static let device = RelayDeviceStatus(paired: true, deviceId: "tablet-regression-device", lastSeenAt: 1_791_065_600_000,
        serverTime: 1_791_065_600_000, pushConfigured: false, pushRegistered: false, tabletRepliesAvailable: true, tabletContactSendAvailable: true)
    static let conversationID = ProcessInfo.processInfo.arguments.contains("--tablet-input-legacy-route") ? legacyConversationID :
        RelaySource.tablet.nativeConversationID(pairID: session.pairId, profile: 0,
            conversationID: contactName == "Missing Alias" ? "wxid_regression_missing" : "wxid_regression_friend")
    static let legacyConversationID = RelaySource.tablet.conversationID(pairID: session.pairId, profile: 0, name: contactName)
    static let legacyContacts = [try! contacts(version: 1)]
    static let currentContacts = [try! contacts(version: 3)]
    static let legacyBootstrap: RelayReplyRequest = {
        let messageID = UUID(uuidString: "019f1234-5678-7000-8000-123456789ab0")!
        let time: Int64 = 1_791_065_600_000
        let preview = try! RelayCrypto.encrypt(JSONEncoder().encode(RelayPreview(sender: "Regression Friend", body: "Fixture")),
            key: session.messageKey, kid: "phase1", aad: "AWR1|A2I|\(messageID.uuidString.lowercased())|\(device.deviceId!)|1|\(time)|0")
        let message = try! RelayMessage(messageId: messageID, deviceId: device.deviceId!, seq: 1, createdAt: time,
            wechatUserId: 0, replyCapable: true, conversationSendCapable: false, previewEnvelope: preview, assets: [],
            receivedAt: time, hasNativeContent: true, nativeVersion: 7)
        return try! RelayCrypto.makeTabletReplyBootstrap(session: session, target: message)
    }()

    static func install() {
        URLProtocol.registerClass(TabletInputProtocol.self)
    }

    static var configuration: URLSessionConfiguration? {
        guard enabled else { return nil }
        let configuration = URLSessionConfiguration.ephemeral
        // Ephemeral URLSession instances need an explicit protocol list on iOS.
        configuration.protocolClasses = [TabletInputProtocol.self]
        return configuration
    }

    private static func contacts(version: Int) throws -> RelayContactSnapshot {
        let names = ["Regression Friend", "Missing Alias"]
        let contacts: [RelayContact]
        if version == 3 {
            contacts = [try RelayContact(name: names[0], conversationId: "wxid_regression_friend", alias: "regression_friend"),
                try RelayContact(name: names[1], conversationId: "wxid_regression_missing", alias: "")]
        } else {
            contacts = try names.map { try RelayContact(name: $0) }
        }
        let payload = try RelayContactsPayload(v: version == 3 ? 3 : 1, contacts: contacts,
            accountFingerprint: version == 3 ? String(repeating: "a", count: 64) : nil)
        let id = UUID(uuidString: version == 3 ? "019f1234-5678-7000-8000-123456789ab3" : "019f1234-5678-7000-8000-123456789ab2")!
        let time: Int64 = 1_791_065_600_000 + Int64(version)
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(payload), key: session.messageKey, kid: "phase1-contacts",
            aad: "AWR1|A2I_CONTACTS|\(version)|\(id.uuidString.lowercased())|\(device.deviceId!)|\(time)|0")
        return try RelayContactSnapshot(v: version, id: id, deviceId: device.deviceId!, wechatUserId: 0, capturedAt: time, contactsEnvelope: envelope)
    }
}

private final class TabletInputProtocol: URLProtocol, @unchecked Sendable {
    private var pending: DispatchWorkItem?

    override class func canInit(with request: URLRequest) -> Bool {
        // This launch mode owns all networking, so an unexpected origin cannot reach a real service.
        TabletInputRegression.enabled
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, url.host == "tablet-input.invalid" else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        if url.path == "/api/v1/ios/contacts" {
            // Keep the real composer pending long enough to prove draft entry does not require send readiness.
            let work = DispatchWorkItem { [weak self] in self?.respond() }
            pending = work
            DispatchQueue.global().asyncAfter(deadline: .now() + 15, execute: work)
        } else {
            respond()
        }
    }

    override func stopLoading() { pending?.cancel() }

    private func respond() {
        do {
            switch request.url!.path {
            case "/api/v1/ios/status": try send(TabletInputRegression.device)
            case "/api/v1/messages": try send(Messages(pairId: TabletInputRegression.session.pairId, messages: [], hasMore: false))
            case "/api/v1/ios/contacts": try send(Contacts(snapshots: TabletInputRegression.currentContacts))
            case "/api/v1/relay-policy": try send(RelayPolicy.default)
            case "/api/v1/ios/push":
                guard request.httpMethod == "PUT" else { sendError(405); return }
                try send([String: String]())
            case "/api/v1/replies":
                guard request.httpMethod == "POST", let body = requestBody(),
                      let command = try? JSONDecoder().decode(RelayReplyRequest.self, from: body),
                      try acceptsBootstrap(command) else { sendError(400); return }
                try send(Reply(replyId: command.id, status: .replyKeyInstalled))
            case "/api/v1/replies/\(TabletInputRegression.legacyBootstrap.id.uuidString.lowercased())":
                sendError(404)
            default: sendError(404)
            }
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    private func acceptsBootstrap(_ command: RelayReplyRequest) throws -> Bool {
        let fixture = TabletInputRegression.session
        guard command.v == 8, command.deviceId == TabletInputRegression.device.deviceId,
              command.wechatUserId == 0, command.targetMessageId == nil,
              command.targetContactSnapshotId == TabletInputRegression.currentContacts[0].id,
              command.replyEnvelope.kid == "phase2-reply-bootstrap" else { return false }
        let aad = "AWR1|I2A|8|TABLET_CONTACT_REPLY_KEY_BOOTSTRAP|\(fixture.pairId)|\(command.id.uuidString.lowercased())|\(command.deviceId)|\(command.targetContactSnapshotId!.uuidString.lowercased())|\(command.createdAt)|0"
        guard command.replyEnvelope.aad == aad, let iv = decode(command.replyEnvelope.iv),
              let ciphertext = decode(command.replyEnvelope.ct), ciphertext.count > 16 else { return false }
        let key = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: fixture.messageKey), salt: Data(fixture.pairId.utf8),
            info: Data("AWR1|TABLET_REPLY_KEY_BOOTSTRAP|\(command.deviceId)".utf8), outputByteCount: 32)
        let box = try AES.GCM.SealedBox(nonce: AES.GCM.Nonce(data: iv), ciphertext: ciphertext.dropLast(16), tag: ciphertext.suffix(16))
        let plaintext = try AES.GCM.open(box, using: key, authenticating: Data(aad.utf8))
        let payload = try JSONDecoder().decode(Bootstrap.self, from: plaintext)
        return payload.v == 1 && payload.pairId == fixture.pairId && payload.deviceId == command.deviceId && decode(payload.replyKey) == fixture.replyKey
    }

    private func decode(_ value: String) -> Data? {
        let base64 = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        return Data(base64Encoded: base64 + String(repeating: "=", count: (4 - base64.count % 4) % 4))
    }

    private func requestBody() -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var body = Data()
        var buffer = [UInt8](repeating: 0, count: 1_024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count >= 0 else { return nil }
            if count == 0 { break }
            body.append(contentsOf: buffer.prefix(count))
        }
        return body
    }

    private func send<T: Encodable>(_ value: T) throws { deliver(try JSONEncoder().encode(value), status: 200) }
    private func sendError(_ status: Int) { deliver(Data("{}".utf8), status: status) }
    private func deliver(_ body: Data, status: Int) {
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    private struct Messages: Encodable { let pairId: String; let messages: [RelayMessage]; let hasMore: Bool }
    private struct Contacts: Encodable { let snapshots: [RelayContactSnapshot] }
    private struct Reply: Encodable { let replyId: UUID; let status: RelayReplyStatus }
    private struct Bootstrap: Decodable { let v: Int; let pairId: String; let deviceId: String; let replyKey: String }
}
#endif
