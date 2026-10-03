import Foundation
import Testing
@testable import RelayCore

struct RelayNativeSlotsTests {
    private let key = Data(repeating: 7, count: 32)
    private let pair = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b2"
    private let original = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b3")!
    private let second = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b4")!
    private let playback = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b5")!

    private func message() throws -> RelayMessage {
        let id = UUID(uuidString: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0")!
        let preview = try RelayCrypto.encrypt(Data("{\"sender\":\"sender\",\"body\":\"text\"}".utf8), key: key, kid: "phase1", aad: "AWR1|A2I|\(id.uuidString.lowercased())|tablet-device-0001|1|1788148800000|0")
        return try RelayMessage(messageId: id, deviceId: "tablet-device-0001", seq: 1, createdAt: 1_788_148_800_000, wechatUserId: 0, replyCapable: true, conversationSendCapable: false, previewEnvelope: preview, assets: [], receivedAt: 1_788_148_800_000, hasNativeContent: true, nativeVersion: 8, nativeAssetSlots: [
            RelayNativeAssetSlot(id: original, kind: .audio, role: .original),
            RelayNativeAssetSlot(id: playback, kind: .audio, role: .playback, derivedFrom: original),
            RelayNativeAssetSlot(id: second, kind: .file, role: .original)
        ])
    }

    private func content(_ message: RelayMessage) -> RelayNativeContent {
        RelayNativeContent(v: 1, conversationId: "wxid_friend", conversationName: "Friend", accountFingerprint: String(repeating: "a", count: 64), senderId: "sender", senderName: "Sender", isOutgoing: false, kind: .record, text: "Text survives pending attachments", rawXML: "<record/>", attachments: message.nativeAssetSlots.map { RelayNativeContent.Attachment(assetId: $0.id, name: "Attachment", recordItemIndex: 0) }, records: [RelayNativeContent.Record(senderName: "Sender", kind: .record, text: "Record body", rawXML: nil)])
    }

    @Test func unknownSlotsPermitContentAndBindThePairAndVersion() throws {
        let message = try message()
        let body = content(message)
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(body), key: key, kid: "phase2-content", aad: "AWR1|A2I_CONTENT|8|\(pair)|\(message.id.uuidString.lowercased())|tablet-device-0001|1|1788148800000|0")
        #expect(message.nativeAssets.isEmpty && message.hasUnresolvedNativeSlots)
        #expect(try RelayCrypto.decryptNativeContent(envelope, for: message, messageKey: key, pairID: pair) == body)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeContent(envelope, for: message, messageKey: key, pairID: UUID().uuidString) }
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeContent(envelope, for: message, messageKey: key) }
        let legacy = try RelayMessage(messageId: message.id, deviceId: message.deviceId, seq: message.seq, createdAt: message.createdAt, wechatUserId: message.wechatUserId, replyCapable: true, conversationSendCapable: false, previewEnvelope: message.previewEnvelope, assets: [], receivedAt: message.receivedAt, hasNativeContent: true, nativeVersion: 7)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeContent(envelope, for: legacy, messageKey: key, pairID: pair) }
        var object = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(message)) as? [String: Any])
        object["nativeVersion"] = 7
        #expect(throws: (any Error).self) { try JSONDecoder().decode(RelayMessage.self, from: JSONSerialization.data(withJSONObject: object)) }
        object["nativeVersion"] = 8
        object.removeValue(forKey: "nativeAssetSlots")
        #expect(throws: RelayError.invalidResponse) { try JSONDecoder().decode(RelayMessage.self, from: JSONSerialization.data(withJSONObject: object)) }
    }

    @Test func reconnectResolvesOneSlotIntoTheSameMessageAndRejectsReplacement() throws {
        let before = try message()
        let asset = try RelayNativeAssetMetadata(id: original, kind: .audio, mimeType: "audio/silk", byteLength: 3, role: .original)
        let after = try before.mergingNativeManifest(slots: before.nativeAssetSlots, assets: [asset])
        #expect(after.id == before.id && after.seq == before.seq && after.previewEnvelope == before.previewEnvelope)
        #expect(after.nativeAssets == [asset] && after.hasUnresolvedNativeSlots)
        #expect(try after.mergingNativeManifest(slots: before.nativeAssetSlots, assets: [asset]) == after)
        #expect(try after.mergingNativeManifest(slots: before.nativeAssetSlots, assets: []).nativeAssets == [asset])
        try content(before).validate(for: after)
        let changed = try RelayNativeAssetMetadata(id: original, kind: .audio, mimeType: "audio/wav", byteLength: 3, role: .original)
        #expect(throws: RelayError.invalidResponse) { try after.mergingNativeManifest(slots: before.nativeAssetSlots, assets: [changed]) }
        let wrong = try RelayNativeAssetMetadata(id: second, kind: .audio, mimeType: "audio/wav", byteLength: 3, role: .original)
        #expect(throws: RelayError.invalidResponse) { try before.mergingNativeManifest(slots: before.nativeAssetSlots, assets: [wrong]) }
        #expect(throws: RelayError.invalidResponse) { try before.mergingNativeManifest(slots: Array(before.nativeAssetSlots.dropLast()), assets: []) }
    }

    @Test func playbackCanResolveBeforeOriginalWithoutInventingMetadata() throws {
        let before = try message()
        let asset = try RelayNativeAssetMetadata(id: playback, kind: .audio, mimeType: "audio/wav", byteLength: 3, role: .playback, derivedFrom: original)
        let after = try before.mergingNativeManifest(slots: before.nativeAssetSlots, assets: [asset])
        #expect(after.nativeAssets == [asset])
        let data = Data([1, 2, 3])
        let envelope = try RelayCrypto.encrypt(data, key: key, kid: "phase2-asset", aad: "AWR1|A2I_ASSET|8|\(pair)|\(after.id.uuidString.lowercased())|\(playback.uuidString.lowercased())|tablet-device-0001|1|1788148800000|0|audio|audio/wav|3|playback|\(original.uuidString.lowercased())")
        let sealed = try JSONDecoder().decode(RelayNativeAsset.self, from: JSONSerialization.data(withJSONObject: ["id": playback.uuidString, "kind": "audio", "mimeType": "audio/wav", "byteLength": 3, "role": "playback", "derivedFrom": original.uuidString, "envelope": JSONSerialization.jsonObject(with: JSONEncoder().encode(envelope))]))
        #expect(try RelayCrypto.decryptNativeAsset(sealed, for: after, messageKey: key, pairID: pair) == data)
        #expect(throws: RelayError.invalidEnvelope) { try RelayCrypto.decryptNativeAsset(sealed, for: after, messageKey: key, pairID: UUID().uuidString) }
    }

    @Test func lateImagePreviewUpdatesCachedMessageWhileOriginalRemainsPending() throws {
        let base = try message()
        let slots = [RelayNativeAssetSlot(id: original, kind: .image, role: .original),
                     RelayNativeAssetSlot(id: playback, kind: .image, role: .playback, derivedFrom: original)]
        let cached = try RelayMessage(messageId: base.id, deviceId: base.deviceId, seq: base.seq, createdAt: base.createdAt, wechatUserId: base.wechatUserId, replyCapable: true, conversationSendCapable: false, previewEnvelope: base.previewEnvelope, assets: [], receivedAt: base.receivedAt, hasNativeContent: true, nativeVersion: 8, nativeAssetSlots: slots)
        let preview = try RelayNativeAssetMetadata(id: playback, kind: .image, mimeType: "image/jpeg", byteLength: 305148, role: .playback, derivedFrom: original)
        let refreshed = try cached.mergingNativeManifest(slots: slots, assets: [preview])
        #expect(refreshed.id == cached.id && refreshed.seq == cached.seq && refreshed.previewEnvelope == cached.previewEnvelope)
        #expect(refreshed.nativeAssets == [preview] && refreshed.hasUnresolvedNativeSlots)
        #expect(try refreshed.mergingNativeManifest(slots: slots, assets: []).nativeAssets == [preview])
        let full = try RelayNativeAssetMetadata(id: original, kind: .image, mimeType: "image/jpeg", byteLength: 2320802, role: .original)
        let upgraded = try refreshed.mergingNativeManifest(slots: slots, assets: [full, preview])
        #expect(!upgraded.hasUnresolvedNativeSlots && upgraded.nativeAssets.contains(full) && upgraded.nativeAssets.contains(preview))
        let changed = try RelayNativeAssetMetadata(id: playback, kind: .image, mimeType: "image/jpeg", byteLength: 1, role: .playback, derivedFrom: original)
        #expect(throws: RelayError.invalidResponse) { try refreshed.mergingNativeManifest(slots: slots, assets: [changed]) }
    }

    @Test func relabelingVersionOrPairCannotReuseTheOriginalGCMTag() throws {
        let target = try message()
        let body = try JSONEncoder().encode(content(target))
        let legacyAAD = "AWR1|A2I_CONTENT|7|\(target.id.uuidString.lowercased())|tablet-device-0001|1|1788148800000|0"
        let legacy = try RelayCrypto.encrypt(body, key: key, kid: "phase2-content", aad: legacyAAD)
        let relabeled = RelayEncryptedEnvelope(alg: legacy.alg, kid: legacy.kid, iv: legacy.iv, aad: RelayCrypto.nativeContentAAD(target, pairID: pair), ct: legacy.ct)
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decryptNativeContent(relabeled, for: target, messageKey: key, pairID: pair) }
        let original = try RelayCrypto.encrypt(body, key: key, kid: "phase2-content", aad: RelayCrypto.nativeContentAAD(target, pairID: pair))
        let otherPair = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b9"
        let moved = RelayEncryptedEnvelope(alg: original.alg, kid: original.kid, iv: original.iv, aad: RelayCrypto.nativeContentAAD(target, pairID: otherPair), ct: original.ct)
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decryptNativeContent(moved, for: target, messageKey: key, pairID: otherPair) }
        let metadata = try RelayNativeAssetMetadata(id: self.original, kind: .audio, mimeType: "audio/silk", byteLength: 3, role: .original)
        let resolved = try target.mergingNativeManifest(slots: target.nativeAssetSlots, assets: [metadata])
        func asset(_ envelope: RelayEncryptedEnvelope) -> RelayNativeAsset {
            RelayNativeAsset(id: metadata.id, kind: metadata.kind, mimeType: metadata.mimeType, byteLength: metadata.byteLength, role: metadata.role, derivedFrom: nil, envelope: envelope)
        }
        let legacyAssetAAD = "AWR1|A2I_ASSET|7|\(target.id.uuidString.lowercased())|\(metadata.id.uuidString.lowercased())|tablet-device-0001|1|1788148800000|0|audio|audio/silk|3|original|"
        let legacyAsset = try RelayCrypto.encrypt(Data([1, 2, 3]), key: key, kid: "phase2-asset", aad: legacyAssetAAD)
        let relabeledAsset = RelayEncryptedEnvelope(alg: legacyAsset.alg, kid: legacyAsset.kid, iv: legacyAsset.iv, aad: RelayCrypto.nativeAssetAAD(metadata, message: resolved, pairID: pair), ct: legacyAsset.ct)
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decryptNativeAsset(asset(relabeledAsset), for: resolved, messageKey: key, pairID: pair) }
        let originalAsset = try RelayCrypto.encrypt(Data([1, 2, 3]), key: key, kid: "phase2-asset", aad: RelayCrypto.nativeAssetAAD(metadata, message: resolved, pairID: pair))
        let movedAsset = RelayEncryptedEnvelope(alg: originalAsset.alg, kid: originalAsset.kid, iv: originalAsset.iv, aad: RelayCrypto.nativeAssetAAD(metadata, message: resolved, pairID: otherPair), ct: originalAsset.ct)
        #expect(throws: RelayError.cryptographyFailed) { try RelayCrypto.decryptNativeAsset(asset(movedAsset), for: resolved, messageKey: key, pairID: otherPair) }
    }

    @Test func sharingUsesActualMimeWithoutReplacingAnExistingFilename() throws {
        let asset = try RelayNativeAssetMetadata(id: original, kind: .file, mimeType: "application/pdf", byteLength: 3, role: .original)
        #expect(asset.shareFilename("original") == "original.pdf")
        #expect(asset.shareFilename("年度报告") == "年度报告.pdf")
        #expect(asset.shareFilename("annual-report.pdf") == "annual-report.pdf")
        #expect(asset.shareFilename("original.custom") == "original.custom")
        #expect(asset.shareFilename("../annual-report.pdf") == "annual-report.pdf")
        #expect(asset.shareFilename(".env") == ".env")
        #expect(asset.shareFilename("..") == "attachment-\(original.uuidString.lowercased()).pdf")
    }
}
