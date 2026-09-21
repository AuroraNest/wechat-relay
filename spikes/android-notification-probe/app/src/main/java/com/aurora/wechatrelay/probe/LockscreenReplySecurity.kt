package com.aurora.wechatrelay.probe

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.spec.GCMParameterSpec

object ReplyRoutes {
    const val RemoteInput = "REMOTE_INPUT"
    const val Accessibility = "ACCESSIBILITY"

    internal fun select(
        hasRemoteInput: Boolean,
        accessibilityArmed: Boolean,
        hasWechatContentIntent: Boolean,
        conversationTitleHash: String,
    ): String? = when {
        hasRemoteInput -> RemoteInput
        accessibilityArmed && hasWechatContentIntent && conversationTitleHash.isNotBlank() -> Accessibility
        else -> null
    }
}

object LockscreenReplyPlatform {
    internal fun isEligible(sdkInt: Int): Boolean = sdkInt >= 36
}

internal enum class InputTextObservation { Send, Wait, Fail }

object AccessibilityReplyPolicy {
    internal fun canStart(startedLocked: Boolean, automationEnabled: Boolean, fullyArmed: Boolean): Boolean =
        automationEnabled && (!startedLocked || fullyArmed)

    internal fun requiresPin(startedLocked: Boolean): Boolean = startedLocked

    internal fun requiresPinEntry(deviceLocked: Boolean): Boolean = deviceLocked
    internal fun shouldRelock(startedLocked: Boolean, currentlyLocked: Boolean): Boolean = startedLocked && !currentlyLocked
    internal fun finalized(startedLocked: Boolean, currentlyLocked: Boolean): Boolean = !startedLocked || currentlyLocked
    internal fun unlockTimeoutStage(gateDismissRequested: Boolean, pinEntryStarted: Boolean): String = when {
        !gateDismissRequested -> "GATE_DISMISS_NOT_REQUESTED_TIMEOUT"
        !pinEntryStarted -> "PIN_ENTRY_NOT_STARTED_TIMEOUT"
        else -> "PIN_ENTRY_TIMEOUT"
    }
    internal fun inputTextObservation(exact: Boolean, nowMillis: Long, deadlineMillis: Long): InputTextObservation = when {
        exact -> InputTextObservation.Send
        nowMillis < deadlineMillis -> InputTextObservation.Wait
        else -> InputTextObservation.Fail
    }
    internal fun shortWechatWindowTimeoutApplies(hasContentIntent: Boolean, isConversationSearch: Boolean): Boolean =
        hasContentIntent || !isConversationSearch
    internal fun shouldRequestHome(
        startedLocked: Boolean,
        terminalStatus: String,
        initialForegroundPackage: String?,
        currentForegroundPackage: String?,
        wechatPackage: String,
    ): Boolean = !startedLocked && terminalStatus == "SENT_TO_WECHAT" &&
        initialForegroundPackage != null && initialForegroundPackage != wechatPackage &&
        currentForegroundPackage == wechatPackage
}

internal class ReplyCancellation {
    private var reason: String? = null
    private var committed = false
    @Synchronized fun cancel(stage: String): Boolean {
        if (committed || reason != null) return false
        reason = stage
        return true
    }
    @Synchronized fun canAct(): Boolean = reason == null
    @Synchronized fun stage(): String? = reason
    @Synchronized fun isCommitted(): Boolean = committed
    @Synchronized fun <T> runIfActive(action: () -> T): T? = if (reason == null) action() else null
    @Synchronized fun commitIfActive(action: () -> Boolean): Boolean {
        if (reason != null) return false
        val succeeded = action()
        if (succeeded) committed = true
        return succeeded
    }
}

object LockscreenReplySelectors {
    internal fun normalizeTitle(value: CharSequence?): String = value
        ?.toString()
        ?.trim()
        ?.replace(Regex("\\s+"), " ")
        .orEmpty()

    internal fun titleMatches(expectedHash: String, visibleTitleHashes: List<String>): Boolean =
        expectedHash.isNotBlank() && visibleTitleHashes.count { it == expectedHash } == 1

