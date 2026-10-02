import Combine
import RelayCore
import SwiftUI
import UserNotifications

@MainActor
final class RelayAppModel: ObservableObject {
    static let shared = RelayAppModel()
    @Published private(set) var selectedSource: RelaySourceSelection
    @Published var selectedTab = 0
    @Published var conversationPath: [String] = []
    @Published private var localProblem: String?
    private let connections: [RelaySource: RelayConnectionModel]
    private var observations = Set<AnyCancellable>()
    private var foreground = false
    private var pushUpdate: Task<Void, Never>?
    private var pushRetry: Task<Void, Never>?
    private var pushRevision = 0

    private init() {
        #if DEBUG
        let demo = ProcessInfo.processInfo.arguments.contains("--demo")
        #else
        let demo = false
        #endif
        selectedSource = demo ? .tablet : .restored(UserDefaults.standard.string(forKey: "relay-source"))
        connections = Dictionary(uniqueKeysWithValues: RelaySource.allCases.map { ($0, RelayConnectionModel(source: $0, demo: demo)) })
        for connection in connections.values {
            connection.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &observations)
        }
    }

    private var visible: [RelayConnectionModel] { selectedSource.sources.compactMap { connections[$0] } }
    // Mixed management is explicit in Settings; source-specific forms are only opened in a single-source view.
    private var current: RelayConnectionModel { connections[selectedSource == .phone ? .phone : .tablet]! }
    var sourceLabel: String { selectedSource.label }
    var session: RelaySession? { current.session }
    var pairing: RelayPairing? { current.pairing }
    var device: RelayDeviceStatus? { current.device }
    var policy: RelayPolicy { current.policy }
    var previewEnabled: Bool { current.previewEnabled }
    var notificationPermission: UNAuthorizationStatus { current.notificationPermission }
    var lastSync: Date? { visible.compactMap(\.lastSync).min() }
    var isRefreshing: Bool { visible.contains(where: \.isRefreshing) }
    var isSavingPolicy: Bool { current.isSavingPolicy }
    var hasMore: Bool { visible.contains(where: \.hasMore) }
    var isDemo: Bool { current.isDemo }
    var items: [InboxItem] { visible.flatMap(\.items).sorted { $0.message.createdAt < $1.message.createdAt } }
    var outgoing: [OutgoingMessage] { visible.flatMap(\.outgoing).sorted { $0.request.createdAt < $1.request.createdAt } }
    var conversations: [Conversation] { visible.flatMap(\.conversations).sorted { $0.createdAt > $1.createdAt } }
    var friends: [RelayFriend] { visible.flatMap(\.friends).sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending } }
    var friendsCapturedAt: Date? { visible.compactMap(\.friendsCapturedAt).min() }
    var isRefreshingContacts: Bool { visible.contains(where: \.isRefreshingContacts) }
    var contactsProblem: String? { visible.compactMap { connection in connection.contactsProblem.map { "\(connection.source.label): \($0)" } }.first }
    var contactsAvailable: Bool? {
        if visible.contains(where: { $0.contactsAvailable == true }) { return true }
        return visible.allSatisfy { $0.contactsAvailable == false } ? false : nil
    }
    var problem: String? {
        get { localProblem ?? visible.compactMap { connection in connection.problem.map { "\(connection.source.label): \($0)" } }.first }
        set { localProblem = newValue }
    }
    var stateTitle: String {
        if isDemo { return "演示模式 · 虚构数据" }
        if selectedSource != .mixed { return current.stateTitle }
        return visible.map { "\($0.source.label): \($0.stateTitle)" }.joined(separator: " / ")
    }

    func selectSource(_ source: RelaySourceSelection) {
        guard source != selectedSource else { return }
        selectedSource = source
        if !isDemo { UserDefaults.standard.set(source.rawValue, forKey: "relay-source") }
        conversationPath = []
        localProblem = nil
        for connection in connections.values { connection.stop() }
        if foreground { visible.forEach { $0.start() } }
        schedulePushUpdate()
    }

    func start() {
        foreground = true
        visible.forEach { $0.start() }
        schedulePushUpdate()
        // Hidden connections do not poll messages, but a failed push unregister must recover too.
        if pushRetry == nil {
            pushRetry = Task { [weak self] in
                while !Task.isCancelled {
                    do { try await Task.sleep(for: .seconds(30)) } catch { break }
                    guard let self, self.foreground else { break }
                    self.schedulePushUpdate()
                }
            }
        }
    }
    func stop() {
        foreground = false
        pushRetry?.cancel()
        pushRetry = nil
        connections.values.forEach { $0.stop() }
    }
    private func schedulePushUpdate() {
        if !isDemo {
            do {
                try RelayKeychain.save(selectedSource.rawValue, account: "source-selection", service: RelayConnectionModel.keychainService, accessGroup: RelayConnectionModel.keychainGroup)
            } catch { localProblem = Self.describe(error) }
        }
        pushRevision += 1
        guard pushUpdate == nil else { return }
        pushUpdate = Task { [weak self] in
            guard let self else { return }
            repeat {
                let revision = self.pushRevision
                for source in RelaySource.allCases {
                    await self.connections[source]?.setPushSelected(self.selectedSource.sources.contains(source))
                }
                if revision == self.pushRevision { break }
            } while !Task.isCancelled
            self.pushUpdate = nil
        }
    }
    func refresh() async { for connection in visible { await connection.refresh() }; schedulePushUpdate() }
    func refreshContacts() async { for connection in visible { await connection.refreshContacts() } }
    func loadOlder() async { for connection in visible { await connection.loadOlder() } }
    func connect(origin: String, token: String) async throws {
        guard selectedSource != .mixed else { throw RelayError.invalidValue("select pairing source") }
        let connection = current
        try await connection.connect(origin: origin, token: token)
        schedulePushUpdate()
    }
    func disconnect() throws { try current.disconnect(); conversationPath = []; if foreground { current.start() } }
    func disablePushAndDisconnect() async throws { try await current.disablePushAndDisconnect(); conversationPath = []; if foreground { current.start() } }
    func clearHistory() throws { for connection in visible where connection.session != nil { try connection.clearHistory() } }
    func savePolicy(_ value: RelayPolicy) async throws { try await current.savePolicy(value) }
    func setPreviewEnabled(_ enabled: Bool) async throws { try await current.setPreviewEnabled(enabled) }
    func refreshNotificationPermission() async { for connection in visible { await connection.refreshNotificationPermission() } }
    func requestNotifications() async { await current.requestNotifications() }
    func registerPushToken(_ token: String) async {
        for connection in connections.values { await connection.registerPushToken(token) }
        schedulePushUpdate()
    }
    func canSend(to friend: RelayFriend) -> Bool { connections[friend.source]?.canSend(to: friend) == true }
    func avatarItem(for friend: RelayFriend) -> InboxItem? { connections[friend.source]?.avatarItem(for: friend) }
    func markRead(_ conversationID: String) { let id = resolvedConversationID(conversationID); connection(for: id)?.markRead(id) }
    func send(_ body: String, to item: InboxItem) async throws {
        guard let connection = connections[item.source], connection.session?.pairId == item.pairID else { throw RelayError.invalidValue("message source") }
        try await connection.send(body, to: item)
    }
    func send(_ body: String, to friend: RelayFriend) async throws {
        guard let connection = connections[friend.source], connection.session?.pairId == friend.pairID else { throw RelayError.invalidValue("contact source") }
        try await connection.send(body, to: friend)
    }
    func retry(_ reply: OutgoingMessage) async { await connection(for: reply.conversationID)?.retry(reply) }
    func image(for metadata: RelayAssetMetadata, in item: InboxItem) async throws -> UIImage {
        guard let connection = connections[item.source], connection.session?.pairId == item.pairID else { throw RelayError.invalidValue("asset source") }
        return try await connection.image(for: metadata, in: item.message)
    }
    func resolvedConversationID(_ id: String) -> String { items.first(where: { $0.pendingConversationID == id })?.conversationID ?? id }
    func loadNativeContent(for item: InboxItem) async throws -> RelayNativeContent {
        guard let connection = connections[item.source], connection.session?.pairId == item.pairID else { throw RelayError.invalidValue("content source") }
        return try await connection.loadNativeContent(for: item)
    }
    func nativeContentProblem(for item: InboxItem) -> String? { connections[item.source]?.nativeContentProblems[item.id] }
    func nativeLastSync(for item: InboxItem) -> Date? { connections[item.source]?.lastSync }
    func nativeAssetURL(_ metadata: RelayNativeAssetMetadata, in item: InboxItem) async throws -> URL {
        guard let connection = connections[item.source], connection.session?.pairId == item.pairID else { throw RelayError.invalidValue("asset source") }
        return try await connection.nativeAssetURL(metadata, in: item)
    }
    private func connection(for conversationID: String) -> RelayConnectionModel? {
        connections.values.first { connection in connection.session.map { connection.source.owns(conversationID, pairID: $0.pairId) } == true }
    }
    private func source(for userInfo: [AnyHashable: Any]) -> RelaySource? {
        guard let pairID = userInfo["pairId"] as? String else { return nil }
        return RelaySessionRouting.source(pairID: pairID, sessions: connections.compactMapValues(\.session))
    }
    func acceptsNotification(_ userInfo: [AnyHashable: Any]) -> Bool {
        source(for: userInfo).map { selectedSource.sources.contains($0) } == true
    }
    func openNotification(_ userInfo: [AnyHashable: Any], reply: String? = nil) async throws {
        guard let source = source(for: userInfo), let connection = connections[source] else { throw RelayError.invalidResponse }
        // A previously delivered notification may refer to an inactive source. Keep its original pair route.
        if reply == nil, !selectedSource.sources.contains(source) { selectSource(source == .tablet ? .tablet : .phone) }
        try await connection.openNotification(userInfo, reply: reply)
        if reply == nil { selectedTab = 0; conversationPath = connection.conversationPath }
    }
    #if DEBUG
    var canEnterDemo: Bool { connections.values.allSatisfy { $0.session == nil } }
    func enterDemo() {
        guard canEnterDemo else { return }
        connections.values.forEach { $0.enterDemo() }
    }
    #endif
    static func describe(_ error: Error) -> String { RelayConnectionModel.describe(error) }
}
