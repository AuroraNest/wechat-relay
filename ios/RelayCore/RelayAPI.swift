import Foundation
import Security

public final class RelayAPI: NSObject, @unchecked Sendable {
    private let origin: URL
    private let originString: String
    private let sessionConfiguration: URLSessionConfiguration
    private let timeout: TimeInterval
    private let maximumResponseBytes: Int

    public init(origin: URL, timeout: TimeInterval = 30, maximumResponseBytes: Int = 24 * 1_024 * 1_024, configuration: URLSessionConfiguration? = nil) throws {
        let normalized = try RelayOrigin.normalize(origin)
        guard timeout > 0, timeout <= 120, maximumResponseBytes >= 1_024 else { throw RelayError.invalidValue("HTTP configuration") }
        self.origin = normalized
        self.originString = RelayOrigin.string(normalized)
        self.timeout = timeout
        self.maximumResponseBytes = maximumResponseBytes
        let sessionConfiguration = configuration?.copy() as? URLSessionConfiguration ?? URLSessionConfiguration.ephemeral
        sessionConfiguration.timeoutIntervalForRequest = timeout
        sessionConfiguration.timeoutIntervalForResource = timeout
        sessionConfiguration.httpMaximumConnectionsPerHost = 2
        self.sessionConfiguration = sessionConfiguration
        super.init()
    }

    public func createSession(token: String) async throws -> String {
        guard !token.isEmpty else { throw RelayError.invalidValue("test token") }
        let response: RelaySessionResponse = try await request(path: "/api/v1/ios/sessions", method: "POST", testToken: token, body: try encodedBody(EmptyBody()))
        guard !response.ackToken.isEmpty else { throw RelayError.invalidResponse }
        return response.ackToken
    }

    public func createPairing(token: String, ackToken: String) async throws -> RelayPairing {
        guard !token.isEmpty, !ackToken.isEmpty else { throw RelayError.invalidValue("pairing authorization") }
        let response: RelayPairingResponse = try await request(path: "/api/v1/pairings", method: "POST", testToken: token, ackToken: ackToken, body: try encodedBody(EmptyBody()))
        guard RelayValidation.isUUID(response.pairId), !response.pairSecret.isEmpty, response.expiresAt > 0 else { throw RelayError.invalidResponse }
        return RelayPairing(origin: origin, pairId: response.pairId.lowercased(), pairSecret: response.pairSecret, expiresAt: response.expiresAt, messageKey: try randomKey(), replyKey: try randomKey())
    }

    public func messages(session relaySession: RelaySession, beforeSeq: Int? = nil) async throws -> RelayMessagePage {
        var query: [URLQueryItem] = []
        if let beforeSeq {
            guard beforeSeq > 0 else { throw RelayError.invalidValue("before sequence") }
            query.append(URLQueryItem(name: "beforeSeq", value: String(beforeSeq)))
        }
        let page: RelayMessagePage = try await request(path: "/api/v1/messages", query: query, session: relaySession)
        guard page.pairId.lowercased() == relaySession.pairId else { throw RelayError.invalidResponse }
        try page.messages.forEach { message in
            guard message.assets.count <= 2, RelayValidation.isWechatUserId(message.wechatUserId) else { throw RelayError.invalidResponse }
            try RelayCrypto.validateMessageEnvelope(message)
        }
        return page
    }

    public func waitForMessages(session relaySession: RelaySession, afterSeq: Int) async throws -> RelayMessageWaitResult {
        guard afterSeq >= 0 else { throw RelayError.invalidValue("after sequence") }
        return try await request(path: "/api/v1/messages/wait", query: [URLQueryItem(name: "afterSeq", value: String(afterSeq))], session: relaySession, timeout: 60)
    }

