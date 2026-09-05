package com.aurora.wechatrelay.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WechatOriginalImagePolicyTest {
    private val root = CaptureRect(0, 0, 1200, 2670)
    private val bubble = CaptureRect(189, 521, 635, 1121)

    @Test
    fun viewerRequiresUniqueExactFullWindowImageNode() {
        val viewer = node(
            WechatImageViewerPolicy.ViewerViewId,
            WechatImageViewerPolicy.ViewerClass,
            WechatImageViewerPolicy.ViewerDescription,
            root,
        )
        assertEquals(viewer, WechatImageViewerPolicy.selectViewer(listOf(viewer), root))
        assertNull(WechatImageViewerPolicy.selectViewer(listOf(viewer.copy(label = "图片")), root))
        assertNull(WechatImageViewerPolicy.selectViewer(listOf(viewer.copy(bounds = CaptureRect(0, 100, 1200, 2670))), root))
        assertNull(WechatImageViewerPolicy.selectViewer(listOf(viewer, viewer), root))
    }

    @Test
    fun optionalViewOriginalControlAcceptsExactLabelAndSizeSuffix() {
        val control = node(null, "android.widget.TextView", WechatImageViewerPolicy.ViewOriginalLabel, CaptureRect(480, 2200, 720, 2280))
        assertEquals(listOf(control), WechatImageViewerPolicy.viewOriginalControls(listOf(control), root))
        assertEquals(listOf(control.copy(label = "查看原图 768K")), WechatImageViewerPolicy.viewOriginalControls(listOf(control.copy(label = "查看原图 768K")), root))
        assertEquals(listOf(control.copy(label = "查看原图 1.5MB")), WechatImageViewerPolicy.viewOriginalControls(listOf(control.copy(label = "查看原图 1.5MB")), root))
        assertTrue(WechatImageViewerPolicy.viewOriginalControls(listOf(control.copy(label = "查看原图后转发")), root).isEmpty())
        assertTrue(WechatImageViewerPolicy.viewOriginalControls(listOf(control.copy(label = "原图")), root).isEmpty())
        assertTrue(WechatImageViewerPolicy.viewOriginalControls(listOf(control.copy(bounds = CaptureRect(-1, 2200, 720, 2280))), root).isEmpty())
    }

    @Test
    fun aspectFitCropUsesVerifiedBubbleRatioAndExcludesViewerChrome() {
        val crop = WechatImageViewerPolicy.mapAspectFitCrop(bubble, root, root, 1200, 2670)
        assertEquals(CaptureRect(0, 511, 1200, 2159), crop)

        val currentDeviceBubble = CaptureRect(189, 1208, 789, 1605)
        assertEquals(
            CaptureRect(0, 930, 1200, 1740),
            WechatImageViewerPolicy.mapAspectFitCrop(currentDeviceBubble, root, root, 1200, 2670),
        )
    }

    @Test
    fun aspectFitCropMapsViewerCoordinatesToScreenshotPixels() {
        val viewer = CaptureRect(0, 100, 1200, 2770)
        val screen = CaptureRect(0, 100, 1200, 2770)
        val crop = WechatImageViewerPolicy.mapAspectFitCrop(bubble, viewer, screen, 600, 1335)
        assertEquals(CaptureRect(0, 255, 600, 1080), crop)
    }

    @Test
    fun aspectFitCropRejectsTallOrInvalidRatiosThatCouldIncludeToolbar() {
        assertNull(WechatImageViewerPolicy.mapAspectFitCrop(CaptureRect(0, 0, 330, 600), root, root, 1200, 2670))
        assertNull(WechatImageViewerPolicy.mapAspectFitCrop(CaptureRect(0, 0, 200, 1200), root, root, 1200, 2670))
        assertNull(WechatImageViewerPolicy.mapAspectFitCrop(CaptureRect(0, 0, 0, 600), root, root, 1200, 2670))
        assertNull(WechatImageViewerPolicy.mapAspectFitCrop(bubble, CaptureRect(-1, 0, 1200, 2670), root, 1200, 2670))
        assertNull(WechatImageViewerPolicy.mapAspectFitCrop(bubble, root, root, 0, 2670))
    }

    @Test
    fun viewerTransitionWaitsThroughTransientMissingRootAndRequiresStableWindow() {
        assertTrue(WechatImageViewerPolicy.withinDeadline(nowMillis = 4_999L, deadlineMillis = 5_000L))
        assertTrue(!WechatImageViewerPolicy.withinDeadline(nowMillis = 5_000L, deadlineMillis = 5_000L))
        assertTrue(!WechatImageViewerPolicy.hasSettled(nowMillis = 1_449L, observedSinceMillis = 1_000L, settleMillis = 450L))
        assertTrue(WechatImageViewerPolicy.hasSettled(nowMillis = 1_450L, observedSinceMillis = 1_000L, settleMillis = 450L))
    }

    private fun node(viewId: String?, className: String, label: String, bounds: CaptureRect) = OriginalImageNodeSnapshot(
        viewId = viewId,
        className = className,
        label = label,
        visible = true,
        enabled = true,
        bounds = bounds,
    )
}
