package com.aurora.wechatrelay.probe

import android.app.Notification
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.charset.StandardCharsets
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

class ProbeCoreTest {
    @Test
    fun keyguardDismissWaitsForFocusedVisibleGateAndRefocusDoesNotRepeatIt() {
        assertFalse(UnlockGatePolicy.shouldRequestDismiss(false, false, false))
        assertTrue(UnlockGatePolicy.shouldRequestDismiss(true, false, false))
        assertFalse(UnlockGatePolicy.shouldRequestDismiss(true, true, false))
        // A successful dismissal can precede the gate regaining focus; that focus event must
        // retry launch, not issue another native dismissal request.
        assertFalse(UnlockGatePolicy.shouldRequestDismiss(true, false, true))
    }

    @Test
    fun remoteInputAlwaysWinsRouteSelection() {
        assertEquals(ReplyRoutes.RemoteInput, ReplyRoutes.select(true, true, true, "title-hash"))
        assertEquals(ReplyRoutes.RemoteInput, ReplyRoutes.select(true, false, false, ""))
        assertEquals(ReplyRoutes.Accessibility, ReplyRoutes.select(false, true, true, "title-hash"))
        assertEquals(null, ReplyRoutes.select(false, true, true, ""))
        assertEquals(null, ReplyRoutes.select(false, false, true, "title-hash"))
    }

    @Test
    fun pinValidatorAcceptsOnlyFourToSixteenAsciiDigits() {
        assertTrue(LockscreenPinStore.isValidPin("1234"))
        assertTrue(LockscreenPinStore.isValidPin("1234567890123456"))
        assertFalse(LockscreenPinStore.isValidPin("123"))
        assertFalse(LockscreenPinStore.isValidPin("12345678901234567"))
        assertFalse(LockscreenPinStore.isValidPin("１２３４"))
        assertFalse(LockscreenPinStore.isValidPin("12 34"))
    }

    @Test
    fun lockscreenRouteRequiresApi36AndCancellationIsTerminal() {
        assertFalse(LockscreenReplyPlatform.isEligible(35))
        assertTrue(LockscreenReplyPlatform.isEligible(36))
        val cancellation = ReplyCancellation()
        assertTrue(cancellation.canAct())
        assertTrue(cancellation.cancel("CALLER_INTERRUPTED"))
        assertFalse(cancellation.cancel("LATER_REASON"))
        assertFalse(cancellation.canAct())
        assertEquals("CALLER_INTERRUPTED", cancellation.stage())
        assertEquals(null, cancellation.runIfActive { "must-not-run" })
        assertFalse(cancellation.commitIfActive { true })

        val committed = ReplyCancellation()
        assertTrue(committed.commitIfActive { true })
        assertTrue(committed.isCommitted())
        assertFalse(committed.cancel("CALLER_INTERRUPTED"))
        assertTrue(committed.canAct())
        assertEquals(null, committed.stage())
    }

    @Test
    fun unlockTimeoutStageSeparatesGateAndPinProgress() {
        assertEquals("GATE_DISMISS_NOT_REQUESTED_TIMEOUT", AccessibilityReplyPolicy.unlockTimeoutStage(false, false))
        assertEquals("PIN_ENTRY_NOT_STARTED_TIMEOUT", AccessibilityReplyPolicy.unlockTimeoutStage(true, false))
        assertEquals("PIN_ENTRY_TIMEOUT", AccessibilityReplyPolicy.unlockTimeoutStage(true, true))
    }

    @Test
    fun inputObservationSendsOnlyExactTextBeforeDeadline() {
        assertEquals(InputTextObservation.Send, AccessibilityReplyPolicy.inputTextObservation(true, 100L, 1_000L))
        assertEquals(InputTextObservation.Wait, AccessibilityReplyPolicy.inputTextObservation(false, 999L, 1_000L))
        assertEquals(InputTextObservation.Fail, AccessibilityReplyPolicy.inputTextObservation(false, 1_000L, 1_000L))
    }

