import RelayCore
import SwiftUI

struct DevicesView: View {
    @EnvironmentObject private var model: RelayAppModel
    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 18) {
                    Image(systemName: "iphone.gen3.radiowaves.left.and.right").font(.system(size: 38, weight: .light)).foregroundStyle(Color.relayGreen)
                    Text("工作微信").font(.title2.bold())
                    Label(model.device?.paired == true ? "Android 已配对" : "正在检查连接", systemImage: model.device?.paired == true ? "checkmark.circle.fill" : "ellipsis.circle").foregroundStyle(Color.relayGreen).font(.subheadline)
                    if let seen = model.device?.lastSeenAt {
                        LabeledContent("最近联系", value: Date(timeIntervalSince1970: Double(seen) / 1_000).formatted(date: .abbreviated, time: .shortened)).font(.footnote).foregroundStyle(.secondary)
                    }
                }.padding(.vertical, 16)
            }
            Section("连接状态") {
                statusRow("中继服务", value: model.lastSync == nil ? "尚未同步" : "最近同步成功", symbol: "network", ready: model.lastSync != nil && model.problem == nil)
                statusRow("系统推送", value: model.device?.pushConfigured == true ? "服务已配置" : "等待服务配置", symbol: "bell", ready: model.device?.pushConfigured == true)
                statusRow("iPhone 注册", value: model.device?.pushRegistered == true ? "已注册" : "尚未注册", symbol: "iphone", ready: model.device?.pushRegistered == true)
                NavigationLink { DiagnosticsView() } label: { Label("连接诊断", systemImage: "waveform.path.ecg") }
            }
        }.navigationTitle("设备与连接").refreshable { await model.refresh() }
    }
    private func statusRow(_ title: String, value: String, symbol: String, ready: Bool) -> some View {
        HStack { Label(title, systemImage: symbol); Spacer(); Text(value).font(.subheadline).foregroundStyle(ready ? Color.secondary : .orange) }
    }
}

