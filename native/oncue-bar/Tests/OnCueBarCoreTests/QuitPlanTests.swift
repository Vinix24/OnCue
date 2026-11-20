import CoreGraphics
import Foundation
import XCTest
@testable import OnCueBarCore

final class QuitPlanTests: XCTestCase {
    func testRecordingAsksAndSignalsServer() {
        let plan = quitPlan(for: ServerStatus(phase: "recording", pid: 42))
        XCTAssertEqual(plan.question, "Er loopt een opname. Afronden en OnCue afsluiten?")
        XCTAssertEqual(plan.signalPid, 42)
    }

    func testFinishedTranscriptionPhasesQuitWithoutQuestion() {
        for phase in ["ready", "failed"] {
            let plan = quitPlan(for: ServerStatus(phase: phase, pid: 9))
            XCTAssertNil(plan.question)
            XCTAssertEqual(plan.signalPid, 9)
        }
    }

    func testUnknownPhaseStillAsksForStatusUnknown() {
        let plan = quitPlan(for: ServerStatus(phase: "bogus", pid: 9))
        XCTAssertEqual(plan.question, "Status onbekend. Toch afsluiten?")
    }

    func testIdleAndTranscribingQuitWithoutQuestion() {
        for phase in ["idle", "transcribing"] {
            let plan = quitPlan(for: ServerStatus(phase: phase, pid: 7))
            XCTAssertNil(plan.question)
            XCTAssertEqual(plan.signalPid, 7)
        }
    }

    func testNoAnswerAsksAndOnlyQuitsBar() {
        let plan = quitPlan(for: nil)
        XCTAssertEqual(plan.question, "Status onbekend. Toch afsluiten?")
        XCTAssertNil(plan.signalPid)
    }

    func testUnknownPhaseAsksInsteadOfQuittingSilently() {
        let plan = quitPlan(for: ServerStatus(phase: "bogus", pid: 9))
        XCTAssertEqual(plan.question, "Status onbekend. Toch afsluiten?")
    }

    func testParseServerStatus() {
        let ok = Data(#"{"phase":"recording","paused":true,"elapsed_s":3,"session":"s","pid":123}"#.utf8)
        XCTAssertEqual(parseServerStatus(ok), ServerStatus(phase: "recording", pid: 123))
        XCTAssertNil(parseServerStatus(Data("nope".utf8)))
        XCTAssertNil(parseServerStatus(Data(#"{"pid":1}"#.utf8)))
        XCTAssertEqual(parseServerStatus(Data(#"{"phase":"idle"}"#.utf8)), ServerStatus(phase: "idle", pid: nil))
    }

    func testPlacementUsesFirstScreen() {
        let first = CGRect(x: 0, y: 0, width: 1440, height: 862)
        let second = CGRect(x: 1440, y: 0, width: 1920, height: 1055)
        let fallback = CGRect(x: 0, y: 0, width: 1440, height: 900)
        XCTAssertEqual(menuBarScreenFrame(visibleFrames: [first, second], fallback: fallback), first)
        XCTAssertEqual(menuBarScreenFrame(visibleFrames: [], fallback: fallback), fallback)
    }
}
