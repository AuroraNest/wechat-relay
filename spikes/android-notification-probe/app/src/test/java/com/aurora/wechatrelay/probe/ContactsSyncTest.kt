package com.aurora.wechatrelay.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ContactsSyncTest {
    @Test
    fun stablePagesDeduplicateOverlapAndRequireTopAndFooterProof() {
        val assembly = ContactsPageAssembly()
        assembly.addStablePage(listOf(" Alice ", "Bob"))
        assembly.addStablePage(listOf("Bob", "Cara"))
        assembly.observeFooter(3)
        assertFalse(ContactsScanPolicy.hasCompleteProof(false, 3, assembly.count()))
        assembly.confirmTop()
        assertEquals(listOf("Alice", "Bob", "Cara"), assembly.complete())
    }

    @Test(expected = IllegalArgumentException::class)
    fun contactsOverTheNameByteLimitFailWithoutTruncation() {
        ContactsPageAssembly().addStablePage(listOf("a".repeat(513)))
    }

    @Test
    fun systemEntriesDoNotInflateTheWechatFriendTotal() {
        val friends = (1..33).map { "Friend $it" }
        val assembly = ContactsPageAssembly()
        assembly.confirmTop()
        assembly.addStablePage(friends.take(20) + " 文件传输助手 ")
        assembly.addStablePage(friends.drop(15) + "微信团队")
        assembly.observeFooter(33)
        assertEquals(33, assembly.count())
        assertEquals(friends, assembly.complete())
    }

    @Test(expected = IllegalStateException::class)
    fun footerCountMismatchCannotPublish() {
        val assembly = ContactsPageAssembly()
        assembly.confirmTop()
        assembly.addStablePage(listOf("Alice", "微信团队", "文件传输助手"))
        assembly.observeFooter(2)
        assembly.complete()
    }

    @Test
    fun pageMustRemainStableForTheMinimumIntervalBeforeItIsConsumed() {
        assertFalse(ContactsScanPolicy.stableFor(7, 1_000L, 7, 1_299L, 300L))
        assertTrue(ContactsScanPolicy.stableFor(7, 1_000L, 7, 1_300L, 300L))
        assertFalse(ContactsScanPolicy.stableFor(8, 1_000L, 7, 2_000L, 300L))
    }

    @Test
    fun contactsEnvelopeMatchesSharedFixedVector() {
        val key = SyncProtocol.decode("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
        val id = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0"
        val envelope = SyncProtocol.encryptContacts(
            key = key,
            id = id,
            deviceId = "device-id-0000001",
            capturedAt = 1788148800000,
            wechatUserId = 999,
            contacts = listOf("测试好友 A", "Example B"),
            iv = SyncProtocol.decode("ICEiIyQlJicoKSor"),
        )
        assertEquals("AWR1|A2I_CONTACTS|1|019d2f1a-7b4c-7d10-8c21-1c77be6a91b0|device-id-0000001|1788148800000|999", envelope.aad)
        assertEquals("qRjQUlapNix5Eyy6oHuAivJzt-el7gKDCdRWMK8_1eHaGwChM8C6UTS8TO90cj2mg0eJrKAeaBmvDw9xKZJI_6V7sPJGZLLukD9mRDX-i9uukg8", envelope.ct)
    }

    @Test
    fun freshContactsSnapshotsUseV2AadWithoutChangingPlaintextSchema() {
        val id = "019d2f1a-7b4c-7d10-8c21-1c77be6a91b0"
        val envelope = SyncProtocol.encryptContactsV2(
            key = ByteArray(32) { 1 },
            id = id,
            deviceId = "device-id-0000001",
            capturedAt = 1788148800000,
            wechatUserId = 999,
            contacts = listOf("测试好友 A"),
            iv = ByteArray(12) { it.toByte() },
        )

        assertEquals("AWR1|A2I_CONTACTS|2|$id|device-id-0000001|1788148800000|999", envelope.aad)
    }
}
