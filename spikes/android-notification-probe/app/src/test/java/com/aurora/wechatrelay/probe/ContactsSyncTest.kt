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

    @Test(expected = IllegalStateException::class)
    fun footerCountMismatchCannotPublish() {
        val assembly = ContactsPageAssembly()
        assembly.confirmTop()
        assembly.addStablePage(listOf("Alice"))
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
}
