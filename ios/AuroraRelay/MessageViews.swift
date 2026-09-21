import Foundation
import Photos
import RelayCore
import SwiftUI

struct InboxView: View {
    @EnvironmentObject private var model: RelayAppModel
    @State private var search = ""
    private var conversations: [Conversation] {
        model.conversations.filter { search.isEmpty || $0.friend.name.localizedCaseInsensitiveContains(search) || $0.body.localizedCaseInsensitiveContains(search) }
    }
    var body: some View {
        List {
            Section {
                HStack(spacing: 7) {
                    Circle().fill(model.problem == nil ? Color.relayGreen : .orange).frame(width: 6, height: 6)
                    Text(model.stateTitle).font(.footnote).foregroundStyle(.secondary)
                    Spacer()
                    if model.isRefreshing { ProgressView().controlSize(.mini) }
                }.listRowSeparator(.hidden).listRowInsets(EdgeInsets(top: 0, leading: 22, bottom: 12, trailing: 22))
                if let problem = model.problem {
                    NavigationLink { DiagnosticsView() } label: { Label(problem, systemImage: "wifi.exclamationmark").font(.footnote).foregroundStyle(.orange) }
                }
                ForEach(conversations) { conversation in
                    NavigationLink(value: conversation.id) {
                        HStack(spacing: 14) {
                            FriendAvatarView(friend: conversation.friend)
                            VStack(alignment: .leading, spacing: 7) {
                                HStack(alignment: .firstTextBaseline) {
                                    Text(conversation.friend.name).font(.system(size: 17, weight: .semibold)).lineLimit(1)
                                    Spacer(minLength: 10)
                                    Text(Date(timeIntervalSince1970: Double(conversation.createdAt) / 1_000), style: .time).font(.caption2).foregroundStyle(.tertiary)
                                }
                                HStack {
                                    Text(conversation.body).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                                    Spacer(minLength: 5)
                                    if conversation.unread > 0 { Text(conversation.unread > 99 ? "99+" : "\(conversation.unread)").font(.caption2.weight(.semibold)).foregroundStyle(.white).padding(.horizontal, 5).padding(.vertical, 2).background(Color.relayGreen, in: Capsule()) }
                                }
                            }
                        }.padding(.vertical, 11)
                    }.listRowSeparator(.visible).listRowInsets(EdgeInsets(top: 0, leading: 22, bottom: 0, trailing: 14))
                }
                if model.hasMore {
                    Button("加载更早的消息") { Task { await model.loadOlder() } }.frame(maxWidth: .infinity).font(.footnote)
                }
            }
        }
        .listStyle(.plain)
        .overlay {
            if conversations.isEmpty {
                ContentUnavailableView(search.isEmpty ? "消息会来到这里" : "没有找到会话", systemImage: "bubble.left.and.bubble.right", description: Text(search.isEmpty ? "Android 收到新的微信消息后, 会自动同步过来." : "试试其他名字或消息内容."))
            }
        }
        .navigationTitle("消息")
        .toolbar { ToolbarItem(placement: .topBarTrailing) { Text("工作微信").font(.subheadline).foregroundStyle(.secondary) } }
        .searchable(text: $search, prompt: "搜索会话")
        .refreshable { await model.refresh() }
        .navigationDestination(for: String.self) { id in ConversationView(conversationID: id) }
    }
}

struct AvatarView: View {
    @EnvironmentObject private var model: RelayAppModel
    let item: InboxItem
    @State private var image: UIImage?
    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            ZStack {
                RoundedRectangle(cornerRadius: 16).fill(Color.relayGreen.opacity(item.message.wechatUserId == 999 ? 0.07 : 0.12))
                if let image { Image(uiImage: image).resizable().scaledToFill() }
                else { Text(String(item.preview.sender.prefix(1))).font(.system(size: 23, weight: .medium)).foregroundStyle(Color.relayGreen) }
            }.frame(width: 52, height: 52).clipShape(RoundedRectangle(cornerRadius: 16))
            if item.message.wechatUserId == 999 { Text("2").font(.system(size: 9, weight: .bold)).foregroundStyle(.white).padding(4).background(Color.relayGreen, in: Circle()).offset(x: 3, y: 3) }
        }.accessibilityHidden(true)
            .task(id: item.id) {
                if let metadata = item.message.assets.first(where: { $0.kind == .avatar }) { image = try? await model.image(for: metadata, in: item.message) }
            }
    }
}

