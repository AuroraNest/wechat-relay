package com.aurora.wechatrelay.probe

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ImageNotificationPolicyTest {
    @Test fun onlyFreshExactImageNotificationsTriggerCapture() {
        val image = Preview("测试联系人", "[图片]", "text")
        assertTrue(ImageNotificationPolicy.shouldCapture(image, false, 0))
        assertTrue(ImageNotificationPolicy.shouldCapture(image.copy(body = "[2条]测试联系人: [图片]"), false, 120_000))
        assertFalse(ImageNotificationPolicy.shouldCapture(image.copy(body = "请发[图片]给我"), false, 0))
        assertFalse(ImageNotificationPolicy.shouldCapture(image.copy(body = "[2条]其他人: [图片]"), false, 0))
        assertFalse(ImageNotificationPolicy.shouldCapture(image, true, 0))
        assertFalse(ImageNotificationPolicy.shouldCapture(image, false, 120_001))
        assertFalse(ImageNotificationPolicy.shouldCapture(image, false, -1))
    }
}
