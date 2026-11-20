import XCTest
@testable import OnCueBarCore

final class HotkeyTests: XCTestCase {
    func testDefaultSpec() throws {
        let spec = try parseHotkey("ctrl+option+r")
        XCTAssertEqual(spec, HotkeySpec(keyCode: 15, modifiers: 0x1000 | 0x0800))
    }

    func testCmdShiftDigit() throws {
        let spec = try parseHotkey("cmd+shift+1")
        XCTAssertEqual(spec, HotkeySpec(keyCode: 18, modifiers: 0x0100 | 0x0200))
    }

    func testAltIsOption() throws {
        XCTAssertEqual(try parseHotkey("alt+r"), try parseHotkey("option+r"))
    }

    func testCaseAndSpacesAreTolerated() throws {
        XCTAssertEqual(try parseHotkey("Ctrl + Option + R"), try parseHotkey("ctrl+option+r"))
    }

    func testRejectsEmpty() {
        XCTAssertThrowsError(try parseHotkey("")) { XCTAssertEqual($0 as? HotkeyError, .empty) }
    }

    func testRejectsNoKey() {
        XCTAssertThrowsError(try parseHotkey("ctrl+option")) { XCTAssertEqual($0 as? HotkeyError, .noKey) }
    }

    func testRejectsTwoKeys() {
        XCTAssertThrowsError(try parseHotkey("ctrl+a+b")) {
            XCTAssertEqual($0 as? HotkeyError, .multipleKeys("a", "b"))
        }
    }

    func testRejectsUnknownModifier() {
        XCTAssertThrowsError(try parseHotkey("hyper+r")) {
            XCTAssertEqual($0 as? HotkeyError, .unknownToken("hyper"))
        }
    }
}
