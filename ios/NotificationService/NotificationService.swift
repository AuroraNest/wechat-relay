import Foundation
import RelayCore
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    private var handler: ((UNNotificationContent) -> Void)?
    private var content: UNMutableNotificationContent?
    private let lock = NSLock()

    override func didReceive(_ request: UNNotificationRequest, withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        handler = contentHandler
        guard let result = request.content.mutableCopy() as? UNMutableNotificationContent else { contentHandler(request.content); handler = nil; return }
        content = result
        result.title = "工作微信"
        result.body = "收到新消息"
        result.subtitle = ""
        result.threadIdentifier = ""
        result.categoryIdentifier = ""
        defer { finish() }
        do {
            let service = "com.auroramaple.wechatrelay"
            let group = Bundle.main.object(forInfoDictionaryKey: "RelayKeychainAccessGroup") as? String
            guard let pairID = result.userInfo["pairId"] as? String else { return }
            var sessions: [RelaySource: RelaySession] = [:]
            for source in RelaySource.allCases {
                sessions[source] = try RelayKeychain.load(RelaySession.self, account: source.account("session"), service: service, accessGroup: group)
            }
            // An upgrade can receive push before its first foreground launch performs migration.
            var legacyPhone = false
            if sessions[.phone] == nil {
                sessions[.phone] = try RelayKeychain.load(RelaySession.self, account: "session", service: service, accessGroup: group)
                legacyPhone = sessions[.phone] != nil
            }
            guard let source = RelaySessionRouting.source(pairID: pairID, sessions: sessions), let session = sessions[source] else { return }
            let selection = RelaySourceSelection.restored(try RelayKeychain.load(String.self, account: "source-selection", service: service, accessGroup: group))
            guard selection.sources.contains(source) else { return }
            result.subtitle = source.label
            if result.userInfo["replyCapable"] as? Bool == true { result.categoryIdentifier = "RELAY_MESSAGE" }
            let previewAccount = source == .phone && legacyPhone ? "preview-enabled" : source.account("preview-enabled")
            guard try RelayKeychain.load(Bool.self, account: previewAccount, service: service, accessGroup: group) == true,
                  result.userInfo["previewEnabled"] as? Bool == true else { return }
            var payload = result.userInfo
            payload["receivedAt"] = payload["createdAt"]
            payload["assets"] = []
            let message = try JSONDecoder().decode(RelayMessage.self, from: JSONSerialization.data(withJSONObject: payload))
            let preview = try RelayCrypto.decryptPreview(message, messageKey: session.messageKey)
            result.title = preview.sender
            result.body = preview.body
            result.threadIdentifier = source.conversationID(pairID: session.pairId, profile: message.wechatUserId, name: preview.sender)
        } catch {
            // Authentication failures and unavailable Keychain data leave a content-free notification.
        }
    }

    override func serviceExtensionTimeWillExpire() { finish() }

    private func finish() {
        lock.lock()
        guard let handler, let content else { lock.unlock(); return }
        self.handler = nil
        lock.unlock()
        handler(content)
    }
}