    public func streamEvents(session relaySession: RelaySession, afterSeq: Int, onEvent: @Sendable (RelayStreamEvent) async throws -> Void) async throws {
        guard relaySession.origin == origin else { throw RelayError.invalidOrigin }
        guard afterSeq >= 0 else { throw RelayError.invalidValue("after sequence") }
        var components = URLComponents(url: origin, resolvingAgainstBaseURL: false)!
        components.path = "/api/v1/ios/events"
        components.queryItems = [URLQueryItem(name: "afterSeq", value: String(afterSeq))]
        guard let url = components.url else { throw RelayError.invalidOrigin }
        var request = URLRequest(url: url)
        request.timeoutInterval = 60
        request.setValue(originString, forHTTPHeaderField: "Origin")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue(relaySession.ackToken, forHTTPHeaderField: "X-AWR-Ack-Token")
        request.setValue(relaySession.pairId, forHTTPHeaderField: "X-AWR-Pair-Id")
        let configuration = sessionConfiguration.copy() as! URLSessionConfiguration
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 3_600
        let connection = URLSession(configuration: configuration, delegate: RelayStreamRedirectDelegate(), delegateQueue: nil)
        defer { connection.invalidateAndCancel() }
        try await withTaskCancellationHandler {
            let (bytes, response) = try await connection.bytes(for: request)
            guard let http = response as? HTTPURLResponse, let responseURL = http.url,
                  RelayOrigin.isSameOrigin(responseURL, as: origin) else { throw RelayError.redirectRejected }
            guard http.statusCode == 200 else { throw RelayError.httpStatus(http.statusCode) }
            guard http.mimeType?.lowercased() == "text/event-stream" else { throw RelayError.invalidResponse }
            var parser = RelayStreamParser()
            for try await byte in bytes {
                try Task.checkCancellation()
                if let event = try parser.append(byte) { try await onEvent(event) }
            }
            // EOF is a disconnect, even when the HTTP response ended successfully.
            throw RelayError.invalidResponse
        } onCancel: {
            connection.invalidateAndCancel()
        }
    }

    public func asset(session relaySession: RelaySession, id: UUID) async throws -> RelayAsset {
        try await request(path: "/api/v1/assets/\(id.uuidString.lowercased())", session: relaySession)
    }

    public func submitReply(session relaySession: RelaySession, request reply: RelayReplyRequest) async throws -> RelayReplyResult {
        try validateReplyRequest(reply, pairId: relaySession.pairId)
        return try await request(path: "/api/v1/replies", method: "POST", session: relaySession, body: try encodedBody(reply))
    }

    public func replyStatus(session relaySession: RelaySession, id: UUID) async throws -> RelayReplyResult {
        try await request(path: "/api/v1/replies/\(id.uuidString.lowercased())", session: relaySession)
    }

    public func policy(session relaySession: RelaySession) async throws -> RelayPolicy {
        try await request(path: "/api/v1/relay-policy", session: relaySession)
    }

    public func savePolicy(session relaySession: RelaySession, policy: RelayPolicy) async throws -> RelayPolicy {
        try validatePolicy(policy)
        return try await request(path: "/api/v1/relay-policy", method: "PUT", session: relaySession, body: try encodedBody(policy))
    }

    public func deviceStatus(session relaySession: RelaySession) async throws -> RelayDeviceStatus {
        try await request(path: "/api/v1/ios/status", session: relaySession)
    }

    public func contacts(session relaySession: RelaySession) async throws -> [RelayContactSnapshot] {
        let response: RelayContactsResponse = try await request(path: "/api/v1/ios/contacts", session: relaySession)
        guard response.snapshots.count <= 2,
              Set(response.snapshots.map(\.wechatUserId)).count == response.snapshots.count else {
            throw RelayError.invalidResponse
        }
        for snapshot in response.snapshots {
            guard snapshot.v == 1, RelayValidation.isUUIDv7(snapshot.id), RelayValidation.isDeviceId(snapshot.deviceId),
                  RelayValidation.isWechatUserId(snapshot.wechatUserId), snapshot.capturedAt > 0 else {
                throw RelayError.invalidResponse
            }
            try RelayCrypto.validateContactsEnvelope(snapshot)
        }
        return response.snapshots
    }

