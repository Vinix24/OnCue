import XCTest

@testable import AudioTeeCore

final class DeviceChangeLogTests: XCTestCase {
  private let date = Date(timeIntervalSince1970: 1_700_000_000)  // 2023-11-14T22:13:20Z

  func testDefaultOutputChange() {
    let line = DeviceChangeLog.defaultDeviceChange(
      property: "default_output",
      old: .init(name: "MacBook Speakers", uid: "BuiltInSpeakerDevice"),
      new: .init(name: "AirPods", uid: "AA-BB-CC:output"), at: date)
    XCTAssertEqual(
      line,
      "audiotee: device_change ts=2023-11-14T22:13:20.000Z property=default_output"
        + " old_name=\"MacBook Speakers\" old_uid=BuiltInSpeakerDevice"
        + " new_name=\"AirPods\" new_uid=AA-BB-CC:output")
  }

  func testDefaultOutputWithNoDeviceIsVisible() {
    let line = DeviceChangeLog.defaultDeviceChange(
      property: "default_system_output", old: .init(name: "A", uid: "a"), new: nil, at: date)
    XCTAssertTrue(line.hasSuffix("new_name=\"\" new_uid=\"\""))
  }

  func testDeviceListChange() {
    let line = DeviceChangeLog.deviceListChange(
      count: 3, added: ["AirPods"], removed: ["Studio Display", "Zoom"], at: date)
    XCTAssertEqual(
      line,
      "audiotee: device_change ts=2023-11-14T22:13:20.000Z property=device_list count=3"
        + " added=[\"AirPods\"] removed=[\"Studio Display\",\"Zoom\"]")
  }

  func testAliveZeroAndOne() {
    XCTAssertTrue(DeviceChangeLog.aliveChange(alive: false, at: date).hasSuffix("alive=0"))
    XCTAssertTrue(DeviceChangeLog.aliveChange(alive: true, at: date).hasSuffix("alive=1"))
  }

  func testNameWithQuoteAndNewlineStaysOneLine() {
    let line = DeviceChangeLog.defaultDeviceChange(
      property: "default_output", old: nil,
      new: .init(name: "Vincent's \"Pro\"\nMic\\", uid: "u"), at: date)
    XCTAssertFalse(line.contains("\n"))
    XCTAssertTrue(line.contains("new_name=\"Vincent's \\\"Pro\\\"\\nMic\\\\\""))
  }

  func testStartLine() {
    let line = DeviceChangeLog.startLine(
      output: .init(name: "X", uid: "x"), systemOutput: nil, at: date)
    XCTAssertTrue(line.hasPrefix("audiotee: device_start ts=2023-11-14T22:13:20.000Z"))
    XCTAssertTrue(line.contains("system_output_name=\"\""))
  }
}