    @Test
    fun pinNodeSelectorRequiresExactSystemUiPackageViewIdAndLabel() {
        val pinAncestors = listOf("com.android.systemui:id/keyguard_pin_view")
        assertTrue(LockscreenReplySelectors.isExactSystemPinNode("com.android.systemui", "com.android.systemui:id/key7", listOf("7"), pinAncestors, '7'))
        assertTrue(LockscreenReplySelectors.isExactSystemPinNode("com.android.systemui", "com.android.systemui:id/key7", listOf("7"), listOf("com.android.systemui:id/pin_container"), '7'))
        assertFalse(LockscreenReplySelectors.isExactSystemPinNode("com.android.systemui", "com.android.systemui:id/key8", listOf("7"), pinAncestors, '7'))
        assertFalse(LockscreenReplySelectors.isExactSystemPinNode("vendor.systemui", "com.android.systemui:id/key7", listOf("7"), pinAncestors, '7'))
        assertFalse(LockscreenReplySelectors.isExactSystemPinNode("com.android.systemui", "com.android.systemui:id/key7", listOf("seven"), pinAncestors, '7'))
        assertFalse(LockscreenReplySelectors.isExactSystemPinNode("com.android.systemui", "com.android.systemui:id/key7", listOf("7"), emptyList(), '7'))
    }

    @Test
    fun targetAndVisibleTitleSelectionAreExactAndUnambiguous() {
        assertEquals("Alice Team", LockscreenReplySelectors.normalizeTitle("  Alice\n Team  "))
        assertTrue(LockscreenReplySelectors.titleMatches("expected", listOf("other", "expected")))
        assertFalse(LockscreenReplySelectors.titleMatches("expected", listOf("expected", "expected")))
        assertEquals(1, LockscreenReplySelectors.uniqueCandidateIndex(listOf(false, true, false)))
        assertEquals(null, LockscreenReplySelectors.uniqueCandidateIndex(listOf(true, true)))
        assertEquals(null, LockscreenReplySelectors.uniqueCandidateIndex(emptyList()))
        assertTrue(NotificationSnapshot.targetMatches("sbn", 999, "hash", "sbn", 999, "hash"))
        assertFalse(NotificationSnapshot.targetMatches("sbn", 999, "hash", "sbn", 0, "hash"))
        assertFalse(NotificationSnapshot.targetMatches("sbn", 999, "hash", "sbn", 999, "changed"))
        assertFalse(LockscreenReplySelectors.isUnambiguousChatSurface(true, 0))
        assertTrue(LockscreenReplySelectors.isUnambiguousChatSurface(true, 1))
        assertFalse(LockscreenReplySelectors.hasUniqueReadySendControl(0))
        assertTrue(LockscreenReplySelectors.hasUniqueReadySendControl(1))
        assertFalse(LockscreenReplySelectors.hasUniqueReadySendControl(2))
        assertFalse(LockscreenReplySelectors.isBottomComposerBounds(20, 100, 800, 180, 1_000))
        assertTrue(LockscreenReplySelectors.isBottomComposerBounds(20, 700, 800, 780, 1_000))
        assertFalse(LockscreenReplySelectors.isSameRowRightSend(20, 700, 800, 780, 100, 710, 180, 770))
        assertFalse(LockscreenReplySelectors.isSameRowRightSend(20, 700, 800, 780, 820, 500, 900, 600))
        assertTrue(LockscreenReplySelectors.isSameRowRightSend(20, 700, 800, 780, 820, 710, 900, 770))
    }

    @Test
    fun conversationSendAcceptsWechatLegacyMessageShapeButRejectsSummaries() {
        assertTrue(NotificationSnapshot.isConversationSendCandidate(Notification.CATEGORY_MESSAGE, false))
        assertFalse(NotificationSnapshot.isConversationSendCandidate(Notification.CATEGORY_MESSAGE, true))
        assertFalse(NotificationSnapshot.isConversationSendCandidate(Notification.CATEGORY_SERVICE, false))
    }