    public func updatePush(session relaySession: RelaySession, token: String?, environment: RelayPushEnvironment, previewEnabled: Bool) async throws {
        let body = RelayPushRequest(deviceToken: token, environment: environment, previewEnabled: previewEnabled)
        let _: EmptyResponse = try await request(path: "/api/v1/ios/push", method: "PUT", session: relaySession, body: try encodedBody(body))
    }

    private func request<Response: Decodable>(path: String, method: String = "GET", query: [URLQueryItem] = [], session relaySession: RelaySession? = nil, testToken: String? = nil, ackToken: String? = nil, body: Data? = nil, timeout: TimeInterval? = nil) async throws -> Response {
        guard relaySession?.origin == nil || relaySession!.origin == origin else { throw RelayError.invalidOrigin }
        var components = URLComponents(url: origin, resolvingAgainstBaseURL: false)!
        components.path = path
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw RelayError.invalidOrigin }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout ?? self.timeout
        request.setValue(originString, forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let relaySession {
            request.setValue(relaySession.ackToken, forHTTPHeaderField: "X-AWR-Ack-Token")
            request.setValue(relaySession.pairId, forHTTPHeaderField: "X-AWR-Pair-Id")
        }
        if let ackToken { request.setValue(ackToken, forHTTPHeaderField: "X-AWR-Ack-Token") }
        if let testToken { request.setValue(testToken, forHTTPHeaderField: "X-AWR-Test-Token") }
        if let body {
            guard body.count <= 16_384 else { throw RelayError.invalidValue("request body") }
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let responseDelegate = RelayResponseDelegate(origin: origin, maximumResponseBytes: maximumResponseBytes)
        let data = try await responseDelegate.data(for: request, configuration: sessionConfiguration)
        if Response.self == EmptyResponse.self { return EmptyResponse() as! Response }
        do {
            return try JSONDecoder().decode(Response.self, from: data)
        } catch {
            throw RelayError.invalidResponse
        }
    }

    private func validateReplyRequest(_ reply: RelayReplyRequest, pairId: String) throws {
        guard (reply.v == 2 || reply.v == 3), RelayValidation.isDeviceId(reply.deviceId), RelayValidation.isWechatUserId(reply.wechatUserId), reply.createdAt > 0, reply.replyEnvelope.alg == "A256GCM", reply.replyEnvelope.kid == "phase1-reply" else { throw RelayError.invalidValue("reply") }
        let id = reply.id.uuidString.lowercased()
        let target = reply.targetMessageId.uuidString.lowercased()
        let expected: String
        if reply.v == 3 {
            expected = "AWR1|I2A|3|CONVERSATION_SEND|\(pairId)|\(id)|\(reply.deviceId)|\(target)|\(reply.createdAt)|\(reply.wechatUserId)"
        } else {
            expected = "AWR1|I2A|\(id)|\(reply.deviceId)|\(target)|\(reply.createdAt)|\(reply.wechatUserId)"
        }
        guard reply.replyEnvelope.aad == expected else { throw RelayError.invalidValue("reply AAD") }
    }

    private func validatePolicy(_ policy: RelayPolicy) throws {
        guard !policy.weekdays.isEmpty, Set(policy.weekdays).count == policy.weekdays.count, policy.weekdays.allSatisfy({ (1...7).contains($0) }), policy.timezone == "Asia/Shanghai", Self.isTime(policy.start), Self.isTime(policy.end), policy.start < policy.end else { throw RelayError.invalidValue("relay policy") }
    }

    private static func isTime(_ value: String) -> Bool {
        value.range(of: "^(?:[01]\\d|2[0-3]):[0-5]\\d$", options: .regularExpression) != nil
    }

    private func encodedBody<T: Encodable>(_ value: T) throws -> Data {
        let data = try JSONEncoder().encode(value)
        guard data.count <= 16_384 else { throw RelayError.invalidValue("request body") }
        return data
    }

    private func randomKey() throws -> Data {
        var data = Data(count: 32)
        let status = data.withUnsafeMutableBytes { bytes in SecRandomCopyBytes(kSecRandomDefault, 32, bytes.baseAddress!) }
        guard status == errSecSuccess else { throw RelayError.cryptographyFailed }
        return data
    }
}

public struct RelayMessagePage: Codable, Sendable, Equatable {
    public let pairId: String
    public let messages: [RelayMessage]
    public let nextCursor: String?
    public let hasMore: Bool
}

public struct RelayMessageWaitResult: Codable, Sendable, Equatable {
    public let available: Bool
    public let latestSeq: Int
}

private struct RelaySessionResponse: Decodable { let ackToken: String }
private struct RelayPairingResponse: Decodable { let pairId: String; let pairSecret: String; let expiresAt: Int64 }
private struct RelayContactsResponse: Decodable { let snapshots: [RelayContactSnapshot] }
private struct RelayPushRequest: Encodable {
    let deviceToken: String?
    let environment: RelayPushEnvironment
    let previewEnabled: Bool

