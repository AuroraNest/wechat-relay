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
    let friend: RelayFriend
    let body: String
    let createdAt: Int64
    let unread: Int
}

struct RelayFriend: Identifiable, Hashable {
    let name: String
    let wechatUserId: Int
    let capturedAt: Int64

    var id: String { "\(wechatUserId):\(name)" }
    var profileLabel: String { wechatUserId == 999 ? "微信 2" : "微信" }
}

struct OutgoingMessage: Codable, Identifiable {
    let request: RelayReplyRequest
    let conversationID: String
    let body: String
    var status: RelayReplyStatus
    var id: UUID { request.id }
}

private struct CachedContacts: Codable {
    var version: Int? = nil
    let id: UUID
    let deviceId: String
    let wechatUserId: Int
    let capturedAt: Int64
    let contacts: [RelayContact]
}

private struct InboxSnapshot: Codable {
    var messages: [RelayMessage] = []
    var outgoing: [OutgoingMessage] = []
    var readThrough: [String: Int] = [:]
    var clearedThrough: Int = 0
    var contacts: [CachedContacts] = []

    enum CodingKeys: String, CodingKey { case messages, outgoing, readThrough, clearedThrough, contacts }

    init(messages: [RelayMessage] = [], outgoing: [OutgoingMessage] = [], readThrough: [String: Int] = [:], clearedThrough: Int = 0, contacts: [CachedContacts] = []) {
        self.messages = messages
        self.outgoing = outgoing
        self.readThrough = readThrough
        self.clearedThrough = clearedThrough
        self.contacts = contacts
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        messages = try values.decode([RelayMessage].self, forKey: .messages)
        outgoing = try values.decode([OutgoingMessage].self, forKey: .outgoing)
        readThrough = try values.decode([String: Int].self, forKey: .readThrough)
        clearedThrough = try values.decode(Int.self, forKey: .clearedThrough)
        // Cache versions before Friends did not have this key.
        contacts = try values.decodeIfPresent([CachedContacts].self, forKey: .contacts) ?? []
    }
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
    @Published private(set) var friends: [RelayFriend] = []
    @Published private(set) var contactsProblem: String?
    @Published private(set) var contactsAvailable: Bool?
    @Published private(set) var isRefreshingContacts = false
    @Published var problem: String?
    @Published var selectedTab = 0
    @Published var conversationPath: [String] = []
    @Published private(set) var isDemo: Bool