    @Test
    fun unlockedRunsNeverUsePinOrRelockButLockedRunsMustRelock() {
        assertTrue(AccessibilityReplyPolicy.canStart(false, true, false))
        assertFalse(AccessibilityReplyPolicy.canStart(true, true, false))
        assertTrue(AccessibilityReplyPolicy.canStart(true, true, true))
        assertFalse(AccessibilityReplyPolicy.requiresPin(false))
        assertFalse(AccessibilityReplyPolicy.requiresPinEntry(false))
        assertFalse(AccessibilityReplyPolicy.shouldRelock(false, false))
        assertTrue(AccessibilityReplyPolicy.finalized(false, false))
        assertTrue(AccessibilityReplyPolicy.requiresPin(true))
        assertTrue(AccessibilityReplyPolicy.requiresPinEntry(true))
        assertTrue(AccessibilityReplyPolicy.shouldRelock(true, false))
        assertFalse(AccessibilityReplyPolicy.finalized(true, false))
        assertTrue(AccessibilityReplyPolicy.finalized(true, true))
    }

    @Test
    fun homeReturnRequiresConfirmedSendAndKnownForegroundTransition() {
        val wechat = NotificationSnapshot.WechatPackage
        assertTrue(AccessibilityReplyPolicy.shouldRequestHome(false, "SENT_TO_WECHAT", "com.example.reader", wechat, wechat))
        assertFalse(AccessibilityReplyPolicy.shouldRequestHome(false, "FAILED", "com.example.reader", wechat, wechat))
        assertFalse(AccessibilityReplyPolicy.shouldRequestHome(false, "SENT_TO_WECHAT", wechat, wechat, wechat))
        assertFalse(AccessibilityReplyPolicy.shouldRequestHome(false, "SENT_TO_WECHAT", null, wechat, wechat))
        assertFalse(AccessibilityReplyPolicy.shouldRequestHome(false, "SENT_TO_WECHAT", "com.example.reader", "com.example.other", wechat))
        assertFalse(AccessibilityReplyPolicy.shouldRequestHome(false, "SENT_TO_WECHAT", "com.example.reader", null, wechat))
        assertFalse(AccessibilityReplyPolicy.shouldRequestHome(true, "SENT_TO_WECHAT", "com.example.reader", wechat, wechat))
    }

    @Test
    fun disarmedUnlockedAutomationRejectsActiveAndNotificationGoneTargets() {
        val activeNotificationAllowed = AccessibilityReplyPolicy.canStart(false, false, true)
        val notificationGoneAllowed = AccessibilityReplyPolicy.canStart(false, false, false)
        assertFalse(activeNotificationAllowed)
        assertFalse(notificationGoneAllowed)
    }

    @Test
    fun decryptedConversationTitleMustMatchThePersistedSaltedHash() {
        val salt = ByteArray(32) { it.toByte() }
        val expected = Privacy.saltedHash("Alice", salt)
        assertTrue(LockscreenReplySelectors.plaintextTitleMatches(expected, " Alice ", salt))
        assertFalse(LockscreenReplySelectors.plaintextTitleMatches(expected, "Bob", salt))
    }

    @Test
    fun replyCapabilityRequiresWechatCreatorAndFreeFormRemoteInput() {
        assertEquals(true, NotificationSnapshot.isReplyCapable(NotificationSnapshot.WechatPackage, listOf(false, true)))
        assertEquals(false, NotificationSnapshot.isReplyCapable(NotificationSnapshot.WechatPackage, listOf(false)))
        assertEquals(false, NotificationSnapshot.isReplyCapable("other.package", listOf(true)))
    }

    @Test
    fun replacementPairingResetsPairBoundState() {
        assertEquals(null, SyncStore.replacedDeviceId(null, "new-device"))
        assertEquals(null, SyncStore.replacedDeviceId("same-device", "same-device"))
        assertEquals("old-device", SyncStore.replacedDeviceId("old-device", "new-device"))
    }

    @Test
    fun pairingDeletesOnlyAnAliasThatWasReplaced() {
        assertFalse(SyncNetwork.shouldDeleteSigningAlias(null, "new-alias"))
        assertFalse(SyncNetwork.shouldDeleteSigningAlias("new-alias", "new-alias"))
        assertTrue(SyncNetwork.shouldDeleteSigningAlias("old-alias", "new-alias"))
    }

