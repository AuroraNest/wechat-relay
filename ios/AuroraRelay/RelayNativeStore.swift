import Foundation
import RelayCore
import UniformTypeIdentifiers

@MainActor
final class RelayNativeStore {
    private let source: RelaySource
    private var generation = UUID()
    #if DEBUG
    private var demoAssets: [UUID: RelayNativeAsset] = [:]
    func seedDemo(_ asset: RelayNativeAsset) { demoAssets[asset.id] = asset }
    #endif

    init(source: RelaySource) {
        self.source = source
        // Playback and share APIs require a local plaintext file. Remove leftovers from the previous process.
        try? FileManager.default.removeItem(at: temporaryRoot)
    }

    func file(metadata: RelayNativeAssetMetadata, message: RelayMessage, content: RelayNativeContent, session: RelaySession) async throws -> URL {
        guard message.nativeAssets.contains(metadata) else { throw RelayError.invalidValue("asset metadata") }
        let run = generation
        let directory = try cacheDirectory(session: session)
        let cached = directory.appendingPathComponent(message.id.uuidString.lowercased() + "-" + metadata.id.uuidString.lowercased() + ".json")
        let demo: RelayNativeAsset?
        #if DEBUG
        demo = demoAssets[metadata.id]
        #else
        demo = nil
        #endif
        let asset: RelayNativeAsset
        if let demo { asset = demo }
        else if FileManager.default.fileExists(atPath: cached.path) {
            do { asset = try JSONDecoder().decode(RelayNativeAsset.self, from: Data(contentsOf: cached)) }
            catch { try? FileManager.default.removeItem(at: cached); throw RelayError.invalidResponse }
        } else {
            asset = try await RelayAPI(origin: session.origin).nativeAsset(session: session, id: metadata.id)
        }
        try Task.checkCancellation()
        guard run == generation else { throw CancellationError() }
        let reference = content.attachments.first { $0.assetId == metadata.id }
        let plaintext: Data
        do { plaintext = try RelayCrypto.decryptNativeAsset(asset, for: message, messageKey: session.messageKey, sha256: reference?.sha256) }
        catch { try? FileManager.default.removeItem(at: cached); throw error }
        if demo == nil, !FileManager.default.fileExists(atPath: cached.path) {
            let encoded = try JSONEncoder().encode(asset)
            try trimCache(directory, incomingBytes: encoded.count)
            try encoded.write(to: cached, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        }
        let outputDirectory = temporaryRoot.appendingPathComponent(session.pairId, isDirectory: true).appendingPathComponent(message.id.uuidString.lowercased(), isDirectory: true).appendingPathComponent(metadata.id.uuidString.lowercased(), isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        let filename = Self.filename(reference?.name, metadata: metadata)
        let output = outputDirectory.appendingPathComponent(filename)
        try plaintext.write(to: output, options: [.atomic, .completeFileProtection])
        return output
    }

    func clear(session: RelaySession) throws {
        generation = UUID()
        for directory in [try cacheDirectory(session: session), temporaryRoot.appendingPathComponent(session.pairId, isDirectory: true)] {
            if FileManager.default.fileExists(atPath: directory.path) { try FileManager.default.removeItem(at: directory) }
        }
    }

    private var temporaryRoot: URL { FileManager.default.temporaryDirectory.appendingPathComponent("relay-native-\(source.rawValue)", isDirectory: true) }

    private func cacheDirectory(session: RelaySession) throws -> URL {
        let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        var directory = support.appendingPathComponent("native-assets", isDirectory: true).appendingPathComponent("\(source.rawValue)-\(session.pairId)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try directory.setResourceValues(values)
        return directory
    }

    private func trimCache(_ directory: URL, incomingBytes: Int) throws {
        let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey])
        let entries = try files.map { url -> (URL, Int, Date) in
            let values = try url.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey])
            return (url, values.fileSize ?? 0, values.contentModificationDate ?? .distantPast)
        }.sorted { $0.2 < $1.2 }
        var total = entries.reduce(incomingBytes) { $0 + $1.1 }
        for (url, size, _) in entries where total > 128 * 1_024 * 1_024 {
            try FileManager.default.removeItem(at: url)
            total -= size
        }
    }

    private static func filename(_ name: String?, metadata: RelayNativeAssetMetadata) -> String {
        let basename = (name ?? "").replacingOccurrences(of: "\\", with: "/").split(separator: "/").last.map(String.init) ?? ""
        let clean = String(String.UnicodeScalarView(basename.unicodeScalars.filter { $0.value >= 32 && $0.value != 127 && $0 != ":" })).trimmingCharacters(in: .whitespacesAndNewlines)
        if !clean.isEmpty, clean != ".", clean != "..", clean.utf8.count <= 240 { return clean }
        let suffix = UTType(mimeType: metadata.mimeType.lowercased())?.preferredFilenameExtension ?? "bin"
        return "attachment-\(metadata.id.uuidString.lowercased()).\(suffix)"
    }
}
