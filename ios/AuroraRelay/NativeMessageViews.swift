import AVKit
import Combine
import QuickLook
import RelayCore
import SwiftUI

struct NativeContentView: View {
    @EnvironmentObject private var model: RelayAppModel
    let item: InboxItem
    @State private var busy = false
    @State private var failure: String?
    @State private var attempt = 0
    @State private var details = false
    @State private var recordsPresented = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let content = item.nativeContent {
                if !content.text.isEmpty && ([.text, .reference, .link, .unsupported].contains(content.kind) ||
                    (content.kind != .record && !item.message.nativeAssets.contains(where: { $0.role == .original }))) {
                    Text(verbatim: content.text).textSelection(.enabled).font(.body).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
                        .contextMenu { Button("详细信息", systemImage: "info.circle") { details = true } }
                }
                if content.kind == .record || !(content.records ?? []).isEmpty {
                    let records = content.records ?? []
                        VStack(alignment: .leading, spacing: 8) {
                            Text(content.kind == .record && !content.text.isEmpty ? content.text.components(separatedBy: .newlines).first ?? "聊天记录" : "聊天记录").font(.headline).lineLimit(2)
                            ForEach(Array(records.prefix(4).enumerated()), id: \.offset) { _, record in
                                Text((record.senderName.isEmpty || record.senderName.hasPrefix("wxid_") ? "" : record.senderName + ": ") + (record.text.isEmpty ? record.kind.label : record.text.replacingOccurrences(of: "\n", with: " "))).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                            }
                            if records.isEmpty { Text(content.text).font(.subheadline).foregroundStyle(.secondary).lineLimit(4) }
                            Divider()
                            Text(records.isEmpty ? "聊天记录" : "聊天记录 · \(records.count) 条").font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                        .onTapGesture { recordsPresented = true }
                        .accessibilityElement(children: .combine).accessibilityAddTraits(.isButton)
                        .accessibilityAction { recordsPresented = true }
                        .accessibilityIdentifier("native-record-summary")
                        .contextMenu { Button("详细信息", systemImage: "info.circle") { details = true } }
                }
                NativeAttachmentsView(content: content, recordIndex: nil, item: item)
                if content.text.isEmpty && content.kind != .record && (content.records ?? []).isEmpty &&
                    !item.message.nativeAssets.contains(where: { $0.role == .original }) {
                    Text(content.kind.label).foregroundStyle(.secondary)
                        .contextMenu { Button("详细信息", systemImage: "info.circle") { details = true } }
                }
            } else {
                Text(verbatim: item.preview.body).textSelection(.enabled)
                if busy { ProgressView("获取完整内容") }
                if let problem = failure ?? model.nativeContentProblem(for: item) { Text(problem).font(.caption).foregroundStyle(.orange) }
                Button("加载完整内容") { attempt += 1 }.disabled(busy)
            }
        }
        .sheet(isPresented: $details) {
            NativeMessageDetails(text: item.nativeContent?.text ?? item.preview.body, xml: item.nativeContent?.rawXML)
        }
        .sheet(isPresented: $recordsPresented) {
            if let content = item.nativeContent { NativeRecordsView(content: content, item: item) }
        }
        .task(id: attempt) {
            let refreshManifest = item.message.hasUnresolvedNativeSlots
            guard item.nativeContent == nil || refreshManifest else { return }
            busy = true
            defer { busy = false }
            do { _ = try await model.loadNativeContent(for: item, refreshManifest: refreshManifest); failure = nil }
            catch is CancellationError {}
            catch { failure = RelayAppModel.describe(error) }
        }
    }
}

