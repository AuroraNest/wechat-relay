import CryptoKit
import Foundation
import ImageIO
import RelayCore
import SwiftUI
import UserNotifications

struct InboxItem: Identifiable {
    let message: RelayMessage
    let preview: RelayPreview
    var id: UUID { message.id }
    var conversationID: String { "\(message.wechatUserId):\(preview.sender)" }
}

struct Conversation: Identifiable {
    let id: String
    let latest: InboxItem
    let unread: Int
}

struct OutgoingMessage: Codable, Identifiable {
    let request: RelayReplyRequest
    let conversationID: String
    let body: String
    var status: RelayReplyStatus
    var id: UUID { request.id }
}

private struct InboxSnapshot: Codable {
    var messages: [RelayMessage] = []
    var outgoing: [OutgoingMessage] = []
    var readThrough: [String: Int] = [:]
    var clearedThrough: Int = 0
}

@MainActor
final class RelayAppModel: ObservableObject {
    static let shared = RelayAppModel()
    static let keychainService = "com.auroramaple.wechatrelay"
    static var keychainGroup: String? { Bundle.main.object(forInfoDictionaryKey: "RelayKeychainAccessGroup") as? String }

    @Published private(set) var session: RelaySession?
    @Published private(set) var items: [InboxItem] = []
    @Published private(set) var outgoing: [OutgoingMessage] = []
    @Published private(set) var device: RelayDeviceStatus?
    @Published private(set) var policy: RelayPolicy = .default
    @Published private(set) var pairing: RelayPairing?
    @Published private(set) var previewEnabled = false
    @Published private(set) var notificationPermission: UNAuthorizationStatus = .notDetermined
    @Published private(set) var lastSync: Date?
    @Published private(set) var isRefreshing = false
    @Published private(set) var isSavingPolicy = false
    @Published private(set) var hasMore = false
    @Published var problem: String?
    @Published var selectedTab = 0
    @Published var conversationPath: [String] = []
    @Published private(set) var isDemo: Bool

    private var snapshot = InboxSnapshot()
    private var loop: Task<Void, Never>?
    private var apnsToken: String?
    private var pushDirty = true
    private var nextCursor: Int?
    private var cacheReadable = true
    private var lastStatusCheck = Date.distantPast
    private var submitting = Set<UUID>()
    private var imageCache = NSCache<NSString, UIImage>()

    private init() {
        #if DEBUG
        isDemo = ProcessInfo.processInfo.arguments.contains("--demo")
        #else
        isDemo = false
        #endif
        imageCache.totalCostLimit = 24 * 1_024 * 1_024
        if isDemo {
            loadDemo()
            return
        }
        do {
            session = try loadSecret(RelaySession.self, account: "session")
            pairing = try loadSecret(RelayPairing.self, account: "pending-pairing")
            previewEnabled = try loadSecret(Bool.self, account: "preview-enabled") ?? false
            apnsToken = try loadSecret(String.self, account: "apns-token")
            try restoreCache()
        } catch {
            cacheReadable = false
            problem = "无法读取本机安全存储. 请先解锁设备再重新打开 App. 已保存的数据没有被覆盖."
        }
    }

    var conversations: [Conversation] {
        Dictionary(grouping: items, by: \.conversationID).compactMap { id, values in
            guard let latest = values.max(by: { $0.message.seq < $1.message.seq }) else { return nil }
            return Conversation(id: id, latest: latest, unread: values.filter { $0.message.seq > (snapshot.readThrough[id] ?? 0) }.count)
        }.sorted { $0.latest.message.seq > $1.latest.message.seq }
    }

    var stateTitle: String {
        if isDemo { return "演示模式 · 虚构数据" }
        if problem != nil { return "连接需要检查" }
        if device?.paired != true { return "等待 Android 配对" }
        return policy.active ? "转发中" : "已暂停转发"
    }