struct DiagnosticsView: View {
    @EnvironmentObject private var model: RelayAppModel
    var body: some View {
        List {
            Section("当前连接") {
                LabeledContent("服务", value: model.session?.origin.host ?? "未连接")
                LabeledContent("设备配对", value: model.device?.paired == true ? "已配对" : "未确认")
                LabeledContent("最近同步", value: model.lastSync?.formatted(date: .abbreviated, time: .standard) ?? "尚未同步")
                LabeledContent("APNs 配置", value: model.device?.pushConfigured == true ? "已配置" : "未配置")
                LabeledContent("推送注册", value: model.device?.pushRegistered == true ? "已注册" : "未注册")
            }
            if let problem = model.problem { Section("需要处理") { Text(problem).foregroundStyle(.orange) } }
            Section {
                Button { Task { await model.refreshNotificationPermission(); await model.refresh() } } label: {
                    HStack { Text("重新检查"); Spacer(); if model.isRefreshing { ProgressView() } }
                }.disabled(model.isRefreshing)
            }
            Section("收不到消息时") {
                Label("确认 Android 中继仍在运行, 并且能访问服务.", systemImage: "1.circle")
                Label("在 Android 上检查通知读取权限、无障碍和电池限制.", systemImage: "2.circle")
                Label("检查转发时间段, 暂停期间的新消息不会补发.", systemImage: "3.circle")
                Label("在 iPhone 系统设置中允许通知, 并检查专注模式.", systemImage: "4.circle")
            }.font(.subheadline)
        }.navigationTitle("连接诊断").navigationBarTitleDisplayMode(.inline)
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: RelayAppModel
    @State private var clear = false
    @State private var disconnect = false
    @State private var error: String?
    var body: some View {
        List {
            Section {
                HStack(spacing: 16) {
                    Image(systemName: "bubble.left.and.bubble.right.fill").font(.system(size: 30)).foregroundStyle(Color.relayGreen)
                        .frame(width: 58, height: 58).background(Color.relayGreen.opacity(0.1), in: RoundedRectangle(cornerRadius: 17))
                    VStack(alignment: .leading, spacing: 5) { Text("Relay").font(.title2.bold()); Text("工作微信, 随身就好.").font(.subheadline).foregroundStyle(.secondary) }
                }.padding(.vertical, 8)
            }
            Section {
                NavigationLink { RelayPolicyView() } label: { Label("转发与时间段", systemImage: "clock") }
                NavigationLink { NotificationSettingsView() } label: { Label("通知与隐私", systemImage: "bell.badge") }
                NavigationLink { DevicesView() } label: { Label("设备与连接", systemImage: "iphone.gen3.radiowaves.left.and.right") }
            }
            Section {
                Button { clear = true } label: { Label("清空本机记录", systemImage: "trash") }
                NavigationLink { AboutView() } label: { Label("关于 Relay", systemImage: "info.circle") }
            }
            Section {
                Button("断开并重新配对", role: .destructive) { disconnect = true }.disabled(model.isRefreshing)
            } footer: { Text("只影响本机连接. Android 和微信中的记录不会被删除.") }
            if let error { Section { Text(error).foregroundStyle(.red) } }
        }.navigationTitle("设置")
            .confirmationDialog("清空本机记录?", isPresented: $clear, titleVisibility: .visible) {
                Button("清空本机记录", role: .destructive) { do { try model.clearHistory() } catch { self.error = RelayAppModel.describe(error) } }
            } message: { Text("删除本机消息、图片缓存和回复状态. 已提交的回复仍可能发送; 服务端已有记录不会被删除.") }
            .confirmationDialog("断开当前连接?", isPresented: $disconnect, titleVisibility: .visible) {
                Button("断开连接", role: .destructive) { Task { do { try await model.disablePushAndDisconnect() } catch { self.error = RelayAppModel.describe(error) } } }
            } message: { Text("本机密钥和记录将被删除, 下次需要重新配对. 正在处理的回复不会被撤回.") }
    }
}

struct RelayPolicyView: View {
    @EnvironmentObject private var model: RelayAppModel
    @Environment(\.dismiss) private var dismiss
    @State private var enabled = true
    @State private var scheduled = false
    @State private var days = Set([1, 2, 3, 4, 5])
    @State private var start = Self.date("09:30")
    @State private var end = Self.date("18:00")
    @State private var error: String?
    private let labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    var body: some View {
        Form {
            Section {
                Toggle("接收转发", isOn: $enabled)
            } footer: { Text("关闭或不在时间段内时, Android 不转发新消息. 之后恢复不会补发暂停期间的消息.") }
            Section {
                Toggle("仅在指定时间转发", isOn: $scheduled)
                if scheduled {
                    ForEach(1...7, id: \.self) { day in
                        Button { if days.contains(day) { days.remove(day) } else { days.insert(day) } } label: {
                            HStack { Text(labels[day - 1]).foregroundStyle(.primary); Spacer(); if days.contains(day) { Image(systemName: "checkmark") } }
                        }.accessibilityAddTraits(days.contains(day) ? [.isSelected] : [])
                    }
                    DatePicker("开始", selection: $start, displayedComponents: .hourAndMinute)
                    DatePicker("结束", selection: $end, displayedComponents: .hourAndMinute)
                }
            } header: { Text("每周时间段") } footer: { Text("按 Asia/Shanghai 时区执行. 开始时间需早于结束时间, 暂不跨午夜.") }
            if let error { Section { Text(error).foregroundStyle(.red) } }
        }
        .navigationTitle("转发设置").navigationBarTitleDisplayMode(.inline)
        .environment(\.timeZone, TimeZone(identifier: "Asia/Shanghai")!)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("保存") {
                    guard !days.isEmpty, Self.time(start) < Self.time(end) else { error = "请选择至少一天, 并让结束时间晚于开始时间."; return }
                    Task {
                        do {
                            let value = RelayPolicy(enabled: enabled, scheduleEnabled: scheduled, weekdays: days.sorted(), start: Self.time(start), end: Self.time(end), timezone: "Asia/Shanghai", active: model.policy.active, updatedAt: model.policy.updatedAt)
                            try await model.savePolicy(value); dismiss()
                        } catch { self.error = RelayAppModel.describe(error) }
                    }
                }.disabled(model.isSavingPolicy)
            }
        }
        .onAppear { enabled = model.policy.enabled; scheduled = model.policy.scheduleEnabled; days = Set(model.policy.weekdays); start = Self.date(model.policy.start); end = Self.date(model.policy.end) }
    }
    private static func formatter() -> DateFormatter {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.timeZone = TimeZone(identifier: "Asia/Shanghai"); formatter.dateFormat = "HH:mm"; return formatter
    }
    private static func date(_ time: String) -> Date { formatter().date(from: time) ?? Date() }
    private static func time(_ date: Date) -> String { formatter().string(from: date) }
}