private struct NativeMessageDetails: View {
    let text: String
    let xml: String?
    @Environment(\.dismiss) private var dismiss
    var body: some View {
                NavigationStack {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 20) {
                            if !text.isEmpty { Text(verbatim: text).textSelection(.enabled) }
                            if let xml, !xml.isEmpty { Text(verbatim: xml).font(.system(.footnote, design: .monospaced)).textSelection(.enabled) }
                        }.frame(maxWidth: .infinity, alignment: .leading).padding()
                    }
                        .navigationTitle("详细信息").navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } }
                            ToolbarItem(placement: .primaryAction) { ShareLink(item: [text, xml ?? ""].filter { !$0.isEmpty }.joined(separator: "\n\n")) { Label("分享原文", systemImage: "square.and.arrow.up") } }
                        }
                }
    }
}

private struct NativeRecordsView: View {
    let content: RelayNativeContent
    let item: InboxItem
    @Environment(\.dismiss) private var dismiss
    @State private var detailIndex: Int?

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    if (content.records ?? []).isEmpty {
                        Text(verbatim: content.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(Array((content.records ?? []).enumerated()), id: \.offset) { index, record in
                        VStack(alignment: .leading, spacing: 5) {
                            if !record.senderName.isEmpty && !record.senderName.hasPrefix("wxid_") { Text(record.senderName).font(.caption).foregroundStyle(.secondary) }
                            VStack(alignment: .leading, spacing: 8) {
                                if !record.text.isEmpty && ([.text, .reference, .link, .record, .unsupported].contains(record.kind) ||
                                    !content.attachments.contains(where: { $0.recordItemIndex == index })) {
                                    Text(verbatim: record.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                                        .contextMenu { Button("详细信息", systemImage: "info.circle") { detailIndex = index } }
                                }
                                NativeAttachmentsView(content: content, recordIndex: index, item: item)
                                if record.text.isEmpty && !content.attachments.contains(where: { $0.recordItemIndex == index }) {
                                    Text(record.kind.label).foregroundStyle(.secondary)
                                        .contextMenu { Button("详细信息", systemImage: "info.circle") { detailIndex = index } }
                                }
                            }.padding(12).background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(.trailing, 30)
                    }
                }.padding()
            }.background(Color(uiColor: .systemGroupedBackground))
            .navigationTitle("聊天记录").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
            .sheet(isPresented: Binding(get: { detailIndex != nil }, set: { if !$0 { detailIndex = nil } })) {
                if let index = detailIndex, let records = content.records, records.indices.contains(index) {
                    NativeMessageDetails(text: records[index].text, xml: records[index].rawXML)
                }
            }
        }
    }
}

private struct NativeAttachmentsView: View {
    let content: RelayNativeContent
    let recordIndex: Int?
    let item: InboxItem

    var body: some View {
        if item.message.nativeVersion == 8 {
            ForEach(item.message.nativeAssetSlots.filter { $0.role == .original }.filter { slot in
                content.attachments.first(where: { $0.assetId == slot.id })?.recordItemIndex == recordIndex
            }) { slot in
                let original = item.message.nativeAssets.first { $0.id == slot.id }
                let playback = item.message.nativeAssets.first { $0.derivedFrom == slot.id && $0.role == .playback }
                if let display = slot.kind == .image ? original ?? playback : playback ?? original {
                    NativeAttachmentView(metadata: display, reference: content.attachments.first { $0.assetId == (slot.kind == .image ? display.id : slot.id) }, item: item, original: display.role == .playback ? original : nil)
                        .id(display.id)
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(verbatim: slot.kind == .image ? "图片" : content.attachments.first(where: { $0.assetId == slot.id })?.name ?? slot.kind.label)
                        Text(slot.kind == .image ? "原图待下载" : "等待源端获取附件").font(.caption).foregroundStyle(.secondary)
                    }.accessibilityIdentifier("native-pending-slot")
                }
            }
        } else {
        ForEach(item.message.nativeAssets.filter { $0.role == .original }.filter { metadata in
            content.attachments.first(where: { $0.assetId == metadata.id })?.recordItemIndex == recordIndex
        }) { original in
            let playback = item.message.nativeAssets.first { $0.derivedFrom == original.id && $0.role == .playback }
            let display = playback ?? original
            NativeAttachmentView(metadata: display, reference: content.attachments.first { $0.assetId == original.id }, item: item, original: playback == nil ? nil : original)
        }
        }
    }
}

