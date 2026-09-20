import CryptoKit
import Foundation
import Security

public enum RelayCrypto {
    public static func decryptPreview(_ message: RelayMessage, messageKey: Data) throws -> RelayPreview {
        try validateMessageEnvelope(message)
        let plaintext = try decrypt(message.previewEnvelope, key: messageKey, expectedKid: "phase1", maximumCiphertextBytes: 4_096)
        do {
            return try JSONDecoder().decode(RelayPreview.self, from: plaintext)
        } catch {
            throw RelayError.invalidResponse
        }
    }

    public static func decryptAsset(_ asset: RelayAsset, for message: RelayMessage, messageKey: Data) throws -> Data {
        try validateAssetEnvelope(asset, message: message)
        return try decrypt(asset.envelope, key: messageKey, expectedKid: "phase1-asset", maximumCiphertextBytes: 8 * 1_024 * 1_024 + 16)
    }

    public static func makeReply(session: RelaySession, target: RelayMessage, body: String, now: Date = Date()) throws -> RelayReplyRequest {
        guard target.replyCapable else { throw RelayError.invalidValue("reply target") }
        let characters = Array(body)
        guard !characters.isEmpty, characters.count <= 1_000 else { throw RelayError.invalidValue("reply body") }
        let id = try UUIDv7.make(now: now)
        let createdAt = Int64(now.timeIntervalSince1970 * 1_000)
        let conversationSend = target.conversationSendCapable
        let version = conversationSend ? 3 : 2
        let aad: String
        let plaintext: Data
        if conversationSend {
            aad = "AWR1|I2A|3|CONVERSATION_SEND|\(session.pairId)|\(id.uuidString.lowercased())|\(target.deviceId)|\(target.messageId.uuidString.lowercased())|\(createdAt)|\(target.wechatUserId)"
            let preview = try decryptPreview(target, messageKey: session.messageKey)
            plaintext = try JSONSerialization.data(withJSONObject: ["body": body, "conversationTitle": preview.sender], options: [])
        } else {
            aad = "AWR1|I2A|\(id.uuidString.lowercased())|\(target.deviceId)|\(target.messageId.uuidString.lowercased())|\(createdAt)|\(target.wechatUserId)"
            plaintext = try JSONSerialization.data(withJSONObject: ["body": body], options: [])
        }
        let envelope = try encrypt(plaintext, key: session.replyKey, kid: "phase1-reply", aad: aad)
        return RelayReplyRequest(v: version, id: id, targetMessageId: target.messageId, deviceId: target.deviceId, wechatUserId: target.wechatUserId, createdAt: createdAt, replyEnvelope: envelope)
    }

