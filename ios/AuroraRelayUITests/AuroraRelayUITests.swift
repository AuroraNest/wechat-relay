import XCTest

final class AuroraRelayUITests: XCTestCase {
    private let app = XCUIApplication()

    override func setUpWithError() throws {
        continueAfterFailure = false
        app.launchArguments = ["--demo"]
        app.launch()
    }

    func testDemoInboxReplyFriendsAndSettings() throws {
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

        let back = app.navigationBars.buttons["BackButton"]
        XCTAssertTrue(back.waitForExistence(timeout: 3))
        XCTAssertGreaterThanOrEqual(back.frame.minY, app.buttons["source-picker"].frame.maxY)
        back.tap()
        let friendsTab = app.tabBars.buttons["好友"]
        XCTAssertTrue(friendsTab.waitForExistence(timeout: 3))
        friendsTab.tap()
        let friendSearch = app.searchFields["搜索好友"]
        XCTAssertTrue(friendSearch.waitForExistence(timeout: 3))
        friendSearch.tap()
        friendSearch.typeText("王珊")
        XCTAssertTrue(app.staticTexts["王珊"].waitForExistence(timeout: 3))
        app.staticTexts["王珊"].tap()
        XCTAssertTrue(reply.waitForExistence(timeout: 3))
        reply.tap()
        reply.typeText("First contact message")
        app.buttons["发送回复"].tap()
        XCTAssertTrue(app.staticTexts["First contact message"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["已发往微信"].waitForExistence(timeout: 3))
        attachScreenshot("好友主动发送")

        app.navigationBars.buttons.firstMatch.tap()
        app.tabBars.buttons["消息"].tap()
        XCTAssertTrue(app.staticTexts["First contact message"].waitForExistence(timeout: 3))
        app.staticTexts["王珊"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["First contact message"].waitForExistence(timeout: 3))
        app.navigationBars.buttons.firstMatch.tap()
        app.tabBars.buttons["设置"].tap()
        app.staticTexts["设备与连接"].tap()
        XCTAssertTrue(app.staticTexts["来源已配对"].waitForExistence(timeout: 3))
        app.staticTexts["连接诊断"].tap()
        XCTAssertTrue(app.staticTexts["设备配对"].waitForExistence(timeout: 3))
        attachScreenshot("设备诊断")
        app.navigationBars.buttons.firstMatch.tap()
        app.navigationBars.buttons.firstMatch.tap()
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

    func testFriendWithExistingConversationOpensChat() throws {
        app.tabBars.buttons["好友"].tap()
        let friend = app.staticTexts["林一"].firstMatch
        XCTAssertTrue(friend.waitForExistence(timeout: 3))
        friend.tap()
        XCTAssertTrue(app.textFields["输入回复"].waitForExistence(timeout: 3))
    }

    func testSourceSwitchKeepsRepliesIsolatedAndMixedShowsBoth() {
        app.staticTexts["林一"].firstMatch.tap()
        let reply = app.textFields["输入回复"]
        XCTAssertTrue(reply.waitForExistence(timeout: 3))
        reply.tap()
        reply.typeText("Tablet route only")
        app.buttons["发送回复"].tap()
        XCTAssertTrue(app.staticTexts["Tablet route only"].waitForExistence(timeout: 3))

        selectSource("小米手机")
        XCTAssertFalse(app.staticTexts["Tablet route only"].exists)
        app.staticTexts["林一"].firstMatch.tap()
        XCTAssertFalse(app.staticTexts["Tablet route only"].exists)
        XCTAssertTrue(reply.waitForExistence(timeout: 3))
        reply.tap()
        reply.typeText("Phone route only")
        app.buttons["发送回复"].tap()
        XCTAssertTrue(app.staticTexts["Phone route only"].waitForExistence(timeout: 3))

        selectSource("混合")
        XCTAssertTrue(app.staticTexts["Tablet route only"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Phone route only"].waitForExistence(timeout: 3))
        XCTAssertEqual(app.staticTexts.matching(identifier: "林一").count, 2)
        attachScreenshot("混合来源独立会话")

        selectSource("平板")
        XCTAssertTrue(app.staticTexts["Tablet route only"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.staticTexts["Phone route only"].exists)
    }

    func testNativeFullContentRawXMLAndOriginalFile() {
        app.terminate()
        app.launchArguments = ["--demo", "--demo-native"]
        app.launch()
        XCTAssertTrue(app.staticTexts["原始内容演示"].firstMatch.waitForExistence(timeout: 5))
        app.staticTexts["原始内容演示"].firstMatch.tap()
        let full = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "完整正文结束")).firstMatch
        XCTAssertTrue(full.waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["原始消息目前仅支持查看."].exists)
        let download = app.buttons["下载附件"].firstMatch
        for _ in 0..<5 where !download.isHittable { app.swipeUp() }
        XCTAssertTrue(download.waitForExistence(timeout: 3))
        download.tap()
        XCTAssertTrue(app.buttons["保存或分享原件"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["预览文件"].exists)
        let raw = app.buttons["查看原始 XML"].firstMatch
        for _ in 0..<5 where !raw.isHittable { app.swipeDown() }
        raw.tap()
        XCTAssertTrue(app.staticTexts["<msg><script>仅作为原始文本显示</script></msg>"].waitForExistence(timeout: 3))
        app.buttons["完成"].tap()
        selectSource("混合")
        XCTAssertEqual(app.staticTexts.matching(identifier: "原始内容演示").count, 2)
        attachScreenshot("完整原始内容与来源隔离")
    }

    private func selectSource(_ label: String) {
        let picker = app.buttons["source-picker"]
        XCTAssertTrue(picker.waitForExistence(timeout: 3))
        picker.tap()
        app.buttons[label].firstMatch.tap()
    }

    private func attachScreenshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