private struct NativeAttachmentView: View {
    @EnvironmentObject private var model: RelayAppModel
    @Environment(\.scenePhase) private var scenePhase
    let metadata: RelayNativeAssetMetadata
    let reference: RelayNativeContent.Attachment?
    let item: InboxItem
    var original: RelayNativeAssetMetadata? = nil
    @State private var fileURL: URL?
    @State private var image: UIImage?
    @State private var imageFrames: RelayImageFrames?
    @State private var player: AVPlayer?
    @State private var busy = false
    @State private var pending = false
    @State private var requested = false
    @State private var failure: String?
    @State private var playbackNotice: String?
    @State private var attempt = 0
    @State private var automaticAttempts = 0
    @State private var previewFile = false
    @State private var previewImage = false
    @State private var originalPresented = false
    @State private var originalURL: URL?
    @State private var openAfterDownload = false
    @State private var details = false

    private var isImage: Bool { metadata.kind == .image || metadata.kind == .sticker }
    private var isMedia: Bool { metadata.kind == .audio || metadata.kind == .video }
    private var isImagePreview: Bool { item.message.nativeVersion == 8 && metadata.kind == .image && metadata.role == .playback }
    private var shareLabel: String { isImagePreview ? "保存或分享预览" : "保存或分享" }
    private var label: String { reference?.name.isEmpty == false ? reference!.name : metadata.kind.label }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if metadata.kind == .file {
                    HStack(spacing: 12) {
                        Image(systemName: "doc.fill").font(.largeTitle).foregroundStyle(.orange)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(verbatim: label).font(.subheadline).lineLimit(3).fixedSize(horizontal: false, vertical: true)
                            Text(ByteCountFormatter.string(fromByteCount: Int64(metadata.byteLength), countStyle: .file)).font(.caption).foregroundStyle(.secondary)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    .onTapGesture { openFile() }
                    .accessibilityElement(children: .combine).accessibilityAddTraits(.isButton)
                    .accessibilityAction { openFile() }
                    .accessibilityIdentifier("native-file-card")
                    .contextMenu { attachmentMenu }
            }
            if let image, let imageFrames {
                    NativePreviewImage(frames: imageFrames, active: scenePhase == .active)
                    .frame(width: min(240, 260 * image.size.width / image.size.height), height: min(260, 240 * image.size.height / image.size.width))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .onTapGesture { previewImage = true }
                    .accessibilityAddTraits(.isButton).accessibilityLabel(isImagePreview ? "查看预览图片" : "查看图片")
                    .accessibilityAction { previewImage = true }
                    .contextMenu { attachmentMenu }
            }
            if isImagePreview {
                Text("预览图, 原图仍待下载").font(.caption).foregroundStyle(.secondary)
                    .accessibilityIdentifier("native-image-preview-pending")
            }
            if let player, let playerItem = player.currentItem {
                VideoPlayer(player: player).frame(height: metadata.kind == .audio ? 90 : 210)
                    .contextMenu { attachmentMenu }
                    .onReceive(playerItem.publisher(for: \.status).receive(on: RunLoop.main)) { status in
                        if status == .failed { playbackNotice = "此附件暂时无法播放, 可以保存或分享原件." }
                    }
            }
            if let playbackNotice { Text(playbackNotice).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            if busy { ProgressView(pending ? isImagePreview ? "等待预览上传" : "等待原件上传" : "下载附件") }
            if let failure { Text(failure).font(.caption).foregroundStyle(.orange) }
            if isMedia && player == nil || isImage && image == nil && !busy {
                Button(pending ? "重新加载" : isMedia ? "播放" : "加载图片", systemImage: isMedia ? "play.fill" : "photo") { requestDownload(open: false) }.disabled(busy)
                    .contextMenu { attachmentMenu }
            }
        }
        .frame(maxWidth: 260, alignment: .leading)
        .overlay(alignment: .topTrailing) {
            if isMedia {
                Menu { attachmentMenu } label: { Image(systemName: "ellipsis.circle").padding(6) }.accessibilityLabel("附件操作")
            }
        }
        .task(id: attempt) { await load() }
        .onChange(of: model.nativeAssetRevision(metadata.id, for: item)) { _, _ in
            if fileURL == nil, requested || isImage, scenePhase == .active {
                automaticAttempts = 0
                attempt += 1
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { automaticAttempts = 0; attempt += 1 }
            else { player?.pause(); attempt += 1 }
        }
        .onDisappear { player?.pause() }
        .sheet(isPresented: $details) {
            if let index = reference?.recordItemIndex, let records = item.nativeContent?.records, records.indices.contains(index) {
                NativeMessageDetails(text: records[index].text, xml: records[index].rawXML)
            } else {
                NativeMessageDetails(text: item.nativeContent?.text ?? item.preview.body, xml: item.nativeContent?.rawXML)
            }
        }
        .sheet(isPresented: $previewFile) {
            if let fileURL {
                NavigationStack {
                    NativeFilePreview(url: fileURL).navigationTitle(label).navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("完成") { previewFile = false } }
                            ToolbarItem(placement: .primaryAction) { ShareLink(item: fileURL) { Label(shareLabel, systemImage: "square.and.arrow.up") } }
                        }
                }
            }
        }
        .sheet(isPresented: $originalPresented) {
            if let originalURL {
                NavigationStack {
                    ShareLink(item: originalURL) { Label("保存或分享原件", systemImage: "square.and.arrow.up") }
                        .padding().navigationTitle("附件原件").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { originalPresented = false } } }
                }
            }
        }
        .fullScreenCover(isPresented: $previewImage) {
            if let fileURL {
                NavigationStack {
                    NativeFilePreview(url: fileURL).navigationTitle(isImagePreview ? "普通清晰度预览" : "图片").navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("完成") { previewImage = false } }
                            ToolbarItem(placement: .primaryAction) { ShareLink(item: fileURL) { Label(shareLabel, systemImage: "square.and.arrow.up") } }
                        }
                }
            }
        }
    }

    @ViewBuilder private var attachmentMenu: some View {
        Button("详细信息", systemImage: "info.circle") { details = true }
        if let fileURL { ShareLink(item: fileURL) { Label(shareLabel, systemImage: "square.and.arrow.up") } }
        if let original {
            Button("保存或分享原件", systemImage: "doc") {
                Task {
                    do { originalURL = try await model.nativeAssetURL(original, in: item); originalPresented = true }
                    catch { failure = RelayAppModel.describe(error) }
                }
            }
        }
    }

    private func requestDownload(open: Bool) {
        requested = true; openAfterDownload = open; automaticAttempts = 0; attempt += 1
    }

    private func openFile() {
        if fileURL != nil { previewFile = true }
        else { requestDownload(open: true) }
    }

    private func load() async {
        guard scenePhase == .active, requested || isImage else { return }
        if let fileURL {
            if isMedia { await preparePlayback(fileURL) }
            return
        }
        busy = true
        defer { busy = false }
        for delay in [0, 5, 15] {
            guard automaticAttempts < 6, !Task.isCancelled, scenePhase == .active else { return }
            if delay > 0 {
                do { try await Task.sleep(for: .seconds(delay)) } catch { return }
            }
            automaticAttempts += 1
            do {
                let url = try await model.nativeAssetURL(metadata, in: item)
                try Task.checkCancellation()
                fileURL = url; failure = nil; pending = false
                if openAfterDownload { previewFile = true; openAfterDownload = false }
                if isImage { loadImage(url) }
                if isMedia { await preparePlayback(url) }
                return
            } catch is CancellationError { return }
            catch RelayError.assetPending { pending = true; failure = RelayAppModel.describe(RelayError.assetPending) }
            catch { pending = false; failure = RelayAppModel.describe(error); return }
        }
    }

    private func loadImage(_ url: URL) {
        do {
            let frames = try RelayImageFrames(data: Data(contentsOf: url))
            imageFrames = frames
            image = UIImage(cgImage: frames.images[0])
        } catch {
            playbackNotice = isImagePreview ? "预览已下载, 此图片暂时无法显示. 可保存预览." : "原件已下载, 此图片暂时无法预览. 可保存原件."
        }
    }

    private func preparePlayback(_ url: URL) async {
        do {
            guard try await AVURLAsset(url: url).load(.isPlayable) else { throw RelayError.invalidValue("unsupported media") }
            try Task.checkCancellation()
            player = AVPlayer(url: url)
            playbackNotice = nil
        } catch is CancellationError {}
        catch {
            player = nil
            playbackNotice = "此附件暂时无法播放, 长按可保存或分享."
        }
    }
}