    public static func makePairingCode(_ pairing: RelayPairing) throws -> String {
        guard RelayValidation.isUUID(pairing.pairId), !pairing.pairSecret.isEmpty, pairing.messageKey.count == 32, pairing.replyKey.count == 32 else {
            throw RelayError.invalidValue("pairing")
        }
        let payload: [String: Any] = [
            "v": 1,
            "origin": RelayOrigin.string(try RelayOrigin.normalize(pairing.origin)),
            "pairId": pairing.pairId,
            "pairSecret": pairing.pairSecret,
            "expiresAt": pairing.expiresAt,
            "kA2I": base64url(pairing.messageKey),
            "kI2A": base64url(pairing.replyKey)
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        return "AWR1:\(base64url(data))"
    }

    public static func encrypt(_ plaintext: Data, key: Data, kid: String, aad: String) throws -> RelayEncryptedEnvelope {
        guard key.count == 32, !aad.isEmpty else { throw RelayError.invalidValue("encryption input") }
        var iv = Data(count: 12)
        let status = iv.withUnsafeMutableBytes { bytes in
            SecRandomCopyBytes(kSecRandomDefault, 12, bytes.baseAddress!)
        }
        guard status == errSecSuccess else { throw RelayError.cryptographyFailed }
        do {
            let nonce = try AES.GCM.Nonce(data: iv)
            let box = try AES.GCM.seal(plaintext, using: SymmetricKey(data: key), nonce: nonce, authenticating: Data(aad.utf8))
            return RelayEncryptedEnvelope(kid: kid, iv: base64url(iv), aad: aad, ct: base64url(box.ciphertext + box.tag))
        } catch {
            throw RelayError.cryptographyFailed
        }
    }

    static func validateMessageEnvelope(_ message: RelayMessage) throws {
        guard RelayValidation.isDeviceId(message.deviceId), message.seq > 0, message.createdAt > 0,
              RelayValidation.isWechatUserId(message.wechatUserId), message.previewEnvelope.alg == "A256GCM", message.previewEnvelope.kid == "phase1" else {
            throw RelayError.invalidEnvelope
        }
        let base = "AWR1|A2I|\(message.messageId.uuidString.lowercased())|\(message.deviceId)|\(message.seq)|\(message.createdAt)"
        let expected = "\(base)|\(message.wechatUserId)"
        let legacyAllowed = message.wechatUserId == 0 && message.previewEnvelope.aad == base
        guard legacyAllowed || message.previewEnvelope.aad == expected else { throw RelayError.invalidEnvelope }
    }

    static func validateAssetEnvelope(_ asset: RelayAsset, message: RelayMessage) throws {
        guard RelayValidation.isImageMIMEType(asset.mimeType), RelayValidation.isValidAssetSize(width: asset.width, height: asset.height), asset.envelope.alg == "A256GCM", asset.envelope.kid == "phase1-asset",
              message.assets.contains(where: { metadata in metadata.id == asset.id && metadata.kind == asset.kind && metadata.mimeType.lowercased() == asset.mimeType.lowercased() && metadata.width == asset.width && metadata.height == asset.height }) else {
            throw RelayError.invalidEnvelope
        }
        let base = "AWR1|A2I_ASSET|\(asset.id.uuidString.lowercased())|\(message.deviceId)|\(message.seq)|\(message.createdAt)"
        let expected = "\(base)|\(message.wechatUserId)"
        let legacyAllowed = message.wechatUserId == 0 && asset.envelope.aad == base
        guard legacyAllowed || asset.envelope.aad == expected else { throw RelayError.invalidEnvelope }
    }

    static func decrypt(_ envelope: RelayEncryptedEnvelope, key: Data, expectedKid: String, maximumCiphertextBytes: Int) throws -> Data {
        guard envelope.alg == "A256GCM", envelope.kid == expectedKid, key.count == 32,
              let iv = decodeBase64url(envelope.iv), iv.count == 12,
              let ciphertextAndTag = decodeBase64url(envelope.ct), ciphertextAndTag.count >= 17, ciphertextAndTag.count <= maximumCiphertextBytes else {
            throw RelayError.invalidEnvelope
        }
        do {
            let split = ciphertextAndTag.count - 16
            let box = try AES.GCM.SealedBox(nonce: AES.GCM.Nonce(data: iv), ciphertext: ciphertextAndTag.prefix(split), tag: ciphertextAndTag.suffix(16))
            return try AES.GCM.open(box, using: SymmetricKey(data: key), authenticating: Data(envelope.aad.utf8))
        } catch {
            throw RelayError.cryptographyFailed
        }
    }

    static func base64url(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }

    static func decodeBase64url(_ value: String) -> Data? {
        guard !value.isEmpty, value.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil else { return nil }
        var base64 = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        base64.append(String(repeating: "=", count: (4 - base64.count % 4) % 4))
        return Data(base64Encoded: base64)
    }
}

public enum UUIDv7 {
    public static func make(now: Date = Date()) throws -> UUID {
        let milliseconds = UInt64(max(0, now.timeIntervalSince1970 * 1_000))
        guard milliseconds <= 0xFFFF_FFFF_FFFF else { throw RelayError.invalidValue("UUIDv7 time") }
        var bytes = [UInt8](repeating: 0, count: 16)
        for index in 0..<6 { bytes[index] = UInt8((milliseconds >> UInt64((5 - index) * 8)) & 0xff) }
        let status = bytes.withUnsafeMutableBytes { rawBuffer in
            SecRandomCopyBytes(kSecRandomDefault, 10, rawBuffer.baseAddress!.advanced(by: 6))
        }
        guard status == errSecSuccess else { throw RelayError.cryptographyFailed }
        bytes[6] = (bytes[6] & 0x0f) | 0x70
        bytes[8] = (bytes[8] & 0x3f) | 0x80
        return UUID(uuid: (bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7], bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]))
    }
}
