import Foundation
/// A parsed hotkey: Carbon virtual key code plus Carbon modifier flags.
public struct HotkeySpec: Equatable {
    public let keyCode: UInt32
    public let modifiers: UInt32
}

public enum HotkeyError: Error, Equatable, CustomStringConvertible {
    case empty
    case noKey
    case multipleKeys(String, String)
    case unknownToken(String)

    public var description: String {
        switch self {
        case .empty: return "hotkey is empty"
        case .noKey: return "hotkey has no key (use one letter or digit)"
        case let .multipleKeys(a, b): return "hotkey has two keys: \(a) and \(b)"
        case let .unknownToken(t): return "unknown hotkey part: \(t)"
        }
    }
}

// Carbon modifier flags (Events.h): cmdKey, shiftKey, optionKey, controlKey.
private let modifierFlags: [String: UInt32] = [
    "cmd": 0x0100, "shift": 0x0200, "option": 0x0800, "alt": 0x0800, "ctrl": 0x1000,
]

// ANSI virtual key codes (Events.h, kVK_ANSI_*).
private let keyCodes: [String: UInt32] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
]

public func parseHotkey(_ spec: String) throws -> HotkeySpec {
    let tokens = spec.lowercased().split(separator: "+", omittingEmptySubsequences: false)
        .map { $0.trimmingCharacters(in: .whitespaces) }
    if tokens.allSatisfy({ $0.isEmpty }) { throw HotkeyError.empty }
    var modifiers: UInt32 = 0
    var key: (name: String, code: UInt32)?
    for token in tokens {
        if let flag = modifierFlags[token] {
            modifiers |= flag
        } else if let code = keyCodes[token] {
            if let first = key { throw HotkeyError.multipleKeys(first.name, token) }
            key = (token, code)
        } else {
            throw HotkeyError.unknownToken(token)
        }
    }
    guard let key else { throw HotkeyError.noKey }
    return HotkeySpec(keyCode: key.code, modifiers: modifiers)
}