    enum CodingKeys: String, CodingKey { case deviceToken, environment, previewEnabled }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        if let deviceToken { try values.encode(deviceToken, forKey: .deviceToken) }
        else { try values.encodeNil(forKey: .deviceToken) }
        try values.encode(environment, forKey: .environment)
        try values.encode(previewEnabled, forKey: .previewEnabled)
    }
}
private struct EmptyBody: Encodable {}
private struct EmptyResponse: Decodable {}

private final class RelayResponseDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let origin: URL
    private let maximumResponseBytes: Int
    private let lock = NSLock()
    private var buffer = Data()
    private var continuation: CheckedContinuation<Data, Error>?
    private var activeSession: URLSession?
    private var activeTask: URLSessionDataTask?
    private var completed = false

    init(origin: URL, maximumResponseBytes: Int) {
        self.origin = origin
        self.maximumResponseBytes = maximumResponseBytes
    }

    func data(for request: URLRequest, configuration: URLSessionConfiguration) async throws -> Data {
        try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                lock.lock()
                self.continuation = continuation
                let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
                let task = session.dataTask(with: request)
                self.activeSession = session
                self.activeTask = task
                lock.unlock()
                if Task.isCancelled { cancel() }
                else { task.resume() }
            }
        }, onCancel: { [weak self] in
            self?.cancel()
        })
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        guard let url = request.url, RelayOrigin.isSameOrigin(url, as: origin) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse, completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse,
              let responseURL = http.url,
              RelayOrigin.isSameOrigin(responseURL, as: origin) else {
            finish(.failure(RelayError.redirectRejected))
            completionHandler(.cancel)
            return
        }
        guard (200...299).contains(http.statusCode) else {
            finish(.failure(RelayError.httpStatus(http.statusCode)))
            completionHandler(.cancel)
            return
        }
        if response.expectedContentLength > Int64(maximumResponseBytes) {
            finish(.failure(RelayError.responseTooLarge))
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        let exceedsLimit = data.count > maximumResponseBytes - buffer.count
        if !exceedsLimit { buffer.append(data) }
        lock.unlock()
        if exceedsLimit {
            finish(.failure(RelayError.responseTooLarge))
            dataTask.cancel()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error {
            finish(.failure(error))
            return
        }
        lock.lock()
        let data = buffer
        lock.unlock()
        finish(.success(data))
    }

    private func finish(_ result: Result<Data, Error>) {
        lock.lock()
        guard !completed, let continuation else { lock.unlock(); return }
        completed = true
        self.continuation = nil
        let session = activeSession
        activeSession = nil
        activeTask = nil
        lock.unlock()
        session?.finishTasksAndInvalidate()
        continuation.resume(with: result)
    }

    private func cancel() {
        lock.lock()
        let task = activeTask
        lock.unlock()
        task?.cancel()
        finish(.failure(CancellationError()))
    }
}
