import Foundation

public enum RelayError: Error, Equatable, LocalizedError {
    case invalidOrigin
    case invalidValue(String)
    case invalidEnvelope
    case cryptographyFailed
    case responseTooLarge
    case httpStatus(Int)
    case redirectRejected
    case invalidResponse

    public var errorDescription: String? {
        switch self {
        case .invalidOrigin: return "Invalid relay origin"
        case .invalidValue(let name): return "Invalid \(name)"
        case .invalidEnvelope: return "Invalid encrypted envelope"
        case .cryptographyFailed: return "Encryption or decryption failed"
        case .responseTooLarge: return "Relay response is too large"
        case .httpStatus(let status): return "Relay request failed with HTTP \(status)"
        case .redirectRejected: return "Relay redirect changed origin"
        case .invalidResponse: return "Invalid relay response"
        }
    }
}

public struct RelaySession: Codable, Sendable, Equatable {
    public let origin: URL
    public let ackToken: String
    public let pairId: String
    public let messageKey: Data
    public let replyKey: Data

    public init(origin: URL, ackToken: String, pairId: String, messageKey: Data, replyKey: Data) throws {
        self.origin = try RelayOrigin.normalize(origin)
        guard !ackToken.isEmpty, RelayValidation.isUUID(pairId), messageKey.count == 32, replyKey.count == 32 else {
            throw RelayError.invalidValue("session")
        }
        self.ackToken = ackToken
        self.pairId = pairId.lowercased()
        self.messageKey = messageKey
        self.replyKey = replyKey
    }
}

public struct RelayEncryptedEnvelope: Codable, Sendable, Equatable {
    public let alg: String
    public let kid: String
    public let iv: String
    public let aad: String
    public let ct: String

    public init(alg: String = "A256GCM", kid: String, iv: String, aad: String, ct: String) {
        self.alg = alg
        self.kid = kid
        self.iv = iv
        self.aad = aad
        self.ct = ct
    }
}

public struct RelayPreview: Codable, Sendable, Equatable {
    public let sender: String
    public let body: String

    public init(sender: String, body: String) {
        self.sender = sender
        self.body = body
    }
}

public struct RelayAssetMetadata: Codable, Sendable, Equatable, Identifiable {
    public let id: UUID
    public let kind: Kind
    public let mimeType: String
    public let width: Int
    public let height: Int

    public enum Kind: String, Codable, Sendable { case avatar, image, sticker }

    public init(id: UUID, kind: Kind, mimeType: String, width: Int, height: Int) throws {
        guard RelayValidation.isImageMIMEType(mimeType), RelayValidation.isValidAssetSize(width: width, height: height) else {
            throw RelayError.invalidValue("asset metadata")
        }
        self.id = id
        self.kind = kind
        self.mimeType = mimeType
        self.width = width
        self.height = height
    }
}

public struct RelayAsset: Codable, Sendable, Equatable {
    public let id: UUID
    public let kind: RelayAssetMetadata.Kind
    public let mimeType: String
    public let width: Int
    public let height: Int
    public let envelope: RelayEncryptedEnvelope
}

public struct RelayMessage: Codable, Identifiable, Sendable, Equatable {
    public let messageId: UUID
    public let deviceId: String
    public let seq: Int
    public let createdAt: Int64
    public let wechatUserId: Int
    public let replyCapable: Bool
    public let conversationSendCapable: Bool
    public let previewEnvelope: RelayEncryptedEnvelope
    public let assets: [RelayAssetMetadata]
    public let receivedAt: Int64

    public var id: UUID { messageId }

    public init(messageId: UUID, deviceId: String, seq: Int, createdAt: Int64, wechatUserId: Int, replyCapable: Bool, conversationSendCapable: Bool, previewEnvelope: RelayEncryptedEnvelope, assets: [RelayAssetMetadata], receivedAt: Int64) throws {
        guard RelayValidation.isDeviceId(deviceId), seq > 0, createdAt > 0, receivedAt > 0,
              RelayValidation.isWechatUserId(wechatUserId), !(conversationSendCapable && !replyCapable), assets.count <= 2,
              Set(assets.map(\.id)).count == assets.count else {
            throw RelayError.invalidValue("message")
        }
        self.messageId = messageId
        self.deviceId = deviceId
        self.seq = seq
        self.createdAt = createdAt
        self.wechatUserId = wechatUserId
        self.replyCapable = replyCapable
        self.conversationSendCapable = conversationSendCapable
        self.previewEnvelope = previewEnvelope
        self.assets = assets
        self.receivedAt = receivedAt
    }
}

public struct RelayReplyRequest: Codable, Sendable, Equatable, Identifiable {
    public let v: Int
    public let id: UUID
    public let targetMessageId: UUID
    public let deviceId: String
    public let wechatUserId: Int
    public let createdAt: Int64
    public let replyEnvelope: RelayEncryptedEnvelope

    enum CodingKeys: String, CodingKey { case v, id, targetMessageId, deviceId, wechatUserId, createdAt, replyEnvelope }