    internal fun plaintextTitleMatches(expectedHash: String, title: CharSequence, salt: ByteArray): Boolean {
        val normalized = normalizeTitle(title)
        return expectedHash.isNotBlank() && normalized.isNotEmpty() && Privacy.saltedHash(normalized, salt) == expectedHash
    }

    internal fun uniqueCandidateIndex(matches: List<Boolean>): Int? {
        val indexes = matches.indices.filter { matches[it] }
        return indexes.singleOrNull()
    }

    internal fun isUnambiguousChatSurface(titleMatches: Boolean, editableInputs: Int): Boolean =
        titleMatches && editableInputs == 1

    internal fun hasUniqueReadySendControl(sendControls: Int): Boolean = sendControls == 1

    internal fun isBottomComposerBounds(left: Int, top: Int, right: Int, bottom: Int, screenHeight: Int): Boolean =
        screenHeight > 0 && left >= 0 && right > left && bottom > top &&
            top >= screenHeight * 55 / 100 && bottom <= screenHeight

    internal fun isSameRowRightSend(
        inputLeft: Int,
        inputTop: Int,
        inputRight: Int,
        inputBottom: Int,
        sendLeft: Int,
        sendTop: Int,
        sendRight: Int,
        sendBottom: Int,
    ): Boolean = inputRight > inputLeft && inputBottom > inputTop &&
        sendRight > sendLeft && sendBottom > sendTop && sendLeft >= inputRight &&
        minOf(inputBottom, sendBottom) > maxOf(inputTop, sendTop)

    internal fun isExactSystemPinNode(
        packageName: CharSequence?,
        viewIdResourceName: String?,
        labels: List<CharSequence>,
        ancestorViewIds: List<String>,
        digit: Char,
    ): Boolean = packageName?.toString() == "com.android.systemui" &&
        viewIdResourceName == "com.android.systemui:id/key$digit" &&
        ancestorViewIds.any { it == "com.android.systemui:id/keyguard_pin_view" || it == "com.android.systemui:id/pin_container" } &&
        labels.any { normalizeTitle(it) == digit.toString() }
}

class LockscreenPinStore(context: Context) {
    private val appContext = context.applicationContext
    private val preferences = appContext.getSharedPreferences(PreferencesName, Context.MODE_PRIVATE)

    data class Status(
        val buildEnabled: Boolean,
        val platformEligible: Boolean,
        val pinStored: Boolean,
        val attemptBlocked: Boolean,
        val explicitlyEnabled: Boolean,
        val accessibilityLive: Boolean,
        val userUnlocked: Boolean,
    ) {
        val armed: Boolean
            get() = buildEnabled && platformEligible && pinStored && !attemptBlocked && explicitlyEnabled && accessibilityLive && userUnlocked
    }

    data class DisarmResult(val localStateDeleted: Boolean, val keyDeleted: Boolean)

    fun status(accessibilityLive: Boolean, userUnlocked: Boolean): Status = withAuthorizationLock {
        Status(
            buildEnabled = BuildConfig.LOCKSCREEN_ACCESSIBILITY_REPLY,
            platformEligible = LockscreenReplyPlatform.isEligible(Build.VERSION.SDK_INT),
            pinStored = hasEncryptedPin(),
            attemptBlocked = preferences.getBoolean(AttemptInProgress, false),
            explicitlyEnabled = preferences.getBoolean(ExplicitOptIn, false),
            accessibilityLive = accessibilityLive,
            userUnlocked = userUnlocked,
        )
    }

    fun automationEnabled(): Boolean = withAuthorizationLock {
        BuildConfig.LOCKSCREEN_ACCESSIBILITY_REPLY &&
            LockscreenReplyPlatform.isEligible(Build.VERSION.SDK_INT) &&
            preferences.getBoolean(ExplicitOptIn, false) &&
            hasEncryptedPin()
    }

