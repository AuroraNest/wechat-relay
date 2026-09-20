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
