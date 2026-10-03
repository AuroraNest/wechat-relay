import CryptoKit
import Foundation
import ImageIO
import RelayCore
import SwiftUI
import UserNotifications

struct InboxItem: Identifiable {
    let source: RelaySource
    let pairID: String
    let message: RelayMessage
    let preview: RelayPreview
    var nativeContent: RelayNativeContent? = nil
    var id: UUID { message.id }
    var pendingConversationID: String { "\(source.rawValue):\(pairID):pending:\(message.wechatUserId):\(message.id.uuidString.lowercased())" }
    var conversationID: String {
        if let nativeContent { return source.nativeConversationID(pairID: pairID, profile: message.wechatUserId, conversationID: nativeContent.conversationId) }
        if message.hasNativeContent { return pendingConversationID }
        return source.conversationID(pairID: pairID, profile: message.wechatUserId, name: preview.sender)
    }
    var conversationName: String {
        guard let content = nativeContent else {
            guard message.hasNativeContent else { return preview.sender }
            if preview.sender.hasSuffix("@chatroom") { return "群聊" }
            if preview.sender.isEmpty || preview.sender.hasPrefix("wxid_") { return "微信会话" }
            return preview.sender
        }
        let name = content.conversationName.trimmingCharacters(in: .whitespacesAndNewlines)
        if name.isEmpty || name == content.conversationId || name.hasSuffix("@chatroom") {
            return content.conversationId.hasSuffix("@chatroom") ? "群聊" : "微信会话"
        }
        return name
    }
    var body: String { nativeContent?.text.isEmpty == false ? nativeContent!.text : preview.body }
}

struct Conversation: Identifiable {
    let id: String
    let friend: RelayFriend
    let body: String
    let createdAt: Int64
    let unread: Int
}

struct RelayFriend: Identifiable, Hashable {
    let source: RelaySource
    let pairID: String
    let name: String
    let wechatUserId: Int
    let capturedAt: Int64
    var stableConversationID: String? = nil

    var id: String { stableConversationID ?? source.conversationID(pairID: pairID, profile: wechatUserId, name: name) }
    var profileLabel: String { wechatUserId == 999 ? "微信 2" : "微信" }
}

struct OutgoingMessage: Codable, Identifiable {
    let request: RelayReplyRequest
    var conversationID: String
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
    var accountFingerprint: String? = nil
}

private struct InboxSnapshot: Codable {
    var messages: [RelayMessage] = []
    var outgoing: [OutgoingMessage] = []
    var readThrough: [String: Int] = [:]
    var clearedThrough: Int = 0
    var contacts: [CachedContacts] = []
    var nativeContents: [String: RelayNativeContent] = [:]
    var tabletBootstrap: RelayReplyRequest? = nil
    var tabletBootstrapStatus: RelayReplyStatus? = nil

    enum CodingKeys: String, CodingKey { case messages, outgoing, readThrough, clearedThrough, contacts, nativeContents, tabletBootstrap, tabletBootstrapStatus }

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
        nativeContents = try values.decodeIfPresent([String: RelayNativeContent].self, forKey: .nativeContents) ?? [:]
        tabletBootstrap = try values.decodeIfPresent(RelayReplyRequest.self, forKey: .tabletBootstrap)
        tabletBootstrapStatus = try values.decodeIfPresent(RelayReplyStatus.self, forKey: .tabletBootstrapStatus)
    }
}

@MainActor
final class RelayConnectionModel: ObservableObject {
    let source: RelaySource
    private var pushSelected = false
    static let keychainService = "com.auroramaple.wechatrelay"
    static var keychainGroup: String? { Bundle.main.object(forInfoDictionaryKey: "RelayKeychainAccessGroup") as? String }

    @Published private(set) var session: RelaySession?
    @Published private(set) var items: [InboxItem] = []
    @Published private(set) var outgoing: [OutgoingMessage] = []
    @Published private(set) var device: RelayDeviceStatus?
    @Published private(set) var policy: RelayPolicy = .default
    @Published private(set) var pairing: RelayPairing?
    @Published private(set) var previewEnabled = false
    @Published private(set) var tabletReplyProblem: String?
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

    @Published private(set) var nativeContentProblems: [UUID: String] = [:]
    @Published private(set) var nativeAssetRevisions: [UUID: Int] = [:]
    private var pendingNativeAssets = Set<UUID>()
    private var nativeAssetTasks: [UUID: (id: UUID, task: Task<URL, Error>)] = [:]
    private var nativeContentTasks: [UUID: (id: UUID, task: Task<RelayNativeContentResponse, Error>)] = [:]
    private var nativeBackfillTask: (id: UUID, task: Task<Void, Never>)?
    private var nativeContentTerminal = Set<UUID>()
    private let nativeStore: RelayNativeStore
    private var snapshot = InboxSnapshot()
    private var loop: Task<Void, Never>?
    private var stream: Task<Void, Never>?
    private var foregroundRun: UUID?
    private var streamConnected = false
    private var refreshRequested = false
    private var apnsToken: String?
    private var pushDirty = true
    private var pushUpdating = false
    private var nextCursor: Int?
    private var cacheReadable = true
    private var lastStatusCheck = Date.distantPast
    private var submitting = Set<UUID>()
    private var preparingTabletReplyKey = false
    private var imageCache = NSCache<NSString, UIImage>()