    private var snapshot = InboxSnapshot()
    private var loop: Task<Void, Never>?
    private var stream: Task<Void, Never>?
    private var foregroundRun: UUID?
    private var streamConnected = false
    private var refreshRequested = false
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
        let incoming = Dictionary(grouping: items, by: \.conversationID)
        let sent = Dictionary(grouping: outgoing, by: \.conversationID)
        return Set(incoming.keys).union(sent.keys).compactMap { id in
            let values = incoming[id] ?? []
            let latest = values.max(by: { $0.message.seq < $1.message.seq })
            let reply = sent[id]?.max(by: { $0.request.createdAt < $1.request.createdAt })
            guard let profile = latest?.message.wechatUserId ?? reply?.request.wechatUserId else { return nil }
            let title = latest?.preview.sender ?? String(id.dropFirst("\(profile):".count))
            let useReply = (reply?.request.createdAt ?? 0) >= (latest?.message.createdAt ?? 0)
            let time = useReply ? reply!.request.createdAt : latest!.message.createdAt
            return Conversation(id: id, friend: RelayFriend(name: title, wechatUserId: profile, capturedAt: time), body: useReply ? reply!.body : latest!.preview.body, createdAt: time, unread: values.filter { $0.message.seq > (snapshot.readThrough[id] ?? 0) }.count)
        }.sorted { $0.createdAt > $1.createdAt }
    }

    var friendsCapturedAt: Date? {
        snapshot.contacts.map(\.capturedAt).max().map { Date(timeIntervalSince1970: Double($0) / 1_000) }
    }

    var stateTitle: String {
        if isDemo { return "演示模式 · 虚构数据" }
        if problem != nil { return "连接需要检查" }
        if device?.paired != true { return "等待 Android 配对" }
        return policy.active ? "转发中" : "已暂停转发"
    }

    func start() {
        guard !isDemo, loop == nil else { return }
        let run = UUID()
        foregroundRun = run
        loop = Task { [weak self] in
            await self?.refreshNotificationPermission()
            while !Task.isCancelled {
                guard let self, self.foregroundRun == run else { return }
                // Keep a low-frequency reconciliation while connected, and poll during stream outages.
                if !self.streamConnected || self.problem != nil || Date().timeIntervalSince(self.lastSync ?? .distantPast) >= 30 {
                    await self.refresh()
                }
                guard !Task.isCancelled, self.foregroundRun == run else { return }
                self.startEventStream(run: run)
                try? await Task.sleep(for: .seconds(self.problem == nil ? 4 : 15))
            }
        }
    }

    func stop() {
        foregroundRun = nil
        loop?.cancel(); loop = nil
        stream?.cancel(); stream = nil
        streamConnected = false
        refreshRequested = false
    }

    private func startEventStream(run: UUID) {
        guard stream == nil, let session, device?.paired == true, cacheReadable else { return }
        stream = Task { [weak self] in
            var delay = 1
            while !Task.isCancelled {
                guard let self, self.foregroundRun == run, self.session?.pairId == session.pairId else { return }
                do {
                    let api = try RelayAPI(origin: session.origin)
                    let cursor = max(self.snapshot.clearedThrough, self.snapshot.messages.map(\.seq).max() ?? 0)
                    try await api.streamEvents(session: session, afterSeq: cursor) { [weak self] event in
                        await self?.receiveStreamEvent(event, pairID: session.pairId, run: run)
                    }
                } catch {
                    guard !Task.isCancelled, self.foregroundRun == run else { return }
                    if self.streamConnected { delay = 1 }
                    self.streamConnected = false
                    // A stream failure does not imply message sync failed; HTTP polling remains available.
                }
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, 30)
            }
        }
    }

    private func receiveStreamEvent(_ event: RelayStreamEvent, pairID: String, run: UUID) async {
        guard !Task.isCancelled, foregroundRun == run, session?.pairId == pairID else { return }
        if case .ready = event { streamConnected = true }
        // The ready event follows subscription, so this also recovers reply changes during disconnection.
        await refresh()
    }

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
        friends = []
        contactsAvailable = nil
        contactsProblem = nil
        pushDirty = true
        await refresh()
    }

    func refresh() async {
        guard !isDemo, let session, cacheReadable, !Task.isCancelled else { return }
        guard !isRefreshing else { refreshRequested = true; return }
        isRefreshing = true
        defer {
            isRefreshing = false
            if refreshRequested {
                refreshRequested = false
                Task { [weak self] in await self?.refresh() }
            }
        }
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
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            if policy.updatedAt == 0 || Date().timeIntervalSince(lastStatusCheck) < 2 {
                let currentPolicy = try await api.policy(session: session)
                guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
                policy = currentPolicy
            }
            if pushDirty {
                try await api.updatePush(session: session, token: apnsToken, environment: pushEnvironment, previewEnabled: previewEnabled)
                guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
                pushDirty = false
                let currentDevice = try await api.deviceStatus(session: session)
                guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
                device = currentDevice
            }
            lastSync = Date()
            problem = nil
        } catch is CancellationError {
        } catch {
            if self.session?.pairId == session.pairId, !Task.isCancelled { problem = Self.describe(error) }
        }
    }

    private func syncMessages(api: RelayAPI, session: RelaySession) async throws {
        let known = max(snapshot.clearedThrough, snapshot.messages.map(\.seq).max() ?? 0)
        var cursor: Int?
        var collected: [RelayMessage] = []
        repeat {
            let page = try await api.messages(session: session, beforeSeq: cursor)
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
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
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            try merge(page.messages.filter { $0.seq > snapshot.clearedThrough })
            self.nextCursor = page.nextCursor.flatMap(Int.init)
            hasMore = page.hasMore && self.nextCursor != nil
            try saveCache()
        } catch {
            if self.session?.pairId == session.pairId, !Task.isCancelled { problem = Self.describe(error) }
        }
    }

    func refreshContacts() async {
        guard !isDemo, let session, !isRefreshingContacts, cacheReadable else { return }
        isRefreshingContacts = true
        defer { isRefreshingContacts = false }
        do {
            let api = try RelayAPI(origin: session.origin)
            let currentDevice = try await api.deviceStatus(session: session)
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            device = currentDevice
            lastStatusCheck = Date()
            guard currentDevice.paired, let currentDeviceID = currentDevice.deviceId,
                  currentDeviceID.range(of: "^[A-Za-z0-9_-]{16,128}$", options: .regularExpression) != nil else {
                throw RelayError.invalidResponse
            }
            let snapshots = try await api.contacts(session: session)
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            try mergeContacts(snapshots, currentDeviceID: currentDeviceID, session: session)
            contactsAvailable = true
            contactsProblem = nil
        } catch is CancellationError {
        } catch RelayError.httpStatus(404) {
            // Older Relay servers do not expose contacts. Keep message synchronization independent.
            contactsAvailable = false
            contactsProblem = nil
        } catch {
            if !Task.isCancelled { contactsProblem = Self.describe(error) }
        }
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

    private func mergeContacts(_ incoming: [RelayContactSnapshot], currentDeviceID: String, session: RelaySession) throws {
        guard incoming.count <= 2, Set(incoming.map(\.wechatUserId)).count == incoming.count else {
            throw RelayError.invalidResponse
        }
        let decoded = try incoming.map { snapshot -> (RelayContactSnapshot, RelayContactsPayload) in
            guard snapshot.deviceId == currentDeviceID else { throw RelayError.invalidResponse }
            return (snapshot, try RelayCrypto.decryptContacts(snapshot, messageKey: session.messageKey))
        }
        var cached: [Int: CachedContacts] = [:]
        for local in snapshot.contacts where cached[local.wechatUserId]?.capturedAt ?? .min < local.capturedAt {
            cached[local.wechatUserId] = local
        }
        var changed = false
        for (remote, payload) in decoded {
            if let local = cached[remote.wechatUserId] {
                if remote.capturedAt < local.capturedAt { continue }
                if remote.capturedAt == local.capturedAt {
                    guard remote.id == local.id, remote.deviceId == local.deviceId else { throw RelayError.invalidResponse }
                    continue
                }
            }
            cached[remote.wechatUserId] = CachedContacts(version: remote.v, id: remote.id, deviceId: remote.deviceId, wechatUserId: remote.wechatUserId, capturedAt: remote.capturedAt, contacts: payload.contacts)
            changed = true
        }
        guard changed else { return }
        let previousContacts = snapshot.contacts
        snapshot.contacts = cached.values.sorted { $0.wechatUserId < $1.wechatUserId }
        do {
            try saveCache()
        } catch {
            snapshot.contacts = previousContacts
            throw error
        }
        refreshFriends()
    }

    private func refreshFriends() {
        friends = snapshot.contacts.flatMap { snapshot in
            snapshot.contacts.map { RelayFriend(name: $0.name, wechatUserId: snapshot.wechatUserId, capturedAt: snapshot.capturedAt) }
        }.sorted {
            let comparison = $0.name.localizedStandardCompare($1.name)
            if comparison == .orderedSame { return $0.wechatUserId < $1.wechatUserId }
            return comparison == .orderedAscending
        }
    }

    func canSend(to friend: RelayFriend) -> Bool {
        snapshot.contacts.contains { $0.version == 2 && $0.deviceId == device?.deviceId && $0.wechatUserId == friend.wechatUserId && $0.contacts.contains(where: { $0.name == friend.name }) }
    }

    func avatarItem(for friend: RelayFriend) -> InboxItem? {
        items.last(where: { $0.message.wechatUserId == friend.wechatUserId && $0.preview.sender == friend.name && $0.message.assets.contains(where: { $0.kind == .avatar }) })
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
        try await enqueue(pending)
    }

    func send(_ body: String, to friend: RelayFriend) async throws {
        guard let session, canSend(to: friend),
              let contacts = snapshot.contacts.first(where: { $0.wechatUserId == friend.wechatUserId }) else {
            throw RelayError.invalidValue("contact snapshot")
        }
        let text = body.trimmingCharacters(in: .whitespacesAndNewlines)
        let request = try RelayCrypto.makeContactSend(session: session, snapshotId: contacts.id, deviceId: contacts.deviceId, wechatUserId: friend.wechatUserId, conversationTitle: friend.name, body: text)
        try await enqueue(OutgoingMessage(request: request, conversationID: friend.id, body: text, status: .submitting))
    }

    private func enqueue(_ pending: OutgoingMessage) async throws {
        snapshot.outgoing.append(pending)
        do { try saveCache() } catch { snapshot.outgoing.removeAll { $0.id == pending.id }; throw error }
        outgoing = snapshot.outgoing
        if isDemo { updateReply(pending.id, status: .sentToWechat); return }
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
            guard self.session?.pairId == session.pairId else { return }
            guard result.replyId == reply.id else { throw RelayError.invalidResponse }
            updateReply(reply.id, status: result.status)
            try saveCache()
            // A fast ACK event can arrive while this ID is excluded from refreshReplies.
            submitting.remove(reply.id)
            await refresh()
        } catch {
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            problem = "回复尚未确认提交. 可以点重试, 同一条回复不会重复创建. \(Self.describe(error))"
        }
    }

    private func refreshReplies(api: RelayAPI, session: RelaySession) async throws {
        for reply in outgoing where !reply.status.isTerminal && !submitting.contains(reply.id) {
            do {
                let result = try await api.replyStatus(session: session, id: reply.id)
                guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
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
        snapshot = InboxSnapshot(clearedThrough: cutoff, contacts: snapshot.contacts)
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
        snapshot = InboxSnapshot(); items = []; outgoing = []; friends = []; conversationPath = []
        contactsAvailable = nil; contactsProblem = nil
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
        refreshFriends()
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
            snapshot.contacts = [
                CachedContacts(version: 2, id: try UUIDv7.make(now: Date()), deviceId: "demo_android_0001", wechatUserId: 0, capturedAt: Int64(Date().timeIntervalSince1970 * 1_000), contacts: [try RelayContact(name: "林一"), try RelayContact(name: "陈默"), try RelayContact(name: "文件传输助手"), try RelayContact(name: "王珊")]),
                CachedContacts(version: 2, id: try UUIDv7.make(now: Date()), deviceId: "demo_android_0001", wechatUserId: 999, capturedAt: Int64(Date().timeIntervalSince1970 * 1_000), contacts: [try RelayContact(name: "小周"), try RelayContact(name: "赵晨")])
            ]
            refreshFriends()
            contactsAvailable = true
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
        case .contactSnapshotStale: return "好友名单已失效, 请在小米重新同步好友后重发"
        case .failed: return "Android 发送失败"
        }
    }
}