    func start() {
        guard !isDemo, loop == nil else { return }
        loop = Task { [weak self] in
            await self?.refreshNotificationPermission()
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                // ponytail: foreground polling keeps reply status and device state together; use SSE if this single-client load becomes material.
                try? await Task.sleep(for: .seconds(self.problem == nil ? 4 : 15))
            }
        }
    }

    func stop() { loop?.cancel(); loop = nil }

    #if DEBUG
    func enterDemo() {
        guard session == nil else { return }
        stop()
        isDemo = true
        problem = nil
        loadDemo()
    }
    #endif

    func connect(origin: String, token: String) async throws {
        guard !isDemo, session == nil, cacheReadable else { throw RelayError.invalidValue("existing connection") }
        guard let url = URL(string: origin.trimmingCharacters(in: .whitespacesAndNewlines)) else { throw RelayError.invalidOrigin }
        let api = try RelayAPI(origin: url)
        let ack = try await api.createSession(token: token.trimmingCharacters(in: .whitespacesAndNewlines))
        let newPairing = try await api.createPairing(token: token.trimmingCharacters(in: .whitespacesAndNewlines), ackToken: ack)
        let newSession = try newPairing.makeSession(ackToken: ack)
        try saveSecret(newPairing, account: "pending-pairing")
        try saveSecret(newSession, account: "session")
        session = newSession
        pairing = newPairing
        problem = nil
        snapshot = InboxSnapshot()
        pushDirty = true
        await refresh()
    }

    func refresh() async {
        guard !isDemo, let session, !isRefreshing, cacheReadable else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let api = try RelayAPI(origin: session.origin)
            if device == nil || Date().timeIntervalSince(lastStatusCheck) > 25 {
                let current = try await api.deviceStatus(session: session)
                guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
                device = current
                lastStatusCheck = Date()
            }
            guard device?.paired == true else { problem = nil; return }
            if pairing != nil {
                try deleteSecret(account: "pending-pairing")
                pairing = nil
            }
            try await syncMessages(api: api, session: session)
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            try await refreshReplies(api: api, session: session)
            if policy.updatedAt == 0 || Date().timeIntervalSince(lastStatusCheck) < 2 {
                policy = try await api.policy(session: session)
            }
            if pushDirty {
                try await api.updatePush(session: session, token: apnsToken, environment: pushEnvironment, previewEnabled: previewEnabled)
                pushDirty = false
                device = try await api.deviceStatus(session: session)
            }
            lastSync = Date()
            problem = nil
        } catch is CancellationError {
        } catch {
            if !Task.isCancelled { problem = Self.describe(error) }
        }
    }

    private func syncMessages(api: RelayAPI, session: RelaySession) async throws {
        let known = max(snapshot.clearedThrough, snapshot.messages.map(\.seq).max() ?? 0)
        var cursor: Int?
        var collected: [RelayMessage] = []
        repeat {
            let page = try await api.messages(session: session, beforeSeq: cursor)
            collected += page.messages.filter { $0.seq > known }
            let next = page.nextCursor.flatMap(Int.init)
            if snapshot.messages.isEmpty && cursor == nil { nextCursor = next; hasMore = page.hasMore && snapshot.clearedThrough == 0 }
            if !page.hasMore || known == 0 || page.messages.contains(where: { $0.seq <= known }) { break }
            guard let next, next > 0, cursor == nil || next < cursor! else { throw RelayError.invalidResponse }
            cursor = next
            try Task.checkCancellation()
        } while true
        guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
        if !collected.isEmpty { try merge(collected); try saveCache() }
    }

    func loadOlder() async {
        guard !isDemo, let session, hasMore, let nextCursor else { return }
        do {
            let page = try await RelayAPI(origin: session.origin).messages(session: session, beforeSeq: nextCursor)
            try merge(page.messages.filter { $0.seq > snapshot.clearedThrough })
            self.nextCursor = page.nextCursor.flatMap(Int.init)
            hasMore = page.hasMore && self.nextCursor != nil
            try saveCache()
        } catch { problem = Self.describe(error) }
    }

    private func merge(_ messages: [RelayMessage]) throws {
        guard let session else { return }
        var unique = Dictionary(uniqueKeysWithValues: snapshot.messages.map { ($0.id, $0) })
        for message in messages {
            _ = try RelayCrypto.decryptPreview(message, messageKey: session.messageKey)
            unique[message.id] = message
        }
        snapshot.messages = unique.values.sorted { $0.seq < $1.seq }
        items = try snapshot.messages.map { InboxItem(message: $0, preview: try RelayCrypto.decryptPreview($0, messageKey: session.messageKey)) }
    }

    func markRead(_ conversationID: String) {
        snapshot.readThrough[conversationID] = items.filter { $0.conversationID == conversationID }.map { $0.message.seq }.max() ?? 0
        objectWillChange.send()
        do { try saveCache() } catch { problem = Self.describe(error) }
    }

    func send(_ body: String, to target: InboxItem) async throws {
        guard let session else { throw RelayError.invalidValue("session") }
        let text = body.trimmingCharacters(in: .whitespacesAndNewlines)
        let request = try RelayCrypto.makeReply(session: session, target: target.message, body: text)
        let pending = OutgoingMessage(request: request, conversationID: target.conversationID, body: text, status: .submitting)
        snapshot.outgoing.append(pending)
        do { try saveCache() } catch { snapshot.outgoing.removeAll { $0.id == pending.id }; throw error }
        outgoing = snapshot.outgoing
        if isDemo { updateReply(request.id, status: .sentToWechat); return }
        // Persist the immutable request before submitting; retries reuse its ID, time and ciphertext.
        await submit(pending)
    }

    func retry(_ reply: OutgoingMessage) async {
        guard reply.status == .submitting else { return }
        await submit(reply)
    }

    private func submit(_ reply: OutgoingMessage) async {
        guard let session, !submitting.contains(reply.id) else { return }
        submitting.insert(reply.id)
        defer { submitting.remove(reply.id) }
        do {
            let result = try await RelayAPI(origin: session.origin).submitReply(session: session, request: reply.request)
            guard result.replyId == reply.id else { throw RelayError.invalidResponse }
            updateReply(reply.id, status: result.status)
            try saveCache()
        } catch {
            problem = "回复尚未确认提交. 可以点重试, 同一条回复不会重复创建. \(Self.describe(error))"
        }
    }

    private func refreshReplies(api: RelayAPI, session: RelaySession) async throws {
        for reply in outgoing where !reply.status.isTerminal && !submitting.contains(reply.id) {
            do {
                let result = try await api.replyStatus(session: session, id: reply.id)
                guard result.replyId == reply.id else { throw RelayError.invalidResponse }
                updateReply(reply.id, status: result.status)
            } catch RelayError.httpStatus(404) where reply.status == .submitting {
                // A failed request may never have reached the server. Only an explicit retry submits it again.
            }
        }
        if !outgoing.isEmpty { try saveCache() }
    }

    private func updateReply(_ id: UUID, status: RelayReplyStatus) {
        guard let index = snapshot.outgoing.firstIndex(where: { $0.id == id }) else { return }
        snapshot.outgoing[index].status = status
        outgoing = snapshot.outgoing
    }

    func savePolicy(_ value: RelayPolicy) async throws {
        guard let session else { return }
        isSavingPolicy = true
        defer { isSavingPolicy = false }
        if isDemo { policy = value; return }
        policy = try await RelayAPI(origin: session.origin).savePolicy(session: session, policy: value)
    }

    func setPreviewEnabled(_ enabled: Bool) async throws {
        if !isDemo { try saveSecret(enabled, account: "preview-enabled") }
        previewEnabled = enabled
        pushDirty = true
        await refresh()
    }

    func refreshNotificationPermission() async {
        notificationPermission = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        if notificationPermission == .authorized || notificationPermission == .provisional {
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    func requestNotifications() async {
        guard !isDemo else { return }
        do {
            _ = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound])
            await refreshNotificationPermission()
        } catch { problem = Self.describe(error) }
    }

    func registerPushToken(_ token: String) async {
        guard !isDemo else { return }
        do {
            try saveSecret(token, account: "apns-token")
            apnsToken = token
            pushDirty = true
            await refresh()
        } catch { problem = Self.describe(error) }
    }

    func openNotification(_ userInfo: [AnyHashable: Any], reply: String? = nil) async throws {
        guard let pairID = userInfo["pairId"] as? String, pairID == session?.pairId,
              let rawID = userInfo["messageId"] as? String, let id = UUID(uuidString: rawID) else { throw RelayError.invalidResponse }
        // A banner can be tapped while the foreground poll is already fetching the same message.
        if isRefreshing {
            for _ in 0..<100 {
                if !isRefreshing { break }
                try await Task.sleep(for: .milliseconds(100))
            }
        }
        await refresh()
        while !items.contains(where: { $0.id == id }), hasMore {
            let previous = nextCursor
            await loadOlder()
            if previous == nextCursor { break }
        }
        guard let item = items.first(where: { $0.id == id }) else { throw RelayError.invalidValue("message no longer available") }
        if let reply { try await send(reply, to: item) }
        else { selectedTab = 0; conversationPath = [item.conversationID] }
    }

    func image(for metadata: RelayAssetMetadata, in message: RelayMessage) async throws -> UIImage {
        let key = metadata.id.uuidString as NSString
        if let image = imageCache.object(forKey: key) { return image }
        guard let session else { throw RelayError.invalidValue("session") }
        let asset = try await RelayAPI(origin: session.origin).asset(session: session, id: metadata.id)
        let data = try RelayCrypto.decryptAsset(asset, for: message, messageKey: session.messageKey)
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 2_048, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else { throw RelayError.invalidResponse }
        let image = UIImage(cgImage: thumbnail)
        imageCache.setObject(image, forKey: key, cost: thumbnail.bytesPerRow * thumbnail.height)
        return image
    }

    func clearHistory() throws {
        let cutoff = max(snapshot.clearedThrough, snapshot.messages.map(\.seq).max() ?? 0)
        let previous = snapshot
        let wasReadable = cacheReadable
        snapshot = InboxSnapshot(clearedThrough: cutoff)
        cacheReadable = true
        do { try saveCache() } catch { snapshot = previous; cacheReadable = wasReadable; throw error }
        items = []; outgoing = []; imageCache.removeAllObjects(); hasMore = false
        problem = nil
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
    }

    func disconnect() throws {
        stop()
        if !isDemo {
            try deleteSecret(account: "session")
            try deleteSecret(account: "pending-pairing")
            if let file = try? cacheURL(), FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
        }
        session = nil; pairing = nil; device = nil
        isDemo = false
        snapshot = InboxSnapshot(); items = []; outgoing = []; conversationPath = []
        imageCache.removeAllObjects(); problem = nil; cacheReadable = true
        lastStatusCheck = .distantPast
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
        start()
    }

    func disablePushAndDisconnect() async throws {
        if !isDemo, let session {
            try await RelayAPI(origin: session.origin).updatePush(session: session, token: nil, environment: pushEnvironment, previewEnabled: false)
        }
        try disconnect()
    }

    private var pushEnvironment: RelayPushEnvironment {
        RelayPushEnvironment(rawValue: Bundle.main.object(forInfoDictionaryKey: "RelayAPNSEnvironment") as? String ?? "sandbox") ?? .sandbox
    }

    private func loadSecret<T: Decodable>(_ type: T.Type, account: String) throws -> T? {
        try RelayKeychain.load(type, account: account, service: Self.keychainService, accessGroup: Self.keychainGroup)
    }
    private func saveSecret<T: Encodable>(_ value: T, account: String) throws {
        try RelayKeychain.save(value, account: account, service: Self.keychainService, accessGroup: Self.keychainGroup)
    }
    private func deleteSecret(account: String) throws {
        try RelayKeychain.delete(account: account, service: Self.keychainService, accessGroup: Self.keychainGroup)
    }

    private func cacheURL() throws -> URL {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        return directory.appendingPathComponent("inbox.sealed")
    }

    private func restoreCache() throws {
        guard let session else { return }
        let url = try cacheURL()
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        let box = try AES.GCM.SealedBox(combined: Data(contentsOf: url))
        let data = try AES.GCM.open(box, using: SymmetricKey(data: session.messageKey), authenticating: Data("AWR1|IOS_CACHE|\(session.pairId)".utf8))
        snapshot = try JSONDecoder().decode(InboxSnapshot.self, from: data)
        try merge([])
        outgoing = snapshot.outgoing
        nextCursor = snapshot.messages.map(\.seq).min()
        hasMore = nextCursor != nil && snapshot.clearedThrough == 0
    }

    private func saveCache() throws {
        guard !isDemo else { return }
        guard cacheReadable, let session else { throw RelayError.invalidValue("local storage unavailable") }
        let data = try JSONEncoder().encode(snapshot)
        let box = try AES.GCM.seal(data, using: SymmetricKey(data: session.messageKey), authenticating: Data("AWR1|IOS_CACHE|\(session.pairId)".utf8))
        guard let combined = box.combined else { throw RelayError.cryptographyFailed }
        var url = try cacheURL()
        try combined.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try url.setResourceValues(values)
    }

    static func describe(_ error: Error) -> String {
        switch error {
        case RelayError.invalidOrigin: return "请填写完整的 HTTPS 服务地址, 不带路径或参数."
        case RelayError.httpStatus(401), RelayError.httpStatus(403): return "连接凭证已失效或没有权限. 请检查服务配置与配对状态."
        case RelayError.httpStatus(404): return "当前服务尚未提供此接口, 或消息已过期."
        case RelayError.invalidEnvelope, RelayError.cryptographyFailed: return "消息校验失败, 已阻止显示. 请检查配对是否匹配."
        case let error as URLError where error.code == .notConnectedToInternet: return "网络未连接. 恢复网络后会自动同步."
        case let error as URLError where error.code == .timedOut: return "连接超时. 请检查服务和网络."
        default: return "操作未完成. \(error.localizedDescription)"
        }
    }

    private func loadDemo() {
        do {
            session = try RelaySession(origin: URL(string: "https://example.invalid")!, ackToken: "demo", pairId: UUID().uuidString, messageKey: Data(repeating: 7, count: 32), replyKey: Data(repeating: 9, count: 32))
            guard let session else { return }
            let rows = [("林一", "收到, 我下午确认后回复你.", 0), ("产品讨论组", "新版的页面已经整理好了, 大家看一下.", 0), ("小周", "[语音转文字] 明天十点见, 还是老地方.", 999), ("设计协作", "这版留白很舒服, 可以继续往下做了.", 0), ("陈默", "[聊天记录] 这里有 3 条合并转发的消息.", 0), ("文件传输助手", "下午的会议资料已收到.", 0)]
            for (index, row) in rows.enumerated().reversed() {
                let date = Date().addingTimeInterval(-Double(index * 900))
                let id = try UUIDv7.make(now: date)
                let timestamp = Int64(date.timeIntervalSince1970 * 1_000)
                let seq = rows.count - index
                let deviceID = "demo_android_0001"
                let preview = RelayPreview(sender: row.0, body: row.1)
                let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(preview), key: session.messageKey, kid: "phase1", aad: "AWR1|A2I|\(id.uuidString.lowercased())|\(deviceID)|\(seq)|\(timestamp)|\(row.2)")
                snapshot.messages.append(try RelayMessage(messageId: id, deviceId: deviceID, seq: seq, createdAt: timestamp, wechatUserId: row.2, replyCapable: true, conversationSendCapable: true, previewEnvelope: envelope, assets: [], receivedAt: timestamp))
            }
            try merge([])
            device = RelayDeviceStatus(paired: true, deviceId: "demo_android_0001", lastSeenAt: Int64(Date().timeIntervalSince1970 * 1_000), serverTime: Int64(Date().timeIntervalSince1970 * 1_000), pushConfigured: true, pushRegistered: true)
            lastSync = Date()
        } catch { problem = Self.describe(error) }
    }
}

extension RelayReplyStatus {
    var label: String {
        switch self {
        case .submitting: return "提交尚未确认 · 点按重试"
        case .queued: return "等待 Android 取走"
        case .deliveredToAndroid: return "Android 正在处理"
        case .sentToWechat: return "已发往微信"
        case .notificationNotActive: return "原通知已失效, 请等待对方的新消息"
        case .wechatActionChanged: return "微信回复入口已变化"
        case .remoteInputUnsupported: return "此消息不支持通知回复"
        case .pendingIntentCanceled: return "微信回复入口已失效"
        case .invalidReply: return "回复内容未通过校验"
        case .failed: return "Android 发送失败"
        }
    }
}