struct FriendsView: View {
    @EnvironmentObject private var model: RelayAppModel
    @State private var search = ""

    private var filteredFriends: [RelayFriend] {
        model.friends.filter { search.isEmpty || $0.name.localizedCaseInsensitiveContains(search) }
    }

    private var sections: [(initial: String, friends: [RelayFriend])] {
        Dictionary(grouping: filteredFriends) { friend in
            indexInitial(friend.name)
        }.map { initial, friends in
            (initial, friends.sorted {
                let comparison = $0.name.localizedStandardCompare($1.name)
                return comparison == .orderedSame ? $0.wechatUserId < $1.wechatUserId : comparison == .orderedAscending
            })
        }.sorted {
            if $0.initial == "#" { return false }
            if $1.initial == "#" { return true }
            return $0.initial.localizedStandardCompare($1.initial) == .orderedAscending
        }
    }

    var body: some View {
        List {
            Section {
                if let capturedAt = model.friendsCapturedAt {
                    Label("已同步 \(model.friends.count) 位好友 · \(capturedAt.formatted(date: .abbreviated, time: .shortened))", systemImage: "person.2")
                        .font(.footnote).foregroundStyle(.secondary)
                } else {
                    Label("首次同步请在小米 Relay 点击同步好友.", systemImage: "arrow.triangle.2.circlepath")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Text("好友名单来自已同步的微信通讯录.")
                    .font(.footnote).foregroundStyle(.secondary)
                if model.isRefreshingContacts { ProgressView("正在同步好友") }
            }
            if let problem = model.contactsProblem {
                Section { Label(problem, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.orange) }
            }
            ForEach(sections, id: \.initial) { section in
                Section(section.initial) {
                    ForEach(section.friends) { friend in
                        NavigationLink { ConversationView(conversationID: friend.id) } label: {
                            HStack(spacing: 13) {
                                FriendAvatarView(friend: friend)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(friend.name).font(.body)
                                    if model.friends.filter({ $0.name == friend.name }).count > 1 {
                                        Text(friend.profileLabel).font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }.padding(.vertical, 4)
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("好友")
        .searchable(text: $search, prompt: "搜索好友")
        .overlay {
            if filteredFriends.isEmpty {
                ContentUnavailableView(emptyTitle, systemImage: "person.2", description: Text(emptyDescription))
            }
        }
        .task { await model.refreshContacts() }
        .refreshable { await model.refreshContacts() }
    }

    private var emptyTitle: String {
        if !search.isEmpty { return "没有找到好友" }
        if model.contactsAvailable == false { return "服务尚未支持好友同步" }
        return "好友会来到这里"
    }

    private var emptyDescription: String {
        if !search.isEmpty { return "试试其他名字." }
        if model.contactsAvailable == false { return "升级 Relay 服务后, 可同步微信通讯录好友名单." }
        return "首次同步请在小米 Relay 点击同步好友."
    }

    private func indexInitial(_ name: String) -> String {
        let latin = name.applyingTransform(.toLatin, reverse: false)?.folding(options: .diacriticInsensitive, locale: .current) ?? name
        guard let scalar = latin.trimmingCharacters(in: .whitespacesAndNewlines).uppercased(with: .current).unicodeScalars.first,
              (65...90).contains(scalar.value) else { return "#" }
        return String(scalar)
    }
}

struct FriendAvatarView: View {
    @EnvironmentObject private var model: RelayAppModel
    let friend: RelayFriend
    @State private var image: UIImage?

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            ZStack {
                RoundedRectangle(cornerRadius: 14).fill(Color.relayGreen.opacity(friend.wechatUserId == 999 ? 0.07 : 0.12))
                if let image { Image(uiImage: image).resizable().scaledToFill() }
                else { Text(String(friend.name.prefix(1))).font(.system(size: 20, weight: .medium)).foregroundStyle(Color.relayGreen) }
            }.frame(width: 46, height: 46).clipShape(RoundedRectangle(cornerRadius: 14))
            if friend.wechatUserId == 999 { Text("2").font(.system(size: 9, weight: .bold)).foregroundStyle(.white).padding(4).background(Color.relayGreen, in: Circle()).offset(x: 3, y: 3) }
        }
        .accessibilityHidden(true)
        .task(id: friend.id) {
            image = nil
            guard let source = model.avatarItem(for: friend),
                  let metadata = source.message.assets.first(where: { $0.kind == .avatar }) else { return }
            image = try? await model.image(for: metadata, in: source.message)
        }
    }
}

struct ConversationView: View {
    @EnvironmentObject private var model: RelayAppModel
    let conversationID: String
    @State private var draft = ""
    @State private var sending = false
    @State private var error: String?
    @FocusState private var inputFocused: Bool
    private var messages: [InboxItem] { model.items.filter { $0.conversationID == conversationID } }
    private var target: InboxItem? { messages.last(where: { $0.message.replyCapable }) }
    private var friend: RelayFriend? { model.friends.first { $0.id == conversationID } }
    private var canSend: Bool { target != nil || friend.map { model.canSend(to: $0) } == true }
    private var replies: [OutgoingMessage] { model.outgoing.filter { $0.conversationID == conversationID } }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 18) {
                    Text(model.isDemo ? "演示数据" : "消息内容端到端加密").font(.caption2).foregroundStyle(.tertiary).padding(.top, 18)
                    if model.hasMore { Button("加载更早的消息") { Task { await model.loadOlder() } }.font(.footnote) }
                    ForEach(timeline, id: \.id) { entry in
                        if let item = entry.incoming {
                            HStack(alignment: .top, spacing: 10) {
                                AvatarView(item: item).scaleEffect(0.75).frame(width: 40, height: 40)
                                VStack(alignment: .leading, spacing: 7) {
                                    Text(Date(timeIntervalSince1970: Double(item.message.createdAt) / 1_000), format: .dateTime.month().day().hour().minute()).font(.caption2).foregroundStyle(.secondary)
                                    VStack(alignment: .leading, spacing: 10) {
                                        Text(item.preview.body).textSelection(.enabled).font(.body).lineSpacing(4)
                                        ForEach(item.message.assets.filter { $0.kind != .avatar }) { asset in
                                            MessageImageView(metadata: asset, message: item.message)
                                        }
                                    }.padding(14).background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18))
                                }
                                Spacer(minLength: 30)
                            }.id(entry.id)
                        } else if let reply = entry.outgoing {
                            HStack {
                                Spacer(minLength: 56)
                                VStack(alignment: .trailing, spacing: 7) {
                                    Text(reply.body).textSelection(.enabled).padding(14).background(Color.relayGreen.opacity(0.13), in: RoundedRectangle(cornerRadius: 18))
                                    Button { Task { await model.retry(reply) } } label: {
                                        Text(reply.request.v == 4 && reply.status == .remoteInputUnsupported ? "小米自动化不可用, 请检查 Relay 授权与保护状态" : reply.status.label).font(.caption2).foregroundStyle(reply.status == .sentToWechat ? Color.secondary : Color.relayGreen)
                                    }.disabled(reply.status != .submitting)
                                }
                            }.id(entry.id)
                        }
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }.padding(.horizontal, 18)
            }
            .background(Color(uiColor: .systemGroupedBackground))
            .scrollDismissesKeyboard(.interactively)
            .onAppear { proxy.scrollTo("bottom", anchor: .bottom); model.markRead(conversationID) }
            .onChange(of: timeline.count) { _, _ in
                withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                model.markRead(conversationID)
            }
            .onChange(of: inputFocused) { _, focused in if focused { withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } } }
        }
        .navigationTitle(messages.last?.preview.sender ?? friend?.name ?? model.conversations.first(where: { $0.id == conversationID })?.friend.name ?? "会话").navigationBarTitleDisplayMode(.inline)
        .toolbar(.hidden, for: .tabBar)
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 8) {
                if let error { Text(error).font(.caption).foregroundStyle(.red) }
                if let problem = model.problem { Text(problem).font(.caption).foregroundStyle(.orange).lineLimit(3) }
                HStack(alignment: .bottom, spacing: 10) {
                    TextField(canSend ? "输入回复" : "暂时无法发送", text: $draft, axis: .vertical)
                        .lineLimit(1...5).focused($inputFocused).padding(.horizontal, 15).padding(.vertical, 11)
                        .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 22))
                        .disabled(!canSend)
                    Button {
                        guard canSend else { return }
                        sending = true; error = nil
                        Task {
                            do {
                                if let target { try await model.send(draft, to: target) }
                                else if let friend { try await model.send(draft, to: friend) }
                                draft = ""
                            }
                            catch { self.error = RelayAppModel.describe(error) }
                            sending = false
                        }
                    } label: { Image(systemName: "arrow.up").font(.system(size: 18, weight: .semibold)).frame(width: 44, height: 44).foregroundStyle(.white).background(Color.relayGreen, in: Circle()) }
                        .disabled(!canSend || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || draft.count > 1_000 || sending)
                        .opacity(!canSend || draft.isEmpty ? 0.4 : 1).accessibilityLabel("发送回复")
                }
                if !canSend && friend != nil {
                    Text("请更新小米 Relay 并重新同步一次好友, 即可主动发送消息.").font(.caption).foregroundStyle(.secondary)
                }
                if draft.count > 1_000 { Text("回复最多 1000 字").font(.caption).foregroundStyle(.red) }
                Text("已发往微信表示 Android 已执行发送, 不代表对方已读.").font(.caption2).foregroundStyle(.secondary)
            }.padding(.horizontal, 16).padding(.vertical, 10).background(.bar)
        }
    }

    private struct Entry {
        let id: UUID
        let time: Int64
        let incoming: InboxItem?
        let outgoing: OutgoingMessage?
    }
    private var timeline: [Entry] {
        (messages.map { Entry(id: $0.id, time: $0.message.createdAt, incoming: $0, outgoing: nil) } + replies.map { Entry(id: $0.id, time: $0.request.createdAt, incoming: nil, outgoing: $0) }).sorted { $0.time < $1.time }
    }
}

