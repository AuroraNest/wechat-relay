package com.aurora.wechatrelay.probe

class DuplicateDetector(private val windowMillis: Long = 750L) {
    private val seenAt = mutableMapOf<String, Long>()

    @Synchronized
    fun isDuplicateCandidate(fingerprint: String, observedAtMillis: Long): Boolean {
        val previous = seenAt[fingerprint]
        seenAt[fingerprint] = observedAtMillis
        seenAt.entries.removeIf { (_, at) -> observedAtMillis - at > windowMillis }
        return previous != null && observedAtMillis - previous in 0..windowMillis
    }
}
