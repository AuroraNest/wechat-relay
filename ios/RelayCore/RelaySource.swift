import Foundation

public enum RelaySource: String, Codable, CaseIterable, Sendable {
    case tablet, phone

    public var label: String { self == .tablet ? "平板" : "小米手机" }
    public func account(_ name: String) -> String { "\(rawValue).\(name)" }
    public func conversationID(pairID: String, profile: Int, name: String) -> String {
        "\(rawValue):\(pairID.lowercased()):\(profile):\(name)"
    }
    public func owns(_ conversationID: String, pairID: String) -> Bool {
        conversationID.hasPrefix("\(rawValue):\(pairID.lowercased()):")
    }
}

public enum RelaySourceSelection: String, Codable, CaseIterable, Sendable {
    case tablet, phone, mixed

    public var sources: [RelaySource] {
        switch self {
        case .tablet: return [.tablet]
        case .phone: return [.phone]
        case .mixed: return [.tablet, .phone]
        }
    }
    public var label: String {
        switch self {
        case .tablet: return "平板"
        case .phone: return "小米手机"
        case .mixed: return "混合"
        }
    }
    public static func restored(_ value: String?) -> Self { value.flatMap(Self.init(rawValue:)) ?? .tablet }
}

public enum RelaySessionRouting {
    // Unknown and ambiguous pairs fail closed; never fall back to the currently displayed source.
    public static func source(pairID: String, sessions: [RelaySource: RelaySession]) -> RelaySource? {
        let matches = sessions.filter { $0.value.pairId == pairID.lowercased() }
        return matches.count == 1 ? matches.first?.key : nil
    }
}
