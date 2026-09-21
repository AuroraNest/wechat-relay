import Foundation

public enum RelayStreamEvent: Sendable, Equatable {
    case ready(Int)
    case message(Int)
    case reply(UUID)
}

// Only bounded invalidation hints travel here; canonical data still comes from authenticated APIs.
struct RelayStreamParser {
    private var line = Data()
    private var event = "message"
    private var data = ""
    private var frameBytes = 0
    private var previousWasCR = false

    mutating func append(_ byte: UInt8) throws -> RelayStreamEvent? {
        if byte == 10 && previousWasCR { previousWasCR = false; return nil }
        previousWasCR = byte == 13
        guard byte == 10 || byte == 13 else {
            guard line.count < 1_024, frameBytes < 4_096 else { throw RelayError.responseTooLarge }
            line.append(byte)
            frameBytes += 1
            return nil
        }
        guard let text = String(data: line, encoding: .utf8) else { throw RelayError.invalidResponse }
        line.removeAll(keepingCapacity: true)
        if text.isEmpty {
            defer { event = "message"; data = ""; frameBytes = 0 }
            guard !data.isEmpty else { return nil }
            switch event {
            case "ready", "message":
                guard data.utf8.allSatisfy({ (48...57).contains($0) }),
                      let sequence = Int(data), sequence <= 9_007_199_254_740_991 else { throw RelayError.invalidResponse }
                return event == "ready" ? .ready(sequence) : .message(sequence)
            case "reply":
                guard RelayValidation.isUUID(data), let id = UUID(uuidString: data) else { throw RelayError.invalidResponse }
                return .reply(id)
            default: return nil
            }
        }
        if text.hasPrefix(":") { return nil }
        let parts = text.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        var value = parts.count == 2 ? String(parts[1]) : ""
        if value.hasPrefix(" ") { value.removeFirst() }
        if parts[0] == "event" { event = value }
        if parts[0] == "data" { data += (data.isEmpty ? "" : "\n") + value }
        return nil
    }
}

final class RelayStreamRedirectDelegate: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        // The stream has a fixed endpoint; never forward pair credentials through redirects.
        completionHandler(nil)
    }
}
