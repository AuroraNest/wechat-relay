import RelayCore
import SwiftUI

struct WelcomeView: View {
    @EnvironmentObject private var model: RelayAppModel
    @State private var showConnection = false
    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 28) {
                Spacer()
                Image(systemName: "bubble.left.and.bubble.right.fill")
                    .font(.system(size: 56, weight: .medium)).foregroundStyle(Color.relayGreen)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 12) {
                    Text("工作微信,\n随身就好.").font(.system(size: 36, weight: .bold))
                    Text("\(model.sourceLabel)尚未连接.\n配对后在 iPhone 上收消息, 看图片, 回一句.")
                        .font(.title3).foregroundStyle(.secondary).lineSpacing(5)
                }
                Label("消息内容端到端加密", systemImage: "lock.shield").font(.subheadline).foregroundStyle(.secondary)
                Spacer()
                if let problem = model.problem { Text(problem).font(.footnote).foregroundStyle(.red) }
                Button { showConnection = true } label: {
                    Text("连接我的设备").font(.headline).frame(maxWidth: .infinity).padding(.vertical, 8)
                }.buttonStyle(.borderedProminent).buttonBorderShape(.capsule)
                #if DEBUG
                Button("浏览界面演示") { model.enterDemo() }.frame(maxWidth: .infinity).disabled(!model.canEnterDemo)
                #endif
                Text("每个来源独立配对. 可从顶部切回已连接的来源.").font(.footnote).foregroundStyle(.secondary).frame(maxWidth: .infinity)
            }.padding(28)
                .sheet(isPresented: $showConnection) { ConnectionView() }
        }
    }
}

struct ConnectionView: View {
    @EnvironmentObject private var model: RelayAppModel
    @Environment(\.dismiss) private var dismiss
    @State private var origin = ""
    @State private var token = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://relay.example.com", text: $origin)
                        .keyboardType(.URL).textContentType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("接入令牌", text: $token).textInputAutocapitalization(.never).autocorrectionDisabled()
                } header: { Text("你的中继服务") } footer: {
                    Text("填服务根地址和管理员提供的接入令牌. 令牌仅用于创建本次配对.")
                }
                Section {
                    Label("准备好\(model.sourceLabel)", systemImage: "iphone.and.arrow.forward")
                    Text("下一步会生成配对码. 小米手机在 Relay App 中粘贴; 平板来源由已配置的平板接入端导入配对码.").foregroundStyle(.secondary)
                }
                Section {
                    Text("当前来源会建立独立配对, 其他来源不受影响. 来源端切换配对后, 原 PWA 不再接收新消息; 网页历史不会迁入本机.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if let error { Section { Text(error).foregroundStyle(.red) } }
                Section {
                    Button {
                        busy = true; error = nil
                        Task {
                            do { try await model.connect(origin: origin, token: token); token = ""; dismiss() }
                            catch { self.error = RelayAppModel.describe(error) }
                            busy = false
                        }
                    } label: {
                        HStack { Text("生成配对码"); Spacer(); if busy { ProgressView() } }
                    }.disabled(origin.isEmpty || token.isEmpty || busy)
                }
            }
            .navigationTitle("连接服务").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() }.disabled(busy) } }
            .interactiveDismissDisabled(busy)
        }
    }
}

struct PairingView: View {
    @EnvironmentObject private var model: RelayAppModel
    @State private var copied = false
    @State private var cancel = false
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    Image(systemName: "iphone.gen3.radiowaves.left.and.right").font(.system(size: 52)).foregroundStyle(Color.relayGreen).padding(.top, 30)
                    Text("现在, 连上\(model.sourceLabel).").font(.largeTitle.bold())
                    Text("复制配对码, 在对应来源的 Relay App 或平板接入端导入. 两个来源不能共用同一配对码.").foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 14) {
                        Label("配对码包含密钥, 请勿分享给他人", systemImage: "key.horizontal")
                        TimelineView(.periodic(from: .now, by: 1)) { context in
                            let expiration = Date(timeIntervalSince1970: Double(model.pairing?.expiresAt ?? 0) / 1_000)
                            if expiration > context.date {
                                Text("有效期至 \(expiration.formatted(date: .omitted, time: .shortened))").foregroundStyle(.secondary)
                                Button(copied ? "已复制" : "复制配对码") {
                                    do {
                                        guard let pairing = model.pairing else { return }
                                        UIPasteboard.general.setItems([[UIPasteboard.typeAutomatic: try RelayCrypto.makePairingCode(pairing)]], options: [.localOnly: true, .expirationDate: expiration])
                                        copied = true
                                    } catch { model.problem = RelayAppModel.describe(error) }
                                }.buttonStyle(.borderedProminent).buttonBorderShape(.capsule)
                            } else {
                                Text("配对码已过期. 请返回重新连接.").foregroundStyle(.orange)
                            }
                        }
                    }.font(.subheadline).padding(20).frame(maxWidth: .infinity, alignment: .leading).background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 20))
                    HStack { ProgressView(); Text("等待\(model.sourceLabel)完成配对...").foregroundStyle(.secondary) }
                    if let problem = model.problem { Text(problem).font(.footnote).foregroundStyle(.red) }
                    Button("开启系统通知") { Task { await model.requestNotifications() } }.buttonStyle(.bordered)
                    Text("可以稍后开启通知. App 在前台时仍可同步消息.").font(.footnote).foregroundStyle(.secondary)
                }.padding(24)
            }.background(Color(uiColor: .systemGroupedBackground))
                .navigationTitle("配对设备").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("取消") { cancel = true } } }
                .confirmationDialog("取消本机连接?", isPresented: $cancel, titleVisibility: .visible) {
                    Button("取消连接", role: .destructive) {
                        do { try model.disconnect() } catch { model.problem = RelayAppModel.describe(error) }
                    }
                } message: { Text("已生成的配对码将自然过期. 如果来源端已使用它, 请在该来源端重新配对.") }
        }
    }
}
