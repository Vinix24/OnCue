import CoreGraphics
import Foundation

/// The part of the recording server's /status answer the quit action needs.
public struct ServerStatus: Equatable {
    public let phase: String
    public let pid: Int?

    public init(phase: String, pid: Int?) {
        self.phase = phase
        self.pid = pid
    }
}

/// Reads a /status body. Anything that is not a JSON object with a string
/// `phase` counts as no answer.
public func parseServerStatus(_ data: Data) -> ServerStatus? {
    guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let phase = object["phase"] as? String else { return nil }
    return ServerStatus(phase: phase, pid: object["pid"] as? Int)
}

/// What "OnCue afsluiten" does: an optional question to ask first, and the
/// server pid to send SIGTERM to once the user agrees (or at once without a
/// question). Without a pid only the bar quits.
public struct QuitPlan: Equatable {
    public let question: String?
    public let signalPid: Int?
}

public func quitPlan(for status: ServerStatus?) -> QuitPlan {
    guard let status else {
        return QuitPlan(question: "Status onbekend. Toch afsluiten?", signalPid: nil)
    }
    switch status.phase {
    case "recording":
        return QuitPlan(question: "Er loopt een opname. Afronden en OnCue afsluiten?", signalPid: status.pid)
    case "idle", "transcribing", "ready", "failed":
        return QuitPlan(question: nil, signalPid: status.pid)
    default:
        return QuitPlan(question: "Status onbekend. Toch afsluiten?", signalPid: status.pid)
    }
}

/// The screen that carries the menu bar is the first one in AppKit's list.
public func menuBarScreenFrame(visibleFrames: [CGRect], fallback: CGRect) -> CGRect {
    visibleFrames.first ?? fallback
}