    @Test
    fun pairingGateAllowsOnlyOneInFlightAttempt() {
        val gate = PairingGate()
        assertTrue(gate.tryAcquire())
        assertFalse(gate.tryAcquire())
        gate.release()
        assertTrue(gate.tryAcquire())
        gate.release()
    }

    @Test
    fun pairingKeysAreClearedTogether() {
        val code = PairingCode("pair", "secret", 1L, ByteArray(32) { 1 }, ByteArray(32) { 2 })
        SyncNetwork.clearPairingKeys(code)
        assertTrue(code.a2i.all { it == 0.toByte() })
        assertTrue(code.i2a.all { it == 0.toByte() })
    }

    @Test
    fun decryptsSharedAesGcmVector() {
        val plaintext = AesGcmVectors.decryptUrlSafe(
            key = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
            iv = "ICEiIyQlJicoKSor",
            aad = "AWR1|A2I|019d2f1a-7b4c-7d10-8c21-1c77be6a91b0|phase0-ios|1|1788148800000",
            ciphertext = "qRjVFQL8f3w4RmCeqXmHnPB5zrCl4gyKFdRWMCr4MXoGo4ll4EJAu3OYTtMdWjKPoWfO8-jDVNOXIwf47l4m21dtWNg",
        )

        assertEquals("{\"sender\":\"Phase 0\",\"body\":\"cross-language AES-GCM\"}", plaintext)
    }

    @Test
    fun duplicateDetectorOnlyFlagsExactCallbacksInsideWindow() {
        val detector = DuplicateDetector(windowMillis = 750)

        assertFalse(detector.isDuplicateCandidate("same", 1_000))
        assertTrue(detector.isDuplicateCandidate("same", 1_749))
        assertFalse(detector.isDuplicateCandidate("same", 2_500))
        assertFalse(detector.isDuplicateCandidate("different", 2_501))
    }

    @Test
    fun phase1PreviewUsesUtf8BoundaryAndAadContract() {
        val preview = SyncProtocol.normalizePreview("发送者", "😀".repeat(500), "messaging")
        assertTrue(SyncProtocol.previewJson(preview).toByteArray(Charsets.UTF_8).size <= 600)
        val envelope = SyncProtocol.encrypt(
            ByteArray(32) { 7 },
            "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0",
            "abcdefghijklmnopQRSTUV",
            1,
            1_788_148_800_000,
            preview,
        )
        assertEquals("AWR1|A2I|019d2f1a-7b4c-7d10-8c21-1c77be6a91b0|abcdefghijklmnopQRSTUV|1|1788148800000", envelope.aad)
        assertEquals(12, SyncProtocol.decode(envelope.iv).size)
        assertFalse(SyncProtocol.envelopeJson(envelope).contains("😀"))
        val canonical = SyncProtocol.canonicalRequest("POST", "/api/v1/android/messages", "1788148800000", "nonce", "{}".toByteArray())
        assertEquals(5, canonical.lines().size)
        assertFalse(canonical.contains("\\n"))
    }

    @Test
    fun wechatProfileAllowlistAcceptsOnlyTwoMessageProfiles() {
        assertTrue(NotificationSnapshot.isAllowedWechatSource("com.tencent.mm", 0, "message_channel_new_id"))
        assertTrue(NotificationSnapshot.isAllowedWechatSource("com.tencent.mm", 999, "message_channel_new_id"))
        assertFalse(NotificationSnapshot.isAllowedWechatSource("com.tencent.mm", 10, "message_channel_new_id"))
        assertFalse(NotificationSnapshot.isAllowedWechatSource("com.tencent.mm", 0, "MigrationFSChannel"))
        assertFalse(NotificationSnapshot.isAllowedWechatSource("com.tencent.mobileqq", 0, "message_channel_new_id"))
    }