    fun saveAndArm(pin: CharArray): Boolean = withAuthorizationLock {
        if (!BuildConfig.LOCKSCREEN_ACCESSIBILITY_REPLY || !LockscreenReplyPlatform.isEligible(Build.VERSION.SDK_INT) || !isValidPin(pin)) return@withAuthorizationLock false
        var plaintext: ByteArray? = null
        var encrypted: ByteArray? = null
        try {
            plaintext = pin.concatToString().toByteArray(Charsets.US_ASCII)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, encryptionKey())
            encrypted = cipher.doFinal(plaintext)
            check(
                preferences.edit()
                    .putString(EncryptedPin, SyncProtocol.encode(encrypted))
                    .putString(PinIv, SyncProtocol.encode(cipher.iv))
                    .putBoolean(ExplicitOptIn, true)
                    .remove(AttemptInProgress)
                    .commit(),
            )
            true
        } finally {
            plaintext?.fill(0)
            encrypted?.fill(0)
        }
    }

    fun beginAttempt(): CharArray? = withAuthorizationLock {
        if (!BuildConfig.LOCKSCREEN_ACCESSIBILITY_REPLY ||
            !LockscreenReplyPlatform.isEligible(Build.VERSION.SDK_INT) ||
            !preferences.getBoolean(ExplicitOptIn, false) ||
            preferences.getBoolean(AttemptInProgress, false) ||
            !hasEncryptedPin()
        ) return@withAuthorizationLock null
        val encrypted = runCatching { SyncProtocol.decode(requireNotNull(preferences.getString(EncryptedPin, null))) }.getOrNull()
            ?: return@withAuthorizationLock null
        val iv = runCatching { SyncProtocol.decode(requireNotNull(preferences.getString(PinIv, null))) }.getOrNull()
            ?: return@withAuthorizationLock null
        var plaintext: ByteArray? = null
        try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, encryptionKey(), GCMParameterSpec(128, iv))
            plaintext = cipher.doFinal(encrypted)
            val pin = CharArray(plaintext.size) { plaintext[it].toInt().toChar() }
            if (!isValidPin(pin)) {
                pin.fill('\u0000')
                return@withAuthorizationLock null
            }
            // Persist before the first digit. Every non-success path deliberately leaves this marker set.
            if (!preferences.edit().putBoolean(AttemptInProgress, true).commit()) {
                pin.fill('\u0000')
                return@withAuthorizationLock null
            }
            pin
        } finally {
            encrypted.fill(0)
            iv.fill(0)
            plaintext?.fill(0)
        }
    }

    fun completeSuccessfulFinalization(): Boolean = withAuthorizationLock {
        if (!preferences.getBoolean(AttemptInProgress, false)) return@withAuthorizationLock false
        preferences.edit().remove(AttemptInProgress).commit()
    }

    fun disarmAndDelete(): DisarmResult = withAuthorizationLock {
        val localStateDeleted = preferences.edit()
            .remove(EncryptedPin)
            .remove(PinIv)
            .remove(ExplicitOptIn)
            .remove(AttemptInProgress)
            .commit()
        val keyDeleted = try {
            val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
            if (keyStore.containsAlias(KeyAlias)) keyStore.deleteEntry(KeyAlias)
            !keyStore.containsAlias(KeyAlias)
        } catch (_: Exception) {
            false
        }
        DisarmResult(localStateDeleted, keyDeleted)
    }

    private fun hasEncryptedPin(): Boolean =
        !preferences.getString(EncryptedPin, null).isNullOrBlank() &&
            !preferences.getString(PinIv, null).isNullOrBlank()

    private fun encryptionKey(): javax.crypto.SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (keyStore.getKey(KeyAlias, null) as? javax.crypto.SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(
                KeyGenParameterSpec.Builder(KeyAlias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .setUserAuthenticationRequired(false)
                    .setUnlockedDeviceRequired(false)
                    .build(),
            )
        }.generateKey()
    }

    companion object {
        private val AuthorizationLock = Any()
        internal fun <T> withAuthorizationLock(action: () -> T): T = synchronized(AuthorizationLock) { action() }

        internal fun isValidPin(pin: CharSequence): Boolean =
            pin.length in 4..16 && pin.all { it in '0'..'9' }

        internal fun isValidPin(pin: CharArray): Boolean =
            pin.size in 4..16 && pin.all { it in '0'..'9' }

        private const val PreferencesName = "lockscreen_reply_security"
        private const val EncryptedPin = "encrypted_pin"
        private const val PinIv = "pin_iv"
        private const val ExplicitOptIn = "explicit_opt_in"
        private const val AttemptInProgress = "attempt_in_progress"
        private const val KeyAlias = "awrelay_lockscreen_pin_v1"
    }
}
