import Foundation
import Testing
@testable import RelayCore

@Suite(.serialized)
struct RelaySourceTests {
    private func session(pairID: String = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0", key: UInt8 = 7) throws -> RelaySession {
        try RelaySession(origin: URL(string: "https://example.invalid")!, ackToken: "test-only", pairId: pairID, messageKey: Data(repeating: key, count: 32), replyKey: Data(repeating: key + 1, count: 32))
    }

    @Test func defaultsToTabletAndKeepsMixedSourcesExplicit() {
        #expect(RelaySourceSelection.restored(nil) == .tablet)
        #expect(RelaySourceSelection.restored("old-unknown") == .tablet)
        #expect(RelaySourceSelection.restored("phone").sources == [.phone])
        #expect(RelaySourceSelection.mixed.sources == [.tablet, .phone])
        #expect(RelaySource.tablet.account("session") != RelaySource.phone.account("session"))
    }

    @Test func sameNamedConversationsRemainSeparateAcrossSourcePairAndProfile() throws {
        let pair = try session().pairId
        let phone = RelaySource.phone.conversationID(pairID: pair, profile: 0, name: "同名:好友")
        let tablet = RelaySource.tablet.conversationID(pairID: pair, profile: 0, name: "同名:好友")
        let secondProfile = RelaySource.phone.conversationID(pairID: pair, profile: 999, name: "同名:好友")
        #expect(Set([phone, tablet, secondProfile]).count == 3)
        #expect(RelaySource.phone.owns(phone, pairID: pair))
        #expect(!RelaySource.tablet.owns(phone, pairID: pair))
        #expect(!RelaySource.phone.owns(phone, pairID: UUID().uuidString))
    }

    @Test func notificationRoutingRejectsUnknownAndAmbiguousPairs() throws {
        let phone = try session()
        let tablet = try session(pairID: "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1", key: 11)
        let sessions: [RelaySource: RelaySession] = [.phone: phone, .tablet: tablet]
        #expect(RelaySessionRouting.source(pairID: phone.pairId.uppercased(), sessions: sessions) == .phone)
        #expect(RelaySessionRouting.source(pairID: tablet.pairId, sessions: sessions) == .tablet)
        #expect(RelaySessionRouting.source(pairID: UUID().uuidString, sessions: sessions) == nil)
        #expect(RelaySessionRouting.source(pairID: phone.pairId, sessions: [.phone: phone, .tablet: phone]) == nil)
    }

    @Test func legacyMigrationCopiesOnlyToPhoneAndIsIdempotent() throws {
        let service = "relay-source-test-\(UUID().uuidString)"
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer {
            for account in ["session", "pending-pairing", "preview-enabled", "phone.session", "phone.pending-pairing", "phone.preview-enabled", "tablet.session"] {
                try? RelayKeychain.delete(account: account, service: service)
            }
            try? FileManager.default.removeItem(at: directory)
        }
        let legacy = try session()
        let pending = RelayPairing(origin: legacy.origin, pairId: legacy.pairId, pairSecret: "test-secret", expiresAt: 1_788_148_800_000, messageKey: legacy.messageKey, replyKey: legacy.replyKey)
        try RelayKeychain.save(legacy, account: "session", service: service)
        try RelayKeychain.save(pending, account: "pending-pairing", service: service)
        try RelayKeychain.save(true, account: "preview-enabled", service: service)
        let bytes = Data("opaque-encrypted-cache-fixture".utf8)
        try bytes.write(to: directory.appendingPathComponent("inbox.sealed"))
        try RelayStorageMigration.migrateLegacyPhone(service: service, directory: directory)
        try RelayStorageMigration.migrateLegacyPhone(service: service, directory: directory)
        #expect(try RelayKeychain.load(RelaySession.self, account: "phone.session", service: service) == legacy)
        #expect(try RelayKeychain.load(RelayPairing.self, account: "phone.pending-pairing", service: service) == pending)
        #expect(try RelayKeychain.load(Bool.self, account: "phone.preview-enabled", service: service) == true)
        #expect(try RelayKeychain.load(RelaySession.self, account: "tablet.session", service: service) == nil)
        #expect(try RelayKeychain.load(RelaySession.self, account: "session", service: service) == nil)
        #expect(try Data(contentsOf: directory.appendingPathComponent("inbox-phone-\(legacy.pairId).sealed")) == bytes)
        // Clearing the new phone account must not resurrect a deleted legacy pairing on the next launch.
        try RelayKeychain.delete(account: "phone.session", service: service)
        try RelayStorageMigration.migrateLegacyPhone(service: service, directory: directory)
        #expect(try RelayKeychain.load(RelaySession.self, account: "phone.session", service: service) == nil)
    }

    @Test func conflictingPhoneMigrationPreservesBothSessions() throws {
        let service = "relay-source-test-\(UUID().uuidString)"
        defer { for account in ["session", "phone.session"] { try? RelayKeychain.delete(account: account, service: service) } }
        let legacy = try session()
        let existing = try session(key: 21)
        try RelayKeychain.save(legacy, account: "session", service: service)
        try RelayKeychain.save(existing, account: "phone.session", service: service)
        #expect(throws: RelayError.invalidValue("legacy pairing conflict")) {
            try RelayStorageMigration.migrateLegacyPhone(service: service, directory: FileManager.default.temporaryDirectory)
        }
        #expect(try RelayKeychain.load(RelaySession.self, account: "session", service: service) == legacy)
        #expect(try RelayKeychain.load(RelaySession.self, account: "phone.session", service: service) == existing)
    }
}