struct NotificationSettingsView: View {
    @EnvironmentObject private var model: RelayAppModel
    @Environment(\.openURL) private var openURL
    @State private var error: String?
    var body: some View {
        Form {
            Section {
                LabeledContent("系统通知", value: permission)
                if model.notificationPermission == .notDetermined {
                    Button("允许通知") { Task { await model.requestNotifications() } }
                } else {
                    Button("打开系统通知设置") { if let url = URL(string: UIApplication.openNotificationSettingsURLString) { openURL(url) } }
                }
            } footer: { Text("锁屏提醒和快捷回复需要系统通知权限. 快捷回复要求先通过设备身份验证.") }
            Section {
                Toggle("在通知中显示消息内容", isOn: Binding(get: { model.previewEnabled }, set: { value in
                    Task { do { try await model.setPreviewEnabled(value) } catch { self.error = RelayAppModel.describe(error) } }
                }))
            } footer: { Text("默认只显示收到新消息. 开启后由本机解密发送者和内容, 锁屏是否展示仍遵循 iOS 的预览设置. 已送达的通知不会自动改写.") }
            Section("内容与存储") {
                Label("配对密钥保存在本机 Keychain", systemImage: "key.horizontal")
                Label("消息与本机历史加密保存", systemImage: "lock.shield")
                Text("语音只显示转写文本. 聊天记录卡片可能只有摘要; 完整合并转发内容请在微信中查看.").font(.footnote).foregroundStyle(.secondary)
            }
            if let error { Section { Text(error).foregroundStyle(.red) } }
        }.navigationTitle("通知与隐私").navigationBarTitleDisplayMode(.inline)
            .task { await model.refreshNotificationPermission() }
    }
    private var permission: String {
        switch model.notificationPermission {
        case .authorized: return "已允许"
        case .provisional: return "静默通知"
        case .denied: return "已关闭"
        case .ephemeral: return "临时允许"
        case .notDetermined: return "尚未请求"
        @unknown default: return "请检查系统设置"
        }
    }
}

struct AboutView: View {
    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Relay").font(.largeTitle.bold())
                    Text("把工作微信, 放进口袋.").foregroundStyle(.secondary)
                    Text("版本 \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.1.0")").font(.footnote).foregroundStyle(.secondary)
                }.padding(.vertical, 18)
            }
            Section("它怎样工作") {
                Text("Android 读取微信通知并加密转发. iPhone 从你的服务接收密文, 在本机解密.")
                Text("回复由 Android 调用微信可用的回复入口. 发送状态不代表微信服务端送达或对方已读.")
            }
            Section("当前支持") {
                Text("文本回复、图片查看与保存、语音转写、双微信标记、系统通知和每周转发时间段.")
                Text("尚不支持从 iPhone 发送图片、录音或主动新建微信会话.").foregroundStyle(.secondary)
            }
        }.navigationTitle("关于").navigationBarTitleDisplayMode(.inline)
    }
}
