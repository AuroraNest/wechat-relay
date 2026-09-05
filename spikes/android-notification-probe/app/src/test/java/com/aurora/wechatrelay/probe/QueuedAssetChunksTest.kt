package com.aurora.wechatrelay.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class QueuedAssetChunksTest {
    @Test fun largeCiphertextIsReassembledWithoutOversizedCursorRows() {
        val source = "[\"" + "abcd0123".repeat(1_500_000) + "\"]"
        var largestRead = 0
        val restored = readQueuedAssetChunks(source.length) { start, count ->
            largestRead = maxOf(largestRead, count)
            source.substring(start - 1, start - 1 + count)
        }
        assertEquals(source, restored)
        assertTrue(largestRead <= 256 * 1024)
        assertEquals("[]", readQueuedAssetChunks(2) { _, _ -> "[]" })
    }

    @Test(expected = IllegalStateException::class)
    fun truncatedCiphertextIsNotUploaded() {
        readQueuedAssetChunks(20) { _, _ -> "short" }
    }
}