private struct NativePreviewImage: UIViewRepresentable {
    let frames: RelayImageFrames
    let active: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeUIView(context: Context) -> UIImageView {
        let view = UIImageView()
        view.isUserInteractionEnabled = true
        view.contentMode = .scaleAspectFit
        return view
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: UIImageView, context: Context) -> CGSize? {
        // Use the fitted SwiftUI frame instead of the image's intrinsic pixel dimensions before clipping.
        guard let width = proposal.width, let height = proposal.height else { return nil }
        return CGSize(width: width, height: height)
    }

    func updateUIView(_ view: UIImageView, context: Context) {
        if view.image?.cgImage !== frames.images[0] {
            view.image = UIImage(cgImage: frames.images[0])
            view.layer.removeAnimation(forKey: "native-frames")
        }
        guard active, !reduceMotion, frames.images.count > 1 else {
            view.layer.removeAnimation(forKey: "native-frames")
            return
        }
        guard view.layer.animation(forKey: "native-frames") == nil else { return }
        let animation = CAKeyframeAnimation(keyPath: "contents")
        animation.values = frames.images
        var elapsed = 0.0
        animation.keyTimes = frames.delays.map { delay in
            defer { elapsed += delay }
            return NSNumber(value: elapsed / frames.duration)
        }
        // Discrete playback needs the final boundary to preserve the last frame's duration.
        animation.keyTimes?.append(1)
        animation.duration = frames.duration
        animation.calculationMode = .discrete
        animation.repeatCount = .infinity
        view.layer.add(animation, forKey: "native-frames")
    }

    static func dismantleUIView(_ view: UIImageView, coordinator: ()) { view.layer.removeAnimation(forKey: "native-frames") }
}

private struct NativeFilePreview: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    func makeUIViewController(context: Context) -> QLPreviewController {
        let controller = QLPreviewController()
        controller.dataSource = context.coordinator
        return controller
    }
    func updateUIViewController(_ controller: QLPreviewController, context: Context) {}
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}

private extension RelayNativeContent.Kind {
    var label: String {
        switch self {
        case .text: return "文本"
        case .image: return "图片"
        case .audio: return "语音"
        case .video: return "视频"
        case .file: return "文件"
        case .sticker: return "表情"
        case .reference: return "引用消息"
        case .record: return "聊天记录"
        case .link: return "链接消息"
        case .unsupported: return "消息"
        }
    }
}

private extension RelayNativeAssetMetadata.Kind {
    var label: String {
        switch self { case .image: return "图片"; case .sticker: return "表情"; case .audio: return "语音"; case .video: return "视频"; case .file: return "文件" }
    }
    var symbol: String {
        switch self { case .image, .sticker: return "photo"; case .audio: return "waveform"; case .video: return "video"; case .file: return "doc" }
    }
}