    @Test
    fun profileBoundAadKeepsLegacyHelpersAtUserZero() {
        val id = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0"
        val deviceId = "abcdefghijklmnopQRSTUV"
        val createdAt = 1_788_148_800_000L
        assertEquals("AWR1|A2I|$id|$deviceId|1|$createdAt", SyncProtocol.a2iAad(id, deviceId, 1, createdAt))
        assertEquals("AWR1|A2I|$id|$deviceId|1|$createdAt|0", SyncProtocol.a2iAad(id, deviceId, 1, createdAt, 0))
        assertEquals("AWR1|A2I_ASSET|$id|$deviceId|1|$createdAt|999", SyncProtocol.a2iAssetAad(id, deviceId, 1, createdAt, 999))
        assertEquals("AWR1|I2A|$id|$deviceId|target|$createdAt|999", SyncProtocol.i2aAad(id, deviceId, "target", createdAt, 999))
    }

    @Test
    fun duplicateIdentitiesAreSeparatedByWechatProfile() {
        val preview = Preview("微信", "same", "text")
        assertFalse(SyncProtocol.shortWindowDuplicateKey("sbn", 0, preview) == SyncProtocol.shortWindowDuplicateKey("sbn", 999, preview))
        assertFalse(SyncProtocol.notificationIdentity("sbn", 0, 100, preview) == SyncProtocol.notificationIdentity("sbn", 999, 100, preview))
    }

    @Test
    fun uploadHttpResultsRetryWithoutAnItemSpecificServerContract() {
        assertEquals(SyncNetwork.UploadResult.Complete, SyncNetwork.uploadResultForHttpStatus(200))
        assertEquals(SyncNetwork.UploadResult.Complete, SyncNetwork.uploadResultForHttpStatus(299))
        assertEquals(SyncNetwork.UploadResult.Retry, SyncNetwork.uploadResultForHttpStatus(408))
        assertEquals(SyncNetwork.UploadResult.Retry, SyncNetwork.uploadResultForHttpStatus(429))
        assertEquals(SyncNetwork.UploadResult.Retry, SyncNetwork.uploadResultForHttpStatus(500))
        assertEquals(SyncNetwork.UploadResult.Retry, SyncNetwork.uploadResultForHttpStatus(400))
        assertEquals(SyncNetwork.UploadResult.Retry, SyncNetwork.uploadResultForHttpStatus(404))
    }

    @Test
    fun assetEnvelopeBindsToQueueIdentityAndJsonHasNoPlaintext() {
        val assetId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1"
        val envelope = SyncProtocol.encryptAsset(ByteArray(32) { 9 }, assetId, "abcdefghijklmnopQRSTUV", 7, 1_788_148_800_000, byteArrayOf(1, 2, 3))
        assertEquals("AWR1|A2I_ASSET|$assetId|abcdefghijklmnopQRSTUV|7|1788148800000", envelope.aad)
        val json = SyncProtocol.assetJson(MediaAsset(assetId, "image", "image/jpeg", 1, 1, envelope))
        assertTrue(json.contains("phase1-asset"))
        assertFalse(json.contains("plaintext"))
    }

    @Test
    fun i2aReplyDecryptsOnlyWithItsExactAadBinding() {
        val key = ByteArray(32) { 3 }
        val replyId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b2"
        val deviceId = "abcdefghijklmnopQRSTUV"
        val targetMessageId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b3"
        val createdAt = 1_788_148_800_000L
        val aad = SyncProtocol.i2aAad(replyId, deviceId, targetMessageId, createdAt)
        val iv = ByteArray(12) { (it + 16).toByte() }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
            updateAAD(aad.toByteArray(StandardCharsets.UTF_8))
        }
        val envelope = Envelope(
            "A256GCM",
            "phase1-reply",
            SyncProtocol.encode(iv),
            aad,
            SyncProtocol.encode(cipher.doFinal("{\"body\":\"hello \\ud83d\\ude00\"}".toByteArray(StandardCharsets.UTF_8))),
        )

