import XCTest

final class TabletInputRegressionTests: XCTestCase {
    private let app = XCUIApplication()

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testPendingDraftSurvivesContactRefreshAndAbsentLegacyBootstrap() {
        app.launchArguments = ["--tablet-input-regression"]
        app.launch()
        let composer = app.textFields["message-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        XCTAssertTrue(composer.isEnabled, "A pending send channel must still allow drafting.")
        let send = app.buttons["发送回复"]
        XCTAssertFalse(send.isEnabled)
        composer.tap()
        composer.typeText("Pending tablet draft")
        XCTAssertEqual(composer.value as? String, "Pending tablet draft")

        // Only the real v3 contacts decoder and verified v8 bootstrap response can enable this button.
        let ready = NSPredicate { _, _ in send.isEnabled }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: ready, object: nil)], timeout: 30), .completed)
        XCTAssertEqual(composer.value as? String, "Pending tablet draft")
        XCTAssertTrue(app.navigationBars["Regression Friend"].exists)
        XCTAssertFalse(app.staticTexts["演示数据"].exists)
    }

    func testMissingAliasKeepsSendDisabledButDraftEditable() {
        app.launchArguments = ["--tablet-input-regression", "--tablet-input-missing-alias"]
        app.launch()
        let composer = app.textFields["message-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        XCTAssertTrue(composer.isEnabled)
        composer.tap()
        composer.typeText("Do not send this draft")
        let reason = app.staticTexts["此好友缺少微信号, 暂时无法安全发送. 请在微信确认微信号后重新同步好友."]
        XCTAssertTrue(reason.waitForExistence(timeout: 30))
        XCTAssertEqual(composer.value as? String, "Do not send this draft")
        XCTAssertTrue(composer.isEnabled)
        XCTAssertFalse(app.buttons["发送回复"].isEnabled)
    }

    func testNameOnlyRouteRequiresExplicitFriendSelectionAfterIdentityUpgrade() {
        app.launchArguments = ["--tablet-input-regression", "--tablet-input-legacy-route"]
        app.launch()
        let composer = app.textFields["message-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("Stale route draft")
        let select = app.buttons["选择好友"]
        XCTAssertTrue(select.waitForExistence(timeout: 30))
        XCTAssertEqual(composer.value as? String, "Stale route draft")
        XCTAssertFalse(app.buttons["发送回复"].isEnabled, "A display name cannot authorize a new stable identity.")
        select.tap()
        let friend = app.staticTexts["Regression Friend"].firstMatch
        XCTAssertTrue(friend.waitForExistence(timeout: 5))
        friend.tap()
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("Explicitly selected friend")
        let ready = NSPredicate { _, _ in self.app.buttons["发送回复"].isEnabled }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: ready, object: nil)], timeout: 10), .completed)
        XCTAssertTrue(app.navigationBars["Regression Friend"].exists)
    }
}