    public init(v: Int, id: UUID, targetMessageId: UUID, deviceId: String, wechatUserId: Int, createdAt: Int64, replyEnvelope: RelayEncryptedEnvelope) {
        self.v = v
        self.id = id
        self.targetMessageId = targetMessageId
        self.deviceId = deviceId
        self.wechatUserId = wechatUserId
        self.createdAt = createdAt
        self.replyEnvelope = replyEnvelope
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        v = try values.decode(Int.self, forKey: .v)
        id = try values.decode(UUID.self, forKey: .id)
        targetMessageId = try values.decode(UUID.self, forKey: .targetMessageId)
        deviceId = try values.decode(String.self, forKey: .deviceId)
        wechatUserId = try values.decode(Int.self, forKey: .wechatUserId)
        createdAt = try values.decode(Int64.self, forKey: .createdAt)
        replyEnvelope = try values.decode(RelayEncryptedEnvelope.self, forKey: .replyEnvelope)
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(v, forKey: .v)
        try values.encode(id.uuidString.lowercased(), forKey: .id)
        try values.encode(targetMessageId.uuidString.lowercased(), forKey: .targetMessageId)
        try values.encode(deviceId, forKey: .deviceId)
        try values.encode(wechatUserId, forKey: .wechatUserId)
        try values.encode(createdAt, forKey: .createdAt)
        try values.encode(replyEnvelope, forKey: .replyEnvelope)
    }
}

public struct RelayReplyResult: Codable, Sendable, Equatable {
    public let replyId: UUID
    public let status: RelayReplyStatus
    public let targetMessageId: UUID?
    public let deviceId: String?
    public let wechatUserId: Int?
    public let createdAt: Int64?
    public let statusAt: Int64?
}

public enum RelayReplyStatus: String, Codable, Sendable, Equatable {
    case submitting = "SUBMITTING"
    case queued = "QUEUED"
    case deliveredToAndroid = "DELIVERED_TO_ANDROID"
    case sentToWechat = "SENT_TO_WECHAT"
    case notificationNotActive = "NOTIFICATION_NOT_ACTIVE"
    case wechatActionChanged = "WECHAT_ACTION_CHANGED"
    case remoteInputUnsupported = "REMOTE_INPUT_UNSUPPORTED"
    case pendingIntentCanceled = "PENDING_INTENT_CANCELED"
    case invalidReply = "INVALID_REPLY"
    case failed = "FAILED"

    public var isTerminal: Bool {
        switch self {
        case .submitting, .queued, .deliveredToAndroid: return false
        default: return true
        }
    }
}

public struct RelayPolicy: Codable, Sendable, Equatable {
    public let enabled: Bool
    public let scheduleEnabled: Bool
    public let weekdays: [Int]
    public let start: String
    public let end: String
    public let timezone: String
    public let active: Bool
    public let updatedAt: Int64

    public init(enabled: Bool, scheduleEnabled: Bool, weekdays: [Int], start: String, end: String, timezone: String = "Asia/Shanghai", active: Bool = false, updatedAt: Int64 = 0) {
        self.enabled = enabled
        self.scheduleEnabled = scheduleEnabled
        self.weekdays = weekdays
        self.start = start
        self.end = end
        self.timezone = timezone
        self.active = active
        self.updatedAt = updatedAt
    }

    public static let `default` = RelayPolicy(enabled: true, scheduleEnabled: false, weekdays: [1, 2, 3, 4, 5], start: "09:30", end: "18:00", timezone: "Asia/Shanghai", active: true, updatedAt: 0)
}

public struct RelayDeviceStatus: Codable, Sendable, Equatable {
    public let paired: Bool
    public let deviceId: String?
    public let lastSeenAt: Int64?
    public let serverTime: Int64
    public let pushConfigured: Bool
    public let pushRegistered: Bool

    public init(paired: Bool, deviceId: String?, lastSeenAt: Int64?, serverTime: Int64, pushConfigured: Bool, pushRegistered: Bool) {
        self.paired = paired
        self.deviceId = deviceId
        self.lastSeenAt = lastSeenAt
        self.serverTime = serverTime
        self.pushConfigured = pushConfigured
        self.pushRegistered = pushRegistered
    }
}

public enum RelayPushEnvironment: String, Codable, Sendable { case sandbox, production }

public struct RelayPairing: Codable, Sendable, Equatable {
    public let origin: URL
    public let pairId: String
    public let pairSecret: String
    public let expiresAt: Int64
    public let messageKey: Data
    public let replyKey: Data

    public func makeSession(ackToken: String) throws -> RelaySession {
        try RelaySession(origin: origin, ackToken: ackToken, pairId: pairId, messageKey: messageKey, replyKey: replyKey)
    }
}

enum RelayOrigin {
    static func normalize(_ url: URL) throws -> URL {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "https", components.host != nil,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              components.path.isEmpty || components.path == "/" else {
            throw RelayError.invalidOrigin
        }
        var normalized = components
        normalized.scheme = "https"
        normalized.path = ""
        guard let result = normalized.url else { throw RelayError.invalidOrigin }
        return result
    }

    static func string(_ origin: URL) -> String {
        let port = origin.port.map { ":\($0)" } ?? ""
        return "https://\(origin.host!.lowercased())\(port)"
    }

    static func isSameOrigin(_ url: URL, as origin: URL) -> Bool {
        url.scheme?.lowercased() == "https" && url.host?.lowercased() == origin.host?.lowercased() && url.port == origin.port && url.user == nil && url.password == nil
    }
}

enum RelayValidation {
    static func isUUID(_ value: String) -> Bool { UUID(uuidString: value) != nil }
    static func isWechatUserId(_ value: Int) -> Bool { value == 0 || value == 999 }
    static func isDeviceId(_ value: String) -> Bool {
        let pattern = "^[A-Za-z0-9_-]{16,128}$"
        return value.range(of: pattern, options: .regularExpression) != nil
    }
    static func isImageMIMEType(_ value: String) -> Bool { ["image/jpeg", "image/png", "image/webp"].contains(value.lowercased()) }
    static func isValidAssetSize(width: Int, height: Int) -> Bool { width >= 1 && height >= 1 && width <= 16_384 && height <= 16_384 && width <= 64_000_000 / height }
}