        assertEquals("hello 😀", SyncProtocol.decryptI2aReply(key, replyId, deviceId, targetMessageId, createdAt, envelope))
        try {
            SyncProtocol.decryptI2aReply(key, replyId, deviceId, "different-target", createdAt, envelope)
            throw AssertionError("expected AAD binding failure")
        } catch (_: IllegalArgumentException) {
        }
    }

    @Test
    fun profileBoundReplyRejectsAnotherWechatUser() {
        val key = ByteArray(32) { 4 }
        val replyId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b2"
        val deviceId = "abcdefghijklmnopQRSTUV"
        val targetMessageId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b3"
        val createdAt = 1_788_148_800_000L
        val aad = SyncProtocol.i2aAad(replyId, deviceId, targetMessageId, createdAt, 999)
        val iv = ByteArray(12) { (it + 20).toByte() }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
            updateAAD(aad.toByteArray(StandardCharsets.UTF_8))
        }
        val envelope = Envelope("A256GCM", "phase1-reply", SyncProtocol.encode(iv), aad, SyncProtocol.encode(cipher.doFinal("{\"body\":\"hello\"}".toByteArray(StandardCharsets.UTF_8))))

        assertEquals("hello", SyncProtocol.decryptI2aReply(key, replyId, deviceId, targetMessageId, createdAt, envelope, 999))
        try {
            SyncProtocol.decryptI2aReply(key, replyId, deviceId, targetMessageId, createdAt, envelope, 0)
            throw AssertionError("expected profile-bound AAD failure")
        } catch (_: IllegalArgumentException) {
        }
    }

    @Test
    fun conversationSendV3RejectsAadAndCiphertextTampering() {
        val key = ByteArray(32) { 5 }
        val pairId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b1"
        val replyId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b2"
        val deviceId = "abcdefghijklmnopQRSTUV"
        val targetMessageId = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b3"
        val createdAt = 1_788_148_800_000L
        val aad = SyncProtocol.i2aConversationSendAad(pairId, replyId, deviceId, targetMessageId, createdAt, 999)
        val iv = ByteArray(12) { (it + 32).toByte() }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
            updateAAD(aad.toByteArray(StandardCharsets.UTF_8))
        }
        val ciphertext = cipher.doFinal("{\"body\":\"hello\",\"conversationTitle\":\"Alice\"}".toByteArray(StandardCharsets.UTF_8))
        val envelope = Envelope("A256GCM", "phase1-reply", SyncProtocol.encode(iv), aad, SyncProtocol.encode(ciphertext))

        assertEquals(DecryptedReply("hello", "Alice"), SyncProtocol.decryptI2aConversationSend(key, pairId, replyId, deviceId, targetMessageId, createdAt, envelope, 999))
        try {
            SyncProtocol.decryptI2aConversationSend(key, "different-pair", replyId, deviceId, targetMessageId, createdAt, envelope, 999)
            throw AssertionError("expected v3 AAD binding failure")
        } catch (_: IllegalArgumentException) {
        }
        val tampered = ciphertext.copyOf().also { it[0] = (it[0].toInt() xor 1).toByte() }
        try {
            SyncProtocol.decryptI2aConversationSend(key, pairId, replyId, deviceId, targetMessageId, createdAt, envelope.copy(ct = SyncProtocol.encode(tampered)), 999)
            throw AssertionError("expected v3 ciphertext authentication failure")
        } catch (_: Exception) {
        }
    }

    @Test
    fun replyBodyRejectsBlankAndOverlongUnicodeText() {
        try {
            SyncProtocol.parseReplyBody("{\"body\":\"   \"}")
            throw AssertionError("expected blank reply rejection")
        } catch (_: IllegalArgumentException) {
        }
        try {
            SyncProtocol.parseReplyBody("{\"body\":\"${"😀".repeat(1_001)}\"}")
            throw AssertionError("expected reply length rejection")
        } catch (_: IllegalArgumentException) {
        }
    }

    @Test
    fun imageCaptureSelectsOnlyObservedLatestIncomingImageForExactSender() {
        val selection = ImageCapturePolicy.selectLatest(
            listOf(observedImageRow()),
            "枫",
            CaptureRect(0, 0, 1200, 2400),
            284,
            2200,
        )

        assertEquals(CaptureRect(0, 503, 1200, 1139), selection?.rowBounds)
        assertEquals(CaptureRect(189, 521, 462, 1121), selection?.imageBounds)
        assertEquals(CaptureRect(189, 521, 462, 1121), ImageCapturePolicy.mapCrop(selection!!.imageBounds, CaptureRect(0, 0, 1200, 2400), 1200, 2400))
    }

    @Test
    fun imageCaptureRejectsWrongSenderAndOutgoingRows() {
        val root = CaptureRect(0, 0, 1200, 2400)
        assertEquals(null, ImageCapturePolicy.selectLatest(listOf(observedImageRow()), "Alice", root, 284, 2200))

        val outgoing = observedImageRow().copy(
            images = listOf(imageNode(CaptureRect(738, 521, 1011, 1121))),
            avatars = listOf(avatarNode("枫头像", CaptureRect(1026, 503, 1182, 659))),
        )
        assertEquals(null, ImageCapturePolicy.selectLatest(listOf(outgoing), "枫", root, 284, 2200))
    }

    @Test
    fun imageCaptureRejectsWhenTheLatestVisibleMessageIsText() {
        val olderImage = observedImageRow().copy(bounds = CaptureRect(0, 503, 1200, 1139))
        val latestText = ImageCaptureRowSnapshot(
            ImageCapturePolicy.RowViewId,
            ImageCapturePolicy.RowClass,
            true,
            CaptureRect(0, 1140, 1200, 1320),
            containsText = true,
            images = emptyList(),
            avatars = listOf(avatarNode("枫头像", CaptureRect(18, 1140, 174, 1296))),
        )

        assertEquals(
            null,
            ImageCapturePolicy.selectLatest(listOf(olderImage, latestText), "枫", CaptureRect(0, 0, 1200, 2400), 284, 2200),
        )
    }

    @Test
    fun imageCaptureRejectsClippedAndAmbiguousImageRows() {
        val root = CaptureRect(0, 0, 1200, 2400)
        val clippedRow = observedImageRow().copy(
            bounds = CaptureRect(0, 260, 1200, 327),
            images = listOf(imageNode(CaptureRect(189, 284, 462, 327))),
            avatars = listOf(avatarNode("枫头像", CaptureRect(18, 260, 174, 327))),
        )
        assertEquals(null, ImageCapturePolicy.selectLatest(listOf(clippedRow), "枫", root, 284, 2200))

        val row = observedImageRow()
        assertEquals(null, ImageCapturePolicy.selectLatest(listOf(row.copy(images = row.images + row.images.single())), "枫", root, 284, 2200))
        assertEquals(null, ImageCapturePolicy.selectLatest(listOf(row.copy(avatars = row.avatars + row.avatars.single())), "枫", root, 284, 2200))
        assertEquals(null, ImageCapturePolicy.selectLatest(listOf(row, row), "枫", root, 284, 2200))
    }

    private fun observedImageRow(): ImageCaptureRowSnapshot = ImageCaptureRowSnapshot(
        ImageCapturePolicy.RowViewId,
        ImageCapturePolicy.RowClass,
        true,
        CaptureRect(0, 503, 1200, 1139),
        containsText = false,
        images = listOf(imageNode(CaptureRect(189, 521, 462, 1121))),
        avatars = listOf(avatarNode("枫头像", CaptureRect(18, 503, 174, 659))),
    )

    private fun imageNode(bounds: CaptureRect): ImageCaptureNodeSnapshot = ImageCaptureNodeSnapshot(
        ImageCapturePolicy.ImageViewId,
        ImageCapturePolicy.ImageClass,
        ImageCapturePolicy.ImageDescription,
        visible = true,
        clickable = true,
        bounds = bounds,
    )

    private fun avatarNode(description: String, bounds: CaptureRect): ImageCaptureNodeSnapshot = ImageCaptureNodeSnapshot(
        ImageCapturePolicy.AvatarViewId,
        "android.widget.ImageView",
        description,
        visible = true,
        clickable = true,
        bounds = bounds,
    )
}