    init(source: RelaySource, demo: Bool) {
        self.source = source
        nativeStore = RelayNativeStore(source: source)
        #if DEBUG
        isDemo = demo
        #else
        isDemo = false
        #endif
        imageCache.totalCostLimit = 24 * 1_024 * 1_024
        #if DEBUG
        if TabletInputRegression.enabled {
            guard source == .tablet else { return }
            TabletInputRegression.install()
            session = TabletInputRegression.session
            device = TabletInputRegression.device
            pushDirty = false
            do {
                try mergeContacts(TabletInputRegression.legacyContacts, currentDeviceID: TabletInputRegression.device.deviceId!, session: TabletInputRegression.session)
                snapshot.tabletBootstrap = TabletInputRegression.legacyBootstrap
                snapshot.tabletBootstrapStatus = .submitting
            } catch { problem = Self.describe(error) }
            return
        }
        #endif
        if isDemo {
            loadDemo()
            return
        }
        do {
            if source == .phone { try migrateLegacyStorage() }
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
        guard let session else { return [] }
        let incoming = Dictionary(grouping: items, by: \.conversationID)
        let sent = Dictionary(grouping: outgoing, by: \.conversationID)
        return Set(incoming.keys).union(sent.keys).compactMap { id in
            let values = incoming[id] ?? []
            let latest = values.max(by: { $0.message.seq < $1.message.seq })
            let reply = sent[id]?.max(by: { $0.request.createdAt < $1.request.createdAt })
            guard let profile = latest?.message.wechatUserId ?? reply?.request.wechatUserId else { return nil }
            let title = latest?.conversationName ?? String(id.dropFirst("\(source.rawValue):\(session.pairId):\(profile):".count))
            let useReply = (reply?.request.createdAt ?? 0) >= (latest?.message.createdAt ?? 0)
            let time = useReply ? reply!.request.createdAt : latest!.message.createdAt
            return Conversation(id: id, friend: RelayFriend(source: source, pairID: session.pairId, name: title, wechatUserId: profile, capturedAt: time, stableConversationID: id), body: useReply ? reply!.body : latest!.body, createdAt: time, unread: values.filter { $0.nativeContent?.isOutgoing != true && $0.message.seq > (snapshot.readThrough[id] ?? 0) }.count)
        }.sorted { $0.createdAt > $1.createdAt }
    }

    var friendsCapturedAt: Date? {
        snapshot.contacts.map(\.capturedAt).max().map { Date(timeIntervalSince1970: Double($0) / 1_000) }
    }

    var stateTitle: String {
        if isDemo { return "演示模式 · 虚构数据" }
        if problem != nil { return "连接需要检查" }
        if device?.paired != true { return "等待\(source.label)配对" }
        return policy.active ? "转发中" : "已暂停转发"
    }

    func start() {
        guard !isDemo, loop == nil else { return }
        let run = UUID()
        foregroundRun = run
        loop = Task { [weak self] in
            await self?.refreshNotificationPermission()
            guard let self, self.foregroundRun == run, !Task.isCancelled else { return }
            await self.refresh()
            guard self.foregroundRun == run, !Task.isCancelled else { return }
            self.startEventStream(run: run)
        }
    }

    func stop() {
        foregroundRun = nil
        loop?.cancel(); loop = nil
        stream?.cancel(); stream = nil
        nativeBackfillTask?.task.cancel(); nativeBackfillTask = nil
        nativeContentTasks.values.forEach { $0.task.cancel() }
        nativeAssetTasks.values.forEach { $0.task.cancel() }
        nativeAssetTasks = [:]
        streamConnected = false
        refreshRequested = false
    }

    private func startEventStream(run: UUID) {
        guard stream == nil, let session, cacheReadable else { return }
        stream = Task { [weak self] in
            var delay = 1
            while !Task.isCancelled {
                guard let self, self.foregroundRun == run, self.session?.pairId == session.pairId else { return }
                do {
                    let api = try makeAPI(origin: session.origin)
                    let cursor = max(self.snapshot.clearedThrough, self.snapshot.messages.map(\.seq).max() ?? 0)
                    try await api.streamEvents(session: session, afterSeq: cursor) { [weak self] event in
                        await self?.receiveStreamEvent(event, pairID: session.pairId, run: run)
                    }
                } catch {
                    guard !Task.isCancelled, self.foregroundRun == run else { return }
                    if self.streamConnected { delay = 1 }
                    self.streamConnected = false
                    // Reconnecting emits ready and reconciles missed messages without periodic polling.
                }
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, 30)
            }
        }
    }

    private func receiveStreamEvent(_ event: RelayStreamEvent, pairID: String, run: UUID) async {
        guard !Task.isCancelled, foregroundRun == run, session?.pairId == pairID else { return }
        if case .assetReady(let assetID) = event {
            guard let message = snapshot.messages.first(where: { $0.nativeAssetSlots.contains(where: { $0.id == assetID }) || $0.nativeAssets.contains(where: { $0.id == assetID }) }) else { return }
            if message.nativeVersion == 8, let item = items.first(where: { $0.id == message.id }) {
                do { _ = try await loadNativeContent(for: item, refreshManifest: true) }
                catch {
                    // A failed upload hint must recover through the canonical message manifest.
                    await refresh()
                    return
                }
                guard foregroundRun == run, self.session?.pairId == pairID else { return }
            }
            nativeAssetRevisions[assetID, default: 0] += 1
            return
        }
        if case .ready = event {
            streamConnected = true
            let waiting = pendingNativeAssets.union(nativeAssetTasks.keys)
            let messages = items.filter { item in
                item.message.nativeVersion == 8 && (item.message.hasUnresolvedNativeSlots || item.message.nativeAssetSlots.contains { waiting.contains($0.id) })
            }
            for item in messages {
                guard foregroundRun == run, self.session?.pairId == pairID, !Task.isCancelled else { return }
                _ = try? await loadNativeContent(for: item, refreshManifest: true)
            }
            // A disconnected stream can miss uploads without changing the message cursor.
            for assetID in pendingNativeAssets.union(nativeAssetTasks.keys) { nativeAssetRevisions[assetID, default: 0] += 1 }
        }
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
        guard let url = URL(string: origin.trimmingCharacters(in: .whitespacesAndNewlines)) else { throw RelayError.invalidOrigin }
        let api = try makeAPI(origin: url)
        guard !isDemo, session == nil, cacheReadable else { throw RelayError.invalidValue("existing connection") }
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
        if let run = foregroundRun, !Task.isCancelled {
            startEventStream(run: run)
        }
    }

