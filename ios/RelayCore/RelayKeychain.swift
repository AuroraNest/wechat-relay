import Foundation
import Security

public enum RelayKeychain {
    public static func save<T: Encodable>(_ value: T, account: String, service: String, accessGroup: String? = nil) throws {
        let data = try JSONEncoder().encode(value)
        var query = baseQuery(account: account, service: service, accessGroup: accessGroup)
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let addStatus = SecItemAdd(query as CFDictionary, nil)
        if addStatus == errSecDuplicateItem {
            let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
            let updateStatus = SecItemUpdate(baseQuery(account: account, service: service, accessGroup: accessGroup) as CFDictionary, attributes as CFDictionary)
            guard updateStatus == errSecSuccess else { throw keychainError(updateStatus) }
        } else if addStatus != errSecSuccess {
            throw keychainError(addStatus)
        }
    }

    public static func load<T: Decodable>(_ type: T.Type, account: String, service: String, accessGroup: String? = nil) throws -> T? {
        var query = baseQuery(account: account, service: service, accessGroup: accessGroup)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else { throw keychainError(status) }
        return try JSONDecoder().decode(type, from: data)
    }

    public static func delete(account: String, service: String, accessGroup: String? = nil) throws {
        let status = SecItemDelete(baseQuery(account: account, service: service, accessGroup: accessGroup) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw keychainError(status) }
    }

    private static func baseQuery(account: String, service: String, accessGroup: String?) -> [String: Any] {
        var query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account, kSecAttrService as String: service]
        if let accessGroup, !accessGroup.isEmpty { query[kSecAttrAccessGroup as String] = accessGroup }
        return query
    }

    private static func keychainError(_ status: OSStatus) -> RelayError { .invalidValue("Keychain error \(status)") }
}

public enum RelayStorageMigration {
    public static func migrateLegacyPhone(service: String, accessGroup: String? = nil, directory: URL) throws {
        guard let legacy = try RelayKeychain.load(RelaySession.self, account: "session", service: service, accessGroup: accessGroup) else { return }
        let account = RelaySource.phone.account("session")
        let existing = try RelayKeychain.load(RelaySession.self, account: account, service: service, accessGroup: accessGroup)
        guard existing == nil || existing == legacy else { throw RelayError.invalidValue("legacy pairing conflict") }
        let old = directory.appendingPathComponent("inbox.sealed")
        let new = directory.appendingPathComponent("inbox-phone-\(legacy.pairId).sealed")
        // An interrupted migration or an older app can leave a different cache under the same pair.
        if FileManager.default.fileExists(atPath: old.path), FileManager.default.fileExists(atPath: new.path),
           !FileManager.default.contentsEqual(atPath: old.path, andPath: new.path) {
            throw RelayError.invalidValue("legacy cache conflict")
        }
        try RelayKeychain.save(legacy, account: account, service: service, accessGroup: accessGroup)
        if let pending = try RelayKeychain.load(RelayPairing.self, account: "pending-pairing", service: service, accessGroup: accessGroup) {
            guard pending.pairId == legacy.pairId else { throw RelayError.invalidValue("legacy pairing conflict") }
            try RelayKeychain.save(pending, account: RelaySource.phone.account("pending-pairing"), service: service, accessGroup: accessGroup)
        }
        let previewAccount = RelaySource.phone.account("preview-enabled")
        if let preview = try RelayKeychain.load(Bool.self, account: "preview-enabled", service: service, accessGroup: accessGroup),
           try RelayKeychain.load(Bool.self, account: previewAccount, service: service, accessGroup: accessGroup) == nil {
            try RelayKeychain.save(preview, account: previewAccount, service: service, accessGroup: accessGroup)
        }
        if FileManager.default.fileExists(atPath: old.path), !FileManager.default.fileExists(atPath: new.path) {
            try FileManager.default.copyItem(at: old, to: new)
        }
        // Copies precede deletion. A failed attempt preserves the legacy keys for a safe retry.
        try RelayKeychain.delete(account: "pending-pairing", service: service, accessGroup: accessGroup)
        try RelayKeychain.delete(account: "session", service: service, accessGroup: accessGroup)
        if FileManager.default.fileExists(atPath: old.path) { try FileManager.default.removeItem(at: old) }
    }
}
