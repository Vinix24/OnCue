import CoreGraphics
import XCTest
@testable import OnCueBarCore

final class PlacementTests: XCTestCase {
    private let size = CGSize(width: 290, height: 56)

    func testTopRightOnScreenAtZeroOrigin() {
        let frame = CGRect(x: 0, y: 0, width: 1440, height: 900)
        let origin = topRightOrigin(visibleFrame: frame, windowSize: size, margin: 12)
        XCTAssertEqual(origin, CGPoint(x: 1440 - 290 - 12, y: 900 - 56 - 12))
    }

    func testMenuBarInsetKeepsBarBelowMenuBar() {
        let frame = CGRect(x: 0, y: 0, width: 1440, height: 900 - 38)
        let origin = topRightOrigin(visibleFrame: frame, windowSize: size, margin: 12)
        XCTAssertEqual(origin.y, 862 - 56 - 12)
        XCTAssertEqual(origin.x, 1138)
    }

    func testScreenWithNonZeroOrigin() {
        let frame = CGRect(x: -1920, y: 200, width: 1920, height: 1055)
        let origin = topRightOrigin(visibleFrame: frame, windowSize: size, margin: 12)
        XCTAssertEqual(origin, CGPoint(x: -290 - 12, y: 1255 - 56 - 12))
    }
}