    private func makeAPI(origin: URL) throws -> RelayAPI {
        #if DEBUG
        return try RelayAPI(origin: origin, configuration: TabletInputRegression.configuration)
        #else
        return try RelayAPI(origin: origin)
        #endif
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
            let api = try makeAPI(origin: session.origin)
            if device?.paired != true || Date().timeIntervalSince(lastStatusCheck) > 25 {
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
            startNativeBackfill()
            if source == .tablet, device?.tabletContactSendAvailable == true, let deviceID = device?.deviceId {
                do { try await syncContacts(api: api, session: session, deviceID: deviceID) }
                catch { contactsProblem = Self.describe(error) }
            }
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            do {
                try await ensureTabletReplyKey(api: api, session: session)
                tabletReplyProblem = nil
            } catch {
                if self.session?.pairId == session.pairId { tabletReplyProblem = "平板回复通道尚未就绪, 收件不受影响" }
            }
            try await refreshReplies(api: api, session: session)
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            if policy.updatedAt == 0 || Date().timeIntervalSince(lastStatusCheck) < 2 {
                let currentPolicy = try await api.policy(session: session)
                guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
                policy = currentPolicy
            }
            if pushDirty {
                try await reconcilePush()
                guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
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
            // Attachments can arrive without advancing the message cursor.
            for incoming in page.messages where incoming.seq <= known && incoming.nativeVersion == 8 {
                guard let cached = snapshot.messages.first(where: { $0.id == incoming.id }), cached.hasUnresolvedNativeSlots else { continue }
                _ = try RelayCrypto.decryptPreview(incoming, messageKey: session.messageKey)
                let updated = try cached.mergingNativeManifest(slots: incoming.nativeAssetSlots, assets: incoming.nativeAssets)
                if updated != cached { collected.append(updated) }
            }
            let next = page.nextCursor.flatMap(Int.init)
            if cursor == nil && !page.hasMore { nextCursor = nil; hasMore = false }
            else if snapshot.messages.isEmpty && cursor == nil { nextCursor = next; hasMore = page.hasMore && snapshot.clearedThrough == 0 }
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
            let page = try await makeAPI(origin: session.origin).messages(session: session, beforeSeq: nextCursor)
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            try merge(page.messages.filter { $0.seq > snapshot.clearedThrough })
            self.nextCursor = page.nextCursor.flatMap(Int.init)
            hasMore = page.hasMore && self.nextCursor != nil
            try saveCache()
            startNativeBackfill()
        } catch {
            if self.session?.pairId == session.pairId, !Task.isCancelled { problem = Self.describe(error) }
        }
    }

    func refreshContacts() async {
        guard !isDemo, let session, !isRefreshingContacts, cacheReadable else { return }
        isRefreshingContacts = true
        defer { isRefreshingContacts = false }
        do {
            let api = try makeAPI(origin: session.origin)
            let currentDevice = try await api.deviceStatus(session: session)
            guard self.session?.pairId == session.pairId, !Task.isCancelled else { return }
            device = currentDevice
            lastStatusCheck = Date()
            guard currentDevice.paired, let currentDeviceID = currentDevice.deviceId,
                  currentDeviceID.range(of: "^[A-Za-z0-9_-]{16,128}$", options: .regularExpression) != nil else {
                throw RelayError.invalidResponse
            }
            try await syncContacts(api: api, session: session, deviceID: currentDeviceID)
            do {
                try await ensureTabletReplyKey(api: api, session: session, retryFailed: true)
                tabletReplyProblem = nil
            } catch {
                if self.session?.pairId == session.pairId { tabletReplyProblem = "平板回复通道尚未就绪, 收件不受影响" }
            }
        } catch is CancellationError {
        } catch RelayError.httpStatus(404) {
            // Older Relay servers do not expose contacts. Keep message synchronization independent.
            contactsAvailable = false
            contactsProblem = nil
        } catch {
            if !Task.isCancelled { contactsProblem = Self.describe(error) }
        }
    }

    private func syncContacts(api: RelayAPI, session: RelaySession, deviceID: String) async throws {
        let snapshots = try await api.contacts(session: session)
        guard self.session?.pairId == session.pairId, !Task.isCancelled else { throw CancellationError() }
        try mergeContacts(snapshots, currentDeviceID: deviceID, session: session)
        contactsAvailable = true
        contactsProblem = nil
    }

    private func merge(_ messages: [RelayMessage]) throws {
        guard let session else { return }
        var unique = Dictionary(uniqueKeysWithValues: snapshot.messages.map { ($0.id, $0) })
        for message in messages {
            _ = try RelayCrypto.decryptPreview(message, messageKey: session.messageKey)
            unique[message.id] = message
        }
        snapshot.messages = unique.values.sorted { $0.seq < $1.seq }
        items = try snapshot.messages.map { InboxItem(source: source, pairID: session.pairId, message: $0, preview: try RelayCrypto.decryptPreview($0, messageKey: session.messageKey), nativeContent: snapshot.nativeContents[$0.id.uuidString]) }
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
            cached[remote.wechatUserId] = CachedContacts(version: remote.v, id: remote.id, deviceId: remote.deviceId, wechatUserId: remote.wechatUserId, capturedAt: remote.capturedAt, contacts: payload.contacts, accountFingerprint: payload.accountFingerprint)
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
        guard let session else { friends = []; return }
        friends = snapshot.contacts.flatMap { snapshot in
            snapshot.contacts.map { contact in
                let stableID = source == .tablet && snapshot.version == 3 ? contact.conversationId.map { source.nativeConversationID(pairID: session.pairId, profile: snapshot.wechatUserId, conversationID: $0) } : nil
                return RelayFriend(source: source, pairID: session.pairId, name: contact.name, wechatUserId: snapshot.wechatUserId, capturedAt: snapshot.capturedAt, stableConversationID: stableID)
            }
        }.sorted {
            let comparison = $0.name.localizedStandardCompare($1.name)
            if comparison == .orderedSame { return $0.wechatUserId < $1.wechatUserId }
            return comparison == .orderedAscending
        }
    }

    func canSend(to friend: RelayFriend) -> Bool {
        guard friend.source == source, friend.pairID == session?.pairId else { return false }
        guard let contacts = contacts(for: friend) else { return false }
        if source == .tablet, !isDemo {
            return device?.tabletContactSendAvailable == true && tabletReplyKeyInstalled && contacts.version == 3 && contacts.accountFingerprint != nil &&
                contacts.contacts.contains { contact in
                    guard let identity = contact.conversationId, !identity.hasSuffix("@chatroom"), contact.alias?.isEmpty == false else { return false }
                    return source.nativeConversationID(pairID: friend.pairID, profile: friend.wechatUserId, conversationID: identity) == friend.id
                }
        }
        return contacts.version == 2 && contacts.contacts.contains(where: { $0.name == friend.name })
    }

    private func contacts(for friend: RelayFriend) -> CachedContacts? {
        snapshot.contacts.first { $0.deviceId == device?.deviceId && $0.wechatUserId == friend.wechatUserId }
    }

    private var tabletReplyKeyInstalled: Bool { isDemo || (snapshot.tabletBootstrap?.deviceId == device?.deviceId && snapshot.tabletBootstrapStatus == .replyKeyInstalled) }

    func canSend(to target: InboxItem) -> Bool {
        guard target.source == source, target.pairID == session?.pairId, target.message.deviceId == device?.deviceId else { return false }
        if target.message.hasNativeContent {
            return source == .tablet && device?.tabletRepliesAvailable == true && tabletReplyKeyInstalled && RelayCrypto.canReplyToTablet(target: target.message, content: target.nativeContent)
        }
        return target.message.replyCapable
    }

    func sendUnavailableReason(to friend: RelayFriend) -> String? {
        guard !canSend(to: friend) else { return nil }
        guard let contacts = contacts(for: friend) else { return "好友信息尚未同步. 请重新同步好友." }
        guard source == .tablet, !isDemo else { return "此来源尚未提供主动发送能力. 请更新来源端并重新同步好友." }
        guard device?.tabletContactSendAvailable == true else { return "平板尚未提供好友发送能力. 请更新来源端并重新同步好友." }
        guard contacts.version == 3, contacts.accountFingerprint != nil else { return "好友身份信息尚未就绪. 请重新同步平板好友." }
        guard let contact = contacts.contacts.first(where: { contact in contact.conversationId.map { source.nativeConversationID(pairID: friend.pairID, profile: friend.wechatUserId, conversationID: $0) } == friend.id }) else { return "好友信息已更新. 请重新打开好友会话." }
        if contact.conversationId?.hasSuffix("@chatroom") == true { return "平板暂不支持群聊发送." }
        if contact.alias?.isEmpty != false { return "此好友缺少微信号, 暂时无法安全发送. 请在微信确认微信号后重新同步好友." }
        return "平板发送通道尚未就绪. 请稍后刷新."
    }

    func sendUnavailableReason(to target: InboxItem) -> String? {
        guard !canSend(to: target) else { return nil }
        guard target.message.hasNativeContent else { return "这条消息暂不支持回复." }
        if target.nativeContent?.conversationId.hasSuffix("@chatroom") == true { return "平板暂不支持群聊发送." }
        guard device?.tabletRepliesAvailable == true else { return "平板回复能力尚未提供. 请更新来源端后刷新." }
        guard target.nativeContent != nil else { return "消息身份信息尚在加载, 请稍后重试." }
        guard RelayCrypto.canReplyToTablet(target: target.message, content: target.nativeContent) else { return "这条消息暂不支持安全回复." }
        return "平板发送通道尚未就绪. 请稍后刷新."
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
        guard let session, canSend(to: target) else { throw RelayError.invalidValue("reply target") }
        let text = body.trimmingCharacters(in: .whitespacesAndNewlines)
        let request: RelayReplyRequest
        if target.message.hasNativeContent {
            guard source == .tablet, device?.tabletRepliesAvailable == true, let content = target.nativeContent else { throw RelayError.invalidValue("tablet content") }
            try await ensureTabletReplyKey(api: makeAPI(origin: session.origin), session: session)
            guard self.session?.pairId == session.pairId, tabletReplyKeyInstalled else { throw RelayError.invalidValue("tablet reply key") }
            request = try RelayCrypto.makeTabletReply(session: session, target: target.message, content: content, body: text)
        } else {
            request = try RelayCrypto.makeReply(session: session, target: target.message, body: text)
        }
        let pending = OutgoingMessage(request: request, conversationID: target.conversationID, body: text, status: .submitting)
        try await enqueue(pending)
    }

    private func ensureTabletReplyKey(api: RelayAPI, session: RelaySession, retryFailed: Bool = false) async throws {
        guard source == .tablet, !isDemo, let deviceID = device?.deviceId, !preparingTabletReplyKey else { return }
        preparingTabletReplyKey = true
        defer { preparingTabletReplyKey = false }
        if tabletReplyKeyInstalled { return }
        let target = device?.tabletRepliesAvailable == true ? snapshot.messages.last(where: { $0.hasNativeContent && $0.deviceId == deviceID }) : nil
        let contacts = device?.tabletContactSendAvailable == true ? snapshot.contacts.first(where: { $0.version == 3 && $0.wechatUserId == 0 && $0.deviceId == deviceID }) : nil
        guard target != nil || contacts != nil else { return }
        // Contact bootstrap installs the same key without relying on an old message remaining available.
        let migrateLegacyBootstrap = contacts != nil && snapshot.tabletBootstrap?.v == 6
        func makeBootstrap() throws -> RelayReplyRequest {
            if let contacts { return try RelayCrypto.makeTabletContactReplyBootstrap(session: session, snapshotId: contacts.id, deviceId: deviceID) }
            guard let target else { throw RelayError.invalidValue("tablet bootstrap target") }
            return try RelayCrypto.makeTabletReplyBootstrap(session: session, target: target)
        }
        let contactChanged = snapshot.tabletBootstrap?.v == 8 && snapshot.tabletBootstrap?.targetContactSnapshotId != contacts?.id
        let retryTerminalBootstrap = retryFailed && snapshot.tabletBootstrapStatus?.isTerminal == true
        if snapshot.tabletBootstrap?.deviceId != deviceID || contactChanged || migrateLegacyBootstrap || retryTerminalBootstrap {
            snapshot.tabletBootstrap = try makeBootstrap()
            snapshot.tabletBootstrapStatus = .submitting
            try saveCache()
        }
        guard var command = snapshot.tabletBootstrap, snapshot.tabletBootstrapStatus != .replyKeyInstalled else { return }
        let result: RelayReplyResult
        do {
            result = try await api.replyStatus(session: session, id: command.id)
        } catch RelayError.httpStatus(404) where snapshot.tabletBootstrapStatus == .submitting {
            guard self.session?.pairId == session.pairId else { throw RelayError.invalidResponse }
            // Only a confirmed absence permits replacement of an expired, never-accepted control command.
            if Int64(Date().timeIntervalSince1970 * 1_000) - command.createdAt > 120_000 {
                command = try makeBootstrap()
                snapshot.tabletBootstrap = command
                try saveCache()
            }
            result = try await api.submitReply(session: session, request: command)
        }
        guard self.session?.pairId == session.pairId, result.replyId == command.id, snapshot.tabletBootstrap?.id == command.id else { throw RelayError.invalidResponse }
        snapshot.tabletBootstrapStatus = result.status
        try saveCache()
        guard result.status == .replyKeyInstalled else { throw RelayError.invalidValue("tablet reply key") }
    }

    func send(_ body: String, to friend: RelayFriend) async throws {
        guard let session, canSend(to: friend),
              let contacts = contacts(for: friend) else {
            throw RelayError.invalidValue("contact snapshot")
        }
        let text = body.trimmingCharacters(in: .whitespacesAndNewlines)
        let request: RelayReplyRequest
        if source == .tablet, !isDemo {
            try await ensureTabletReplyKey(api: makeAPI(origin: session.origin), session: session)
            guard self.session?.pairId == session.pairId, canSend(to: friend), let account = contacts.accountFingerprint,
                  let contact = contacts.contacts.first(where: { contact in contact.conversationId.map { source.nativeConversationID(pairID: session.pairId, profile: friend.wechatUserId, conversationID: $0) } == friend.id }) else { throw RelayError.invalidValue("tablet contact") }
            request = try RelayCrypto.makeTabletContactSend(session: session, snapshotId: contacts.id, deviceId: contacts.deviceId, contact: contact, accountFingerprint: account, body: text)
        } else {
            request = try RelayCrypto.makeContactSend(session: session, snapshotId: contacts.id, deviceId: contacts.deviceId, wechatUserId: friend.wechatUserId, conversationTitle: friend.name, body: text)
        }
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
        guard let session, source.owns(reply.conversationID, pairID: session.pairId), !submitting.contains(reply.id) else { return }
        submitting.insert(reply.id)
        defer { submitting.remove(reply.id) }
        do {
            let result = try await makeAPI(origin: session.origin).submitReply(session: session, request: reply.request)
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
        policy = try await makeAPI(origin: session.origin).savePolicy(session: session, policy: value)
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
        let asset = try await makeAPI(origin: session.origin).asset(session: session, id: metadata.id)
        let data = try RelayCrypto.decryptAsset(asset, for: message, messageKey: session.messageKey)
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 2_048, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else { throw RelayError.invalidResponse }
        let image = UIImage(cgImage: thumbnail)
        imageCache.setObject(image, forKey: key, cost: thumbnail.bytesPerRow * thumbnail.height)
        return image
    }

    func loadNativeContent(for item: InboxItem, refreshManifest: Bool = false) async throws -> RelayNativeContent {
        guard let session, item.source == source, item.pairID == session.pairId, item.message.hasNativeContent else { throw RelayError.invalidValue("content source") }
        if !refreshManifest, let cached = snapshot.nativeContents[item.id.uuidString] { return cached }
        if refreshManifest, let pending = nativeContentTasks[item.id] {
            // A hint may follow the snapshot of an already-running GET; read again after it finishes.
            _ = try? await pending.task.value
            if nativeContentTasks[item.id]?.id == pending.id { nativeContentTasks[item.id] = nil }
            try Task.checkCancellation()
        }
        let requestID: UUID
        let task: Task<RelayNativeContentResponse, Error>
        if let pending = nativeContentTasks[item.id] {
            requestID = pending.id
            task = pending.task
        } else {
            requestID = UUID()
            task = Task {
                let response = try await makeAPI(origin: session.origin).nativeManifest(session: session, messageID: item.id)
                try Task.checkCancellation()
                return response
            }
            nativeContentTasks[item.id] = (requestID, task)
        }
        defer { if nativeContentTasks[item.id]?.id == requestID { nativeContentTasks[item.id] = nil } }
        do {
            let response = try await task.value
            guard self.session?.pairId == session.pairId, let index = snapshot.messages.firstIndex(where: { $0.id == item.id }), !Task.isCancelled else { throw CancellationError() }
            var message = snapshot.messages[index]
            if message.nativeVersion == 8 {
                guard let slots = response.nativeAssetSlots, let assets = response.nativeAssets else { throw RelayError.invalidResponse }
                message = try message.mergingNativeManifest(slots: slots, assets: assets)
            }
            let content = try RelayCrypto.decryptNativeContent(response.contentEnvelope, for: message, messageKey: session.messageKey, pairID: session.pairId)
            if let cached = snapshot.nativeContents[item.id.uuidString], cached != content { throw RelayError.invalidResponse }
            snapshot.messages[index] = message
            snapshot.nativeContents[item.id.uuidString] = content
            if let read = snapshot.readThrough.removeValue(forKey: item.pendingConversationID) {
                let stable = source.nativeConversationID(pairID: session.pairId, profile: item.message.wechatUserId, conversationID: content.conversationId)
                snapshot.readThrough[stable] = max(snapshot.readThrough[stable] ?? 0, read)
            }
            try merge([])
            try saveCache()
            nativeContentProblems[item.id] = nil
            nativeContentTerminal.remove(item.id)
            return content
        } catch {
            if self.session?.pairId == session.pairId, !(error is CancellationError) {
                nativeContentProblems[item.id] = Self.describe(error)
                if error as? RelayError == .httpStatus(404) || error as? RelayError == .httpStatus(410) { nativeContentTerminal.insert(item.id) }
            }
            throw error
        }
    }

    private func startNativeBackfill() {
        guard nativeBackfillTask == nil else { return }
        let id = UUID()
        let task = Task { [weak self] in
            guard let self else { return }
            await self.refreshNativeContents()
            if self.nativeBackfillTask?.id == id { self.nativeBackfillTask = nil }
        }
        nativeBackfillTask = (id, task)
    }

    private func refreshNativeContents() async {
        // Bound foreground backfill independently of SSE seq. Visible cards can request an older item directly.
        for item in items.reversed().filter({ $0.message.hasNativeContent && $0.nativeContent == nil && !nativeContentTerminal.contains($0.id) }).prefix(8) {
            guard !Task.isCancelled else { return }
            _ = try? await loadNativeContent(for: item)
        }
    }

    func nativeAssetURL(_ metadata: RelayNativeAssetMetadata, in item: InboxItem) async throws -> URL {
        guard let session, item.source == source, item.pairID == session.pairId else { throw RelayError.invalidValue("asset source") }
        let content = try await loadNativeContent(for: item)
        let requestID: UUID
        let task: Task<URL, Error>
        if let existing = nativeAssetTasks[metadata.id] {
            requestID = existing.id; task = existing.task
        } else {
            requestID = UUID()
            task = Task {
                while true {
                    let revision = nativeAssetRevisions[metadata.id, default: 0]
                    do { return try await nativeStore.file(metadata: metadata, message: item.message, content: content, session: session) }
                    catch RelayError.assetPending {
                        try Task.checkCancellation()
                        // An upload hint can arrive while the pending response is in flight.
                        guard revision != nativeAssetRevisions[metadata.id, default: 0] else { throw RelayError.assetPending }
                    }
                }
            }
            nativeAssetTasks[metadata.id] = (requestID, task)
        }
        defer { if nativeAssetTasks[metadata.id]?.id == requestID { nativeAssetTasks[metadata.id] = nil } }
        do {
            let url = try await task.value
            pendingNativeAssets.remove(metadata.id)
            guard self.session?.pairId == session.pairId, snapshot.messages.contains(where: { $0.id == item.id }), !Task.isCancelled else { throw CancellationError() }
            return url
        } catch RelayError.assetPending {
            if self.session?.pairId == session.pairId { pendingNativeAssets.insert(metadata.id) }
            throw RelayError.assetPending
        } catch {
            if !(error is CancellationError) { pendingNativeAssets.remove(metadata.id) }
            throw error
        }
    }

    func clearHistory() throws {
        let cutoff = max(snapshot.clearedThrough, snapshot.messages.map(\.seq).max() ?? 0)
        let previous = snapshot
        let wasReadable = cacheReadable
        snapshot = InboxSnapshot(clearedThrough: cutoff, contacts: snapshot.contacts)
        cacheReadable = true
        do { try saveCache() } catch { snapshot = previous; cacheReadable = wasReadable; throw error }
        nativeContentProblems = [:]; nativeContentTerminal = []
        nativeAssetTasks.values.forEach { $0.task.cancel() }
        nativeAssetTasks = [:]; pendingNativeAssets = []; nativeAssetRevisions = [:]
        items = []; outgoing = []; imageCache.removeAllObjects(); hasMore = false
        if let session { try nativeStore.clear(session: session) }
        problem = nil
        removeDeliveredNotifications()
    }

    func disconnect() throws {
        stop()
        if !isDemo {
            let file = try cacheURL()
            if let session { try nativeStore.clear(session: session) }
            try deleteSecret(account: "session")
            try deleteSecret(account: "pending-pairing")
            if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
        }
        removeDeliveredNotifications()
        nativeContentProblems = [:]; nativeContentTerminal = []
        pendingNativeAssets = []; nativeAssetRevisions = [:]
        session = nil; pairing = nil; device = nil
        isDemo = false
        snapshot = InboxSnapshot(); items = []; outgoing = []; friends = []; conversationPath = []
        contactsAvailable = nil; contactsProblem = nil
        imageCache.removeAllObjects(); problem = nil; cacheReadable = true
        lastStatusCheck = .distantPast
    }

    func disablePushAndDisconnect() async throws {
        if !isDemo {
            pushSelected = false
            pushDirty = true
            while pushUpdating { try await Task.sleep(for: .milliseconds(50)) }
            try await reconcilePush()
        }
        try disconnect()
    }

    private func removeDeliveredNotifications() {
        guard let pairID = session?.pairId else { return }
        let center = UNUserNotificationCenter.current()
        center.getDeliveredNotifications { notifications in
            center.removeDeliveredNotifications(withIdentifiers: notifications.filter { $0.request.content.userInfo["pairId"] as? String == pairID }.map { $0.request.identifier })
        }
    }

    func setPushSelected(_ selected: Bool) async {
        if pushSelected != selected { pushSelected = selected; pushDirty = true }
        do { try await reconcilePush() } catch { problem = Self.describe(error) }
    }

    private func reconcilePush() async throws {
        guard !isDemo, cacheReadable, !pushUpdating else { return }
        pushUpdating = true
        defer { pushUpdating = false }
        while pushDirty, let session {
            let selected = pushSelected
            let token = apnsToken
            let preview = previewEnabled
            try await makeAPI(origin: session.origin).updatePush(session: session, token: selected ? token : nil, environment: pushEnvironment, previewEnabled: selected && preview)
            guard self.session?.pairId == session.pairId else { return }
            if selected == pushSelected, token == apnsToken, preview == previewEnabled { pushDirty = false }
        }
    }

    private func secretAccount(_ account: String) -> String {
        account == "apns-token" ? account : source.account(account)
    }

    private func migrateLegacyStorage() throws {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        try RelayStorageMigration.migrateLegacyPhone(service: Self.keychainService, accessGroup: Self.keychainGroup, directory: directory)
    }

    private var pushEnvironment: RelayPushEnvironment {
        RelayPushEnvironment(rawValue: Bundle.main.object(forInfoDictionaryKey: "RelayAPNSEnvironment") as? String ?? "sandbox") ?? .sandbox
    }

    private func loadSecret<T: Decodable>(_ type: T.Type, account: String) throws -> T? {
        try RelayKeychain.load(type, account: secretAccount(account), service: Self.keychainService, accessGroup: Self.keychainGroup)
    }
    private func saveSecret<T: Encodable>(_ value: T, account: String) throws {
        try RelayKeychain.save(value, account: secretAccount(account), service: Self.keychainService, accessGroup: Self.keychainGroup)
    }
    private func deleteSecret(account: String) throws {
        try RelayKeychain.delete(account: secretAccount(account), service: Self.keychainService, accessGroup: Self.keychainGroup)
    }

    private func cacheURL() throws -> URL {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        guard let session else { throw RelayError.invalidValue("session") }
        return directory.appendingPathComponent("inbox-\(source.rawValue)-\(session.pairId).sealed")
    }

    private func restoreCache() throws {
        guard let session else { return }
        let url = try cacheURL()
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        let box = try AES.GCM.SealedBox(combined: Data(contentsOf: url))
        let data = try AES.GCM.open(box, using: SymmetricKey(data: session.messageKey), authenticating: Data("AWR1|IOS_CACHE|\(session.pairId)".utf8))
        snapshot = try JSONDecoder().decode(InboxSnapshot.self, from: data)
        let prefix = "\(source.rawValue):\(session.pairId):"
        snapshot.readThrough = Dictionary(uniqueKeysWithValues: snapshot.readThrough.map { ($0.key.hasPrefix(prefix) ? $0.key : prefix + $0.key, $0.value) })
        for index in snapshot.outgoing.indices where !snapshot.outgoing[index].conversationID.hasPrefix(prefix) {
            snapshot.outgoing[index].conversationID = prefix + snapshot.outgoing[index].conversationID
        }
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
        case RelayError.invalidValue("message no longer available"): return "这条通知对应的消息不在本机记录中. 请从消息列表查看最新消息."
        case RelayError.assetPending: return "原始附件仍在上传, 稍后会自动重试, 也可手动刷新."
        case RelayError.assetExpired: return "原始附件已超过服务端保留期, 无法重新下载."
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
            session = try RelaySession(origin: URL(string: "https://example.invalid")!, ackToken: "demo", pairId: UUID().uuidString, messageKey: Data(repeating: source == .tablet ? 11 : 7, count: 32), replyKey: Data(repeating: source == .tablet ? 13 : 9, count: 32))
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
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("--demo-native") { try loadNativeDemo(session: session) }
            if ProcessInfo.processInfo.arguments.contains("--demo-native-image") {
                try loadNativeImageDemo(session: session, previewOnly: ProcessInfo.processInfo.arguments.contains("--preview-only"))
            }
            #endif
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
    #if DEBUG
    private func loadNativeImageDemo(session: RelaySession, previewOnly: Bool) throws {
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: 600, height: 400))
        let bytes = renderer.jpegData(withCompressionQuality: 0.9) { context in
            UIColor.systemBlue.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 600, height: 400))
            UIColor.systemYellow.setFill()
            context.fill(CGRect(x: 50, y: 50, width: 500, height: 300))
        }
        let id = try UUIDv7.make(), originalID = try UUIDv7.make(), previewID = try UUIDv7.make()
        let timestamp = Int64(Date().timeIntervalSince1970 * 1_000)
        let deviceID = "demo_native_0001"
        let metadata = try RelayNativeAssetMetadata(id: previewOnly ? previewID : originalID, kind: .image, mimeType: "image/jpeg", byteLength: bytes.count, role: previewOnly ? .playback : .original, derivedFrom: previewOnly ? originalID : nil)
        let preview = RelayPreview(sender: "图片查看演示", body: "点击图片查看全屏")
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(preview), key: session.messageKey, kid: "phase1", aad: "AWR1|A2I|\(id.uuidString.lowercased())|\(deviceID)|8|\(timestamp)|0")
        let slots = [RelayNativeAssetSlot(id: originalID, kind: .image, role: .original), RelayNativeAssetSlot(id: previewID, kind: .image, role: .playback, derivedFrom: originalID)]
        let message = try RelayMessage(messageId: id, deviceId: deviceID, seq: 8, createdAt: timestamp, wechatUserId: 0, replyCapable: false, conversationSendCapable: false, previewEnvelope: envelope, assets: [], receivedAt: timestamp, hasNativeContent: true, nativeAssets: [metadata], nativeVersion: 8, nativeAssetSlots: slots)
        let payload: [String: Any] = ["v": 1, "conversationId": "native-image-demo", "conversationName": "图片查看演示", "senderId": "demo-sender", "senderName": "图片查看演示", "kind": "image", "text": "", "attachments": [["assetId": originalID.uuidString.lowercased(), "name": "original.jpg"], ["assetId": previewID.uuidString.lowercased(), "name": "preview.jpg"]]]
        snapshot.messages.append(message)
        snapshot.nativeContents[id.uuidString] = try JSONDecoder().decode(RelayNativeContent.self, from: JSONSerialization.data(withJSONObject: payload))
        let assetAAD = "AWR1|A2I_ASSET|8|\(session.pairId)|\(id.uuidString.lowercased())|\(metadata.id.uuidString.lowercased())|\(deviceID)|8|\(timestamp)|0|image|image/jpeg|\(bytes.count)|\(metadata.role.rawValue)|\(metadata.derivedFrom?.uuidString.lowercased() ?? "")"
        let assetEnvelope = try RelayCrypto.encrypt(bytes, key: session.messageKey, kid: "phase2-asset", aad: assetAAD)
        var assetObject = try JSONSerialization.jsonObject(with: JSONEncoder().encode(metadata)) as! [String: Any]
        assetObject["envelope"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(assetEnvelope))
        nativeStore.seedDemo(try JSONDecoder().decode(RelayNativeAsset.self, from: JSONSerialization.data(withJSONObject: assetObject)))
    }

    private func loadNativeDemo(session: RelaySession) throws {
        let assetID = try UUIDv7.make()
        let bytes = Data("原始附件演示字节, 不进行摘要或转写.\n".utf8)
        let metadata = try RelayNativeAssetMetadata(id: assetID, kind: .file, mimeType: "text/plain", byteLength: bytes.count, role: .original)
        let id = try UUIDv7.make()
        let createdAt = Int64(Date().timeIntervalSince1970 * 1_000)
        let deviceID = "demo_native_0001"
        let preview = RelayPreview(sender: "原始内容演示", body: "完整原始消息和附件")
        let envelope = try RelayCrypto.encrypt(JSONEncoder().encode(preview), key: session.messageKey, kid: "phase1", aad: "AWR1|A2I|\(id.uuidString.lowercased())|\(deviceID)|7|\(createdAt)|0")
        let message = try RelayMessage(messageId: id, deviceId: deviceID, seq: 7, createdAt: createdAt, wechatUserId: 0, replyCapable: false, conversationSendCapable: false, previewEnvelope: envelope, assets: [], receivedAt: createdAt, hasNativeContent: true, nativeAssets: [metadata])
        let payload: [String: Any] = ["v": 1, "conversationId": "native-demo-room", "conversationName": "原始内容演示", "senderId": "demo-sender", "senderName": "原发送者", "kind": "reference", "text": "完整正文开头\n" + String(repeating: "这一段保留完整原始文本. ", count: 40) + "\n完整正文结束", "rawXML": "<msg><script>仅作为原始文本显示</script></msg>", "attachments": [["assetId": assetID.uuidString.lowercased(), "name": "original-demo.txt"]], "records": [["senderName": "嵌套记录发送者", "kind": "record", "text": "嵌套记录完整文本", "rawXML": "<record><nested>完整保留</nested></record>"]]]
        snapshot.messages.append(message)
        snapshot.nativeContents[id.uuidString] = try JSONDecoder().decode(RelayNativeContent.self, from: JSONSerialization.data(withJSONObject: payload))
        let assetAAD = "AWR1|A2I_ASSET|6|\(id.uuidString.lowercased())|\(assetID.uuidString.lowercased())|\(deviceID)|7|\(createdAt)|0|file|text/plain|\(bytes.count)|original|"
        let assetEnvelope = try RelayCrypto.encrypt(bytes, key: session.messageKey, kid: "phase2-asset", aad: assetAAD)
        var assetObject = try JSONSerialization.jsonObject(with: JSONEncoder().encode(metadata)) as! [String: Any]
        assetObject["envelope"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(assetEnvelope))
        nativeStore.seedDemo(try JSONDecoder().decode(RelayNativeAsset.self, from: JSONSerialization.data(withJSONObject: assetObject)))
    }
    #endif

}

extension RelayReplyStatus {
    var label: String {
        switch self {
        case .submitting: return "提交尚未确认 · 点按重试"
        case .queued: return "等待来源设备处理"
        case .deliveredToAndroid: return "来源设备正在处理"
        case .sentToWechat: return "已发往微信"
        case .replyKeyInstalled: return "平板回复密钥已就绪"
        case .sendUnconfirmed: return "发送结果未确认, 请先在微信核实, 勿重复发送"
        case .notificationNotActive: return "原通知已失效, 请等待对方的新消息"
        case .wechatActionChanged: return "微信回复入口已变化"
        case .remoteInputUnsupported: return "旧版通知快捷回复不可用"
        case .pendingIntentCanceled: return "微信回复入口已失效"
        case .invalidReply: return "回复内容未通过校验"
        case .contactSnapshotStale: return "好友名单已失效, 请在小米重新同步好友后重发"
        case .automationNotReady: return "自动回复尚未就绪, 请检查来源设备"
        case .wechatWindowTimeout: return "等待微信会话超时, 请检查来源微信界面"
        case .failed: return "来源设备发送失败"
        }
    }
}
