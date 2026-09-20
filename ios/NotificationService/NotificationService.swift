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
        result.categoryIdentifier = ""
        defer { finish() }
        do {
            let service = "com.auroramaple.wechatrelay"
            let group = Bundle.main.object(forInfoDictionaryKey: "RelayKeychainAccessGroup") as? String
            guard let session = try RelayKeychain.load(RelaySession.self, account: "session", service: service, accessGroup: group),
                  let pairID = result.userInfo["pairId"] as? String, pairID == session.pairId else { return }
            if result.userInfo["replyCapable"] as? Bool == true { result.categoryIdentifier = "RELAY_MESSAGE" }
            guard try RelayKeychain.load(Bool.self, account: "preview-enabled", service: service, accessGroup: group) == true,
                  result.userInfo["previewEnabled"] as? Bool == true else { return }
            var payload = result.userInfo
            payload["receivedAt"] = payload["createdAt"]
            payload["assets"] = []
            let message = try JSONDecoder().decode(RelayMessage.self, from: JSONSerialization.data(withJSONObject: payload))
            let preview = try RelayCrypto.decryptPreview(message, messageKey: session.messageKey)
            result.title = preview.sender
            result.body = preview.body
            result.threadIdentifier = "\(message.wechatUserId):\(preview.sender)"
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
