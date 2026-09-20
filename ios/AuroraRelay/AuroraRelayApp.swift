import RelayCore
import SwiftUI
import UserNotifications

@main
struct AuroraRelayApp: App {
    @UIApplicationDelegateAdaptor(RelayAppDelegate.self) private var appDelegate
    @StateObject private var model = RelayAppModel.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            Group {
                if model.session == nil { WelcomeView() }
                else if model.pairing != nil { PairingView() }
                else { RelayTabs() }
            }
            .environmentObject(model)
            .tint(Color.relayGreen)
            .overlay {
                if scenePhase != .active {
                    Color(uiColor: .systemBackground).ignoresSafeArea()
                        .overlay { Image(systemName: "bubble.left.and.bubble.right").font(.system(size: 42)).foregroundStyle(Color.relayGreen) }
                }
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { model.start() } else { model.stop() }
            }
            .task { model.start() }
        }
    }
}

struct RelayTabs: View {
    @EnvironmentObject private var model: RelayAppModel
    var body: some View {
        TabView(selection: $model.selectedTab) {
            NavigationStack(path: $model.conversationPath) { InboxView() }
                .tabItem { Label("消息", systemImage: "bubble.left.and.bubble.right") }.tag(0)
            NavigationStack { FriendsView() }
                .tabItem { Label("好友", systemImage: "person.2") }.tag(1)
            NavigationStack { SettingsView() }
                .tabItem { Label("设置", systemImage: "gearshape") }.tag(2)
        }
    }
}

extension Color {
    static let relayGreen = Color(red: 0.10, green: 0.50, blue: 0.35)
}

final class RelayAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let reply = UNTextInputNotificationAction(identifier: "REPLY", title: "回复", options: [.authenticationRequired], textInputButtonTitle: "发送", textInputPlaceholder: "输入回复")
        center.setNotificationCategories([UNNotificationCategory(identifier: "RELAY_MESSAGE", actions: [reply], intentIdentifiers: [], options: [])])
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in await RelayAppModel.shared.registerPushToken(token) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in RelayAppModel.shared.problem = "尚未注册系统推送. \(RelayAppModel.describe(error))" }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        Task { @MainActor in await RelayAppModel.shared.refresh() }
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        guard response.actionIdentifier != UNNotificationDismissActionIdentifier else { completionHandler(); return }
        Task { @MainActor in
            defer { completionHandler() }
            do {
                let reply = (response as? UNTextInputNotificationResponse)?.userText
                try await RelayAppModel.shared.openNotification(response.notification.request.content.userInfo, reply: reply)
            } catch {
                RelayAppModel.shared.problem = RelayAppModel.describe(error)
                if response is UNTextInputNotificationResponse {
                    let content = UNMutableNotificationContent()
                    content.title = "回复未提交"
                    content.body = "请打开 Relay 检查连接和消息状态."
                    try? await center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
                }
            }
        }
    }
}
