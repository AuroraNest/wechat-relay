import XCTest

final class AuroraRelayUITests: XCTestCase {
    private let app = XCUIApplication()

    override func setUpWithError() throws {
        continueAfterFailure = false
        app.launchArguments = ["--demo"]
        app.launch()
    }

    func testDemoInboxReplyDeviceAndSettings() throws {
        XCTAssertTrue(app.staticTexts["演示模式 · 虚构数据"].waitForExistence(timeout: 5))
        attachScreenshot("演示收件箱")

        let contact = app.staticTexts["林一"].firstMatch
        XCTAssertTrue(contact.waitForExistence(timeout: 3))
        contact.tap()

        let reply = app.textFields["输入回复"]
        XCTAssertTrue(reply.waitForExistence(timeout: 3))
        reply.tap()
        reply.typeText("UI smoke reply")
        app.buttons["发送回复"].tap()
        XCTAssertTrue(app.staticTexts["UI smoke reply"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["已发往微信"].waitForExistence(timeout: 3))
        attachScreenshot("演示回复")

        app.navigationBars.buttons.firstMatch.tap()
        let deviceTab = app.tabBars.buttons["设备"]
        XCTAssertTrue(deviceTab.waitForExistence(timeout: 3))
        deviceTab.tap()
        XCTAssertTrue(app.staticTexts["Android 已配对"].waitForExistence(timeout: 3))
        app.staticTexts["连接诊断"].tap()
        XCTAssertTrue(app.staticTexts["设备配对"].waitForExistence(timeout: 3))
        attachScreenshot("设备诊断")

        app.navigationBars.buttons.firstMatch.tap()
        app.tabBars.buttons["设置"].tap()
        app.staticTexts["转发与时间段"].tap()
        let schedule = app.switches["仅在指定时间转发"]
        XCTAssertTrue(schedule.waitForExistence(timeout: 3))
        schedule.switches.firstMatch.tap()
        XCTAssertEqual(schedule.value as? String, "1")
        app.swipeUp()
        XCTAssertTrue(app.datePickers.firstMatch.waitForExistence(timeout: 3))
        XCTAssertEqual(app.datePickers.count, 2)
        XCTAssertTrue(app.staticTexts["开始"].exists)
        XCTAssertTrue(app.staticTexts["结束"].exists)
        attachScreenshot("转发时间段")
        app.buttons["保存"].tap()
        XCTAssertTrue(app.staticTexts["转发与时间段"].waitForExistence(timeout: 3))
    }

    func testWelcomeRejectsInsecureServiceOrigin() {
        app.terminate()
        app.launchArguments = []
        app.launch()
        XCTAssertTrue(app.buttons["连接我的设备"].waitForExistence(timeout: 5))
        attachScreenshot("欢迎页面")
        app.buttons["连接我的设备"].tap()
        let address = app.textFields["https://relay.example.com"]
        XCTAssertTrue(address.waitForExistence(timeout: 3))
        address.tap()
        address.typeText("http://example.invalid")
        app.secureTextFields["接入令牌"].tap()
        app.secureTextFields["接入令牌"].typeText("fictional-token")
        app.buttons["生成配对码"].tap()
        XCTAssertTrue(app.staticTexts["请填写完整的 HTTPS 服务地址, 不带路径或参数."].waitForExistence(timeout: 3))
        attachScreenshot("服务地址校验")
    }

    private func attachScreenshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
