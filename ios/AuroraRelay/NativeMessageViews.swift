import AVKit
import Combine
import ImageIO
import QuickLook
import RelayCore
import SwiftUI

struct NativeContentView: View {
    @EnvironmentObject private var model: RelayAppModel
    let item: InboxItem
    @State private var busy = false
    @State private var failure: String?
    @State private var attempt = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let content = item.nativeContent {
                if !content.senderName.isEmpty {
                    Text(verbatim: content.senderName).font(.caption).foregroundStyle(.secondary)
                }
                Text(content.kind.label).font(.caption2).foregroundStyle(.secondary)
                if !content.text.isEmpty { Text(verbatim: content.text).textSelection(.enabled).font(.body).lineSpacing(4) }
                if let records = content.records, !records.isEmpty {
                    DisclosureGroup("聊天记录 (\(records.count) 条)") {
                        ForEach(Array(records.enumerated()), id: \.offset) { _, record in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(verbatim: record.senderName + " · " + record.kind.label).font(.caption).foregroundStyle(.secondary)
                                if !record.text.isEmpty { Text(verbatim: record.text).textSelection(.enabled) }
                                if let xml = record.rawXML, !xml.isEmpty { NativeRawTextLink(text: xml) }
                            }.padding(.vertical, 6)
                        }
                    }
                }
                if let xml = content.rawXML, !xml.isEmpty { NativeRawTextLink(text: xml) }
                ForEach(item.message.nativeAssets) { metadata in
                    NativeAttachmentView(metadata: metadata, reference: content.attachments.first { $0.assetId == metadata.id }, item: item)
                }
                if (content.kind == .record || content.kind == .reference) && item.message.nativeAssets.isEmpty {
                    Text("已保留来源提供的文本和 XML. 未提供的内嵌文件不会显示为已下载.").font(.caption2).foregroundStyle(.secondary)
                }
            } else {
                Text(verbatim: item.preview.body).textSelection(.enabled)
                if busy { ProgressView("获取完整内容") }
                if let problem = failure ?? model.nativeContentProblem(for: item) { Text(problem).font(.caption).foregroundStyle(.orange) }
                Button("加载完整内容") { attempt += 1 }.disabled(busy)
            }
        }
        .task(id: attempt) {
            guard item.nativeContent == nil else { return }
            busy = true
            defer { busy = false }
            do { _ = try await model.loadNativeContent(for: item); failure = nil }
            catch is CancellationError {}
            catch { failure = RelayAppModel.describe(error) }
        }
    }
}

private struct NativeRawTextLink: View {
    let text: String
    @State private var presented = false
    var body: some View {
        Button("查看原始 XML") { presented = true }.font(.caption)
            .sheet(isPresented: $presented) {
                NavigationStack {
                    ScrollView { Text(verbatim: text).font(.system(.footnote, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding() }
                        .navigationTitle("原始 XML").navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("完成") { presented = false } }
                            ToolbarItem(placement: .primaryAction) { ShareLink(item: text) { Label("分享原文", systemImage: "square.and.arrow.up") } }
                        }
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
    @State private var fileURL: URL?
    @State private var image: UIImage?
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

    private var isImage: Bool { metadata.kind == .image || metadata.kind == .sticker }
    private var isMedia: Bool { metadata.kind == .audio || metadata.kind == .video }
    private var label: String { reference?.name.isEmpty == false ? reference!.name : metadata.kind.label }
    private var canPreviewFile: Bool {
        ["application/pdf", "text/plain", "application/rtf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.openxmlformats-officedocument.presentationml.presentation"].contains(metadata.mimeType.lowercased())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(label, systemImage: metadata.kind.symbol).font(.subheadline).lineLimit(3)
            Text("\(metadata.role == .original ? "原始附件" : "独立播放版本") · \(ByteCountFormatter.string(fromByteCount: Int64(metadata.byteLength), countStyle: .file)) · \(metadata.mimeType)").font(.caption2).foregroundStyle(.secondary)
            if metadata.role == .playback { Text("由原件派生, 原始字节另行保留.").font(.caption2).foregroundStyle(.secondary) }
            if let image {
                Button { previewImage = true } label: {
                    Image(uiImage: image).resizable().scaledToFit().frame(maxWidth: 240, maxHeight: 260).clipShape(RoundedRectangle(cornerRadius: 10))
                }.accessibilityLabel("查看原始图片")
            }
            if let player, let playerItem = player.currentItem {
                VideoPlayer(player: player).frame(height: metadata.kind == .audio ? 90 : 210)
                    .onReceive(playerItem.publisher(for: \.status).receive(on: RunLoop.main)) { status in
                        if status == .failed { playbackNotice = "此附件暂时无法播放, 可以保存或分享原件." }
                    }
            }
            if let playbackNotice { Text(playbackNotice).font(.caption).foregroundStyle(.secondary) }
            if busy { ProgressView(pending ? "等待原件上传" : "下载附件") }
            if let failure { Text(failure).font(.caption).foregroundStyle(.orange) }
            if let fileURL {
                ShareLink(item: fileURL) { Label(metadata.role == .original ? "保存或分享原件" : "保存或分享播放版本", systemImage: "square.and.arrow.up") }
                if canPreviewFile { Button("预览文件") { previewFile = true } }
            } else {
                Button(pending ? "重新检查附件" : "下载附件") {
                    requested = true; automaticAttempts = 0; attempt += 1
                }.disabled(busy)
            }
        }
        .padding(.vertical, 6)
        .task(id: attempt) { await load() }
        .onChange(of: model.nativeLastSync(for: item)) { _, _ in
            if pending, !busy, automaticAttempts < 6, scenePhase == .active { attempt += 1 }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { automaticAttempts = 0; attempt += 1 }
            else { player?.pause(); attempt += 1 }
        }
        .onDisappear { player?.pause() }
        .sheet(isPresented: $previewFile) { if let fileURL { NativeFilePreview(url: fileURL) } }
        .fullScreenCover(isPresented: $previewImage) {
            if let fileURL {
                NavigationStack {
                    NativeFilePreview(url: fileURL).navigationTitle("原始图片").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { previewImage = false } } }
                }
            }
        }
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
                if isImage { loadImage(url) }
                if isMedia { await preparePlayback(url) }
                return
            } catch is CancellationError { return }
            catch RelayError.assetPending { pending = true; failure = RelayAppModel.describe(RelayError.assetPending) }
            catch { pending = false; failure = RelayAppModel.describe(error); return }
        }
    }

    private func loadImage(_ url: URL) {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int, let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= 16_384, height <= 16_384, width <= 64_000_000 / height,
              let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 2_048, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else {
            playbackNotice = "原件已下载, 此图片暂时无法预览. 可保存原件."
            return
        }
        image = UIImage(cgImage: thumbnail)
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
            playbackNotice = "iPhone 暂不支持此原始编码 (例如 SILK), 可以保存或分享原件. 不进行语音转写."
        }
    }
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
        case .text: return "完整文本"
        case .image: return "原始图片"
        case .audio: return "原始语音"
        case .video: return "原始视频"
        case .file: return "原始文件"
        case .sticker: return "原始表情"
        case .reference: return "引用消息"
        case .record: return "聊天记录"
        case .link: return "链接消息"
        case .unsupported: return "原始消息"
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
