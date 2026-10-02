import CryptoKit
import Foundation

public struct RelayNativeAssetMetadata: Codable, Sendable, Equatable, Identifiable {
    public enum Kind: String, Codable, Sendable { case image, sticker, audio, video, file }
    public enum Role: String, Codable, Sendable { case original, playback }
    public let id: UUID
    public let kind: Kind
    public let mimeType: String
    public let byteLength: Int
    public let role: Role
    public let derivedFrom: UUID?

    public init(id: UUID, kind: Kind, mimeType: String, byteLength: Int, role: Role, derivedFrom: UUID? = nil) throws {
        guard (1...8_388_608).contains(byteLength),
              mimeType.range(of: "^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$", options: .regularExpression) != nil,
              mimeType.utf8.count <= 128,
              (role == .original && derivedFrom == nil) || (role == .playback && derivedFrom != nil && derivedFrom != id) else {
            throw RelayError.invalidValue("native asset metadata")
        }
        self.id = id
        self.kind = kind
        self.mimeType = mimeType
        self.byteLength = byteLength
        self.role = role
        self.derivedFrom = derivedFrom
    }

    private enum CodingKeys: String, CodingKey { case id, kind, mimeType, byteLength, role, derivedFrom }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(id: values.decode(UUID.self, forKey: .id), kind: values.decode(Kind.self, forKey: .kind), mimeType: values.decode(String.self, forKey: .mimeType), byteLength: values.decode(Int.self, forKey: .byteLength), role: values.decode(Role.self, forKey: .role), derivedFrom: values.decodeIfPresent(UUID.self, forKey: .derivedFrom))
    }

    public static func validate(_ assets: [Self]) throws {
        guard assets.count <= 8, Set(assets.map(\.id)).count == assets.count else { throw RelayError.invalidResponse }
        for asset in assets where asset.role == .playback {
            guard let original = assets.first(where: { $0.id == asset.derivedFrom }), original.role == .original,
                  original.kind == asset.kind else { throw RelayError.invalidResponse }
        }
    }
}

public struct RelayNativeAsset: Codable, Sendable, Equatable {
    public let id: UUID
    public let kind: RelayNativeAssetMetadata.Kind
    public let mimeType: String
    public let byteLength: Int
    public let role: RelayNativeAssetMetadata.Role
    public let derivedFrom: UUID?
    public let envelope: RelayEncryptedEnvelope

    public var metadata: RelayNativeAssetMetadata {
        get throws { try RelayNativeAssetMetadata(id: id, kind: kind, mimeType: mimeType, byteLength: byteLength, role: role, derivedFrom: derivedFrom) }
    }
}

public struct RelayNativeContent: Codable, Sendable, Equatable {
    public enum Kind: String, Codable, Sendable { case text, image, audio, video, file, sticker, reference, record, link, unsupported }
    public struct Attachment: Codable, Sendable, Equatable {
        public let assetId: UUID
        public let name: String
        public let sha256: String?
        public let recordItemIndex: Int?

        public init(assetId: UUID, name: String, sha256: String? = nil, recordItemIndex: Int? = nil) {
            self.assetId = assetId
            self.name = name
            self.sha256 = sha256
            self.recordItemIndex = recordItemIndex
        }
    }
    public struct Record: Codable, Sendable, Equatable {
        public let senderName: String
        public let kind: Kind
        public let text: String
        public let rawXML: String?
    }
    public let v: Int
    public let conversationId: String
    public let conversationName: String
    public let senderId: String
    public let senderName: String
    public let isOutgoing: Bool?
    public let kind: Kind
    public let text: String
    public let rawXML: String?
    public let attachments: [Attachment]
    public let records: [Record]?

    public func validate(for message: RelayMessage) throws {
        guard v == 1, !conversationId.isEmpty, conversationId.utf8.count <= 1_024,
              attachments.count <= 8, Set(attachments.map(\.assetId)).count == attachments.count,
              Set(attachments.map(\.assetId)) == Set(message.nativeAssets.map(\.id)) else { throw RelayError.invalidResponse }
        for attachment in attachments {
            if let index = attachment.recordItemIndex {
                guard let records, records.indices.contains(index) else { throw RelayError.invalidResponse }
            }
            if let sha256 = attachment.sha256 {
                guard sha256.range(of: "^[a-fA-F0-9]{64}$", options: .regularExpression) != nil else { throw RelayError.invalidResponse }
            }
        }
    }
}

extension RelayCrypto {
    public static func decryptNativeContent(_ envelope: RelayEncryptedEnvelope, for message: RelayMessage, messageKey: Data) throws -> RelayNativeContent {
        guard message.hasNativeContent, !message.replyCapable, !message.conversationSendCapable,
              envelope.aad == nativeContentAAD(message) else { throw RelayError.invalidEnvelope }
        let plaintext = try decrypt(envelope, key: messageKey, expectedKid: "phase2-content", maximumCiphertextBytes: 1_024 * 1_024 + 16)
        let content: RelayNativeContent
        do { content = try JSONDecoder().decode(RelayNativeContent.self, from: plaintext) }
        catch { throw RelayError.invalidResponse }
        try content.validate(for: message)
        return content
    }

    public static func decryptNativeAsset(_ asset: RelayNativeAsset, for message: RelayMessage, messageKey: Data, sha256: String? = nil) throws -> Data {
        let metadata = try asset.metadata
        guard message.hasNativeContent, message.nativeAssets.contains(metadata),
              asset.envelope.aad == nativeAssetAAD(metadata, message: message) else { throw RelayError.invalidEnvelope }
        let plaintext = try decrypt(asset.envelope, key: messageKey, expectedKid: "phase2-asset", maximumCiphertextBytes: metadata.byteLength + 16)
        guard plaintext.count == metadata.byteLength else { throw RelayError.invalidEnvelope }
        if let sha256 {
            let actual = SHA256.hash(data: plaintext).map { String(format: "%02x", $0) }.joined()
            guard actual == sha256.lowercased() else { throw RelayError.invalidEnvelope }
        }
        return plaintext
    }

    static func nativeContentAAD(_ message: RelayMessage) -> String {
        "AWR1|A2I_CONTENT|6|\(message.id.uuidString.lowercased())|\(message.deviceId)|\(message.seq)|\(message.createdAt)|\(message.wechatUserId)"
    }

    static func nativeAssetAAD(_ asset: RelayNativeAssetMetadata, message: RelayMessage) -> String {
        "AWR1|A2I_ASSET|6|\(message.id.uuidString.lowercased())|\(asset.id.uuidString.lowercased())|\(message.deviceId)|\(message.seq)|\(message.createdAt)|\(message.wechatUserId)|\(asset.kind.rawValue)|\(asset.mimeType)|\(asset.byteLength)|\(asset.role.rawValue)|\(asset.derivedFrom?.uuidString.lowercased() ?? "")"
    }
}