struct MessageImageView: View {
    @EnvironmentObject private var model: RelayAppModel
    let metadata: RelayAssetMetadata
    let message: RelayMessage
    @State private var image: UIImage?
    @State private var failed = false
    @State private var attempt = 0
    @State private var expanded = false
    var body: some View {
        Group {
            if let image {
                Button { expanded = true } label: {
                    Image(uiImage: image).resizable().scaledToFit().frame(maxWidth: 240, maxHeight: 260).clipShape(RoundedRectangle(cornerRadius: 12))
                }.accessibilityLabel("查看图片")
            } else if failed {
                Button("图片未加载, 点按重试") { failed = false; attempt += 1 }.font(.footnote)
            } else { ProgressView().frame(width: 170, height: 100) }
        }
        .task(id: attempt) {
            do { image = try await model.image(for: metadata, in: message) }
            catch { failed = true }
        }
        .fullScreenCover(isPresented: $expanded) { if let image { ImageViewer(image: image) } }
    }
}

struct ImageViewer: View {
    @Environment(\.dismiss) private var dismiss
    let image: UIImage
    @State private var scale: CGFloat = 1
    @State private var lastScale: CGFloat = 1
    @State private var sharing = false
    @State private var notice: String?
    var body: some View {
        NavigationStack {
            Image(uiImage: image).resizable().scaledToFit().scaleEffect(scale)
                .frame(maxWidth: .infinity, maxHeight: .infinity).background(.black)
                .gesture(MagnificationGesture().onChanged { scale = min(4, max(1, lastScale * $0)) }.onEnded { _ in lastScale = scale })
                .onTapGesture(count: 2) { withAnimation { scale = scale == 1 ? 2 : 1; lastScale = scale } }
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) { Button("完成") { dismiss() } }
                    ToolbarItemGroup(placement: .topBarTrailing) {
                        Button { sharing = true } label: { Image(systemName: "square.and.arrow.up") }.accessibilityLabel("分享图片")
                        Button { Task { await saveImage() } } label: { Image(systemName: "square.and.arrow.down") }.accessibilityLabel("保存图片")
                    }
                }
                .toolbarBackground(.black, for: .navigationBar).preferredColorScheme(.dark)
                .sheet(isPresented: $sharing) { ShareSheet(image: image) }
                .alert("图片", isPresented: Binding(get: { notice != nil }, set: { if !$0 { notice = nil } })) { Button("好") { notice = nil } } message: { Text(notice ?? "") }
        }
    }
    private func saveImage() async {
        let permission = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard permission == .authorized || permission == .limited else { notice = "请在系统设置中允许 Relay 添加照片."; return }
        do {
            try await PHPhotoLibrary.shared().performChanges { PHAssetChangeRequest.creationRequestForAsset(from: image) }
            notice = "已保存到照片."
        } catch { notice = "保存失败, 请重试." }
    }
}

private struct ShareSheet: UIViewControllerRepresentable {
    let image: UIImage
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: [image], applicationActivities: nil) }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
