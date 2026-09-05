package com.aurora.wechatrelay.probe

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.LinkedHashSet
import java.util.concurrent.TimeUnit
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.spec.GCMParameterSpec

@Entity(tableName = "sync_queue")
data class SyncQueueItem(
    @androidx.room.PrimaryKey val id: String,
    val deviceId: String,
    val seq: Long,
    val createdAt: Long,
    val envelope: String,
    val assetsJson: String,
    val wechatUserId: Int,
    val replyCapable: Boolean,
    val conversationSendCapable: Boolean,
    val state: String,
)

@Entity(tableName = "reply_targets")
data class ReplyTarget(
    @androidx.room.PrimaryKey val messageId: String,
    val sbnKey: String,
    val postTime: Long,
    val actionIndex: Int,
    val createdAt: Long,
    val wechatUserId: Int,
    val wechatUserSerial: Long,
    val route: String,
    val conversationTitleHash: String,
)
data class MessageEncryptionContext(val deviceId: String, val a2iKey: ByteArray)
data class RequestSigningContext(val deviceId: String, val signingAlias: String)
data class PendingReplyAck(val deviceId: String, val replyId: String, val status: String)

internal fun readQueuedAssetChunks(length: Int, read: (Int, Int) -> String?): String {
    require(length in 0..24 * 1024 * 1024)
    return buildString(length) {
        while (this.length < length) {
            val count = minOf(256 * 1024, length - this.length)
            val chunk = checkNotNull(read(this.length + 1, count))
            check(chunk.length == count)
            append(chunk)
        }
    }
}

@Dao
interface SyncQueueDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE) fun insert(item: SyncQueueItem): Long
    // Keep large ciphertext out of CursorWindow and load only the image currently being uploaded.
    @Query("SELECT id, deviceId, seq, createdAt, envelope, '' AS assetsJson, wechatUserId, replyCapable, conversationSendCapable, state FROM sync_queue WHERE deviceId = :deviceId AND state IN ('QUEUED', 'UPLOADING') ORDER BY seq") fun pending(deviceId: String): List<SyncQueueItem>
    @Query("SELECT length(assetsJson) FROM sync_queue WHERE deviceId = :deviceId AND id = :id") fun assetLength(deviceId: String, id: String): Int?
    @Query("SELECT substr(assetsJson, :start, :count) FROM sync_queue WHERE deviceId = :deviceId AND id = :id") fun assetChunk(deviceId: String, id: String, start: Int, count: Int): String?
    @Query("UPDATE sync_queue SET state = :state WHERE id = :id AND deviceId = :deviceId") fun state(deviceId: String, id: String, state: String)
    @Query("DELETE FROM sync_queue WHERE deviceId = :deviceId AND state = 'SERVER_ACCEPTED' AND createdAt < :cutoff") fun cleanupAccepted(deviceId: String, cutoff: Long)
    @Query("DELETE FROM sync_queue WHERE id IN (SELECT id FROM sync_queue WHERE deviceId = :deviceId AND state = 'SERVER_ACCEPTED' ORDER BY createdAt ASC LIMIT :count)") fun dropOldestAccepted(deviceId: String, count: Int)
    @Query("SELECT COUNT(*) FROM sync_queue WHERE deviceId = :deviceId") fun count(deviceId: String): Int
    @Query("UPDATE sync_queue SET deviceId = :deviceId WHERE deviceId = ''") fun bindLegacy(deviceId: String)
    @Query("DELETE FROM sync_queue WHERE deviceId = :deviceId") fun clear(deviceId: String)
}

@Dao
interface ReplyTargetDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE) fun insert(target: ReplyTarget)
    @Query("SELECT * FROM reply_targets WHERE messageId = :messageId") fun find(messageId: String): ReplyTarget?
    @Query("DELETE FROM reply_targets") fun clear()
}

@Database(entities = [SyncQueueItem::class, ReplyTarget::class], version = 9, exportSchema = false)
abstract class SyncDatabase : RoomDatabase() {
    abstract fun queue(): SyncQueueDao
    abstract fun replyTargets(): ReplyTargetDao
}

class SyncStore private constructor(context: Context) {
    private val appContext = context.applicationContext
    private val preferences = appContext.getSharedPreferences("phase1_sync", Context.MODE_PRIVATE)
    private val database = Room.databaseBuilder(appContext, SyncDatabase::class.java, "phase1-sync.db")
        .addMigrations(Migration1To2, Migration2To3, Migration3To4, Migration4To5, Migration5To6, Migration6To7, Migration7To8, Migration8To9)
        .build()
    private val queue = database.queue()
    private val replyTargets = database.replyTargets()

    @Synchronized
    fun paired(): Boolean {
        val alias = preferences.getString(SigningAlias, null) ?: return false
        if (preferences.getString(DeviceId, null) == null ||
            preferences.getString(WrappedA2i, null) == null ||
            preferences.getString(WrappedI2a, null) == null
        ) return false
        return try {
            val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
            keyStore.getKey(alias, null) is java.security.PrivateKey
        } catch (_: Exception) {
            false
        }
    }
    @Synchronized
    fun deviceId(): String = requireNotNull(preferences.getString(DeviceId, null))
    @Synchronized
    fun isCurrentDevice(expectedDeviceId: String): Boolean = preferences.getString(DeviceId, null) == expectedDeviceId
    @Synchronized
    fun messageEncryptionContext(expectedDeviceId: String): MessageEncryptionContext {
        check(isCurrentDevice(expectedDeviceId))
        return MessageEncryptionContext(expectedDeviceId, unwrap(requireNotNull(preferences.getString(WrappedA2i, null))))
    }
    @Synchronized
    fun requestSigningContext(expectedDeviceId: String? = null): RequestSigningContext {
        val deviceId = requireNotNull(preferences.getString(DeviceId, null))
        check(expectedDeviceId == null || expectedDeviceId == deviceId)
        return RequestSigningContext(deviceId, requireNotNull(preferences.getString(SigningAlias, null)))
    }
    @Synchronized
    fun signingAliasOrNull(): String? = preferences.getString(SigningAlias, null)
    @Synchronized
    fun nextSeq(expectedDeviceId: String): Long {
        check(isCurrentDevice(expectedDeviceId))
        val next = preferences.getLong(Sequence, 0L) + 1L
        check(preferences.edit().putLong(Sequence, next).commit())
        return next
    }
    @Synchronized
    fun enqueue(expectedDeviceId: String, id: String, seq: Long, createdAt: Long, envelope: String, assetsJson: String = "[]", wechatUserId: Int = 0, replyTarget: ReplyTarget? = null, conversationSendCapable: Boolean = false): Boolean {
        check(isCurrentDevice(expectedDeviceId))
        require(NotificationSnapshot.isAllowedWechatUserId(wechatUserId))
        require(replyTarget == null || replyTarget.wechatUserId == wechatUserId)
        require(!conversationSendCapable || replyTarget != null)
        var queued = false
        database.runInTransaction {
            queue.bindLegacy(expectedDeviceId)
            queue.cleanupAccepted(expectedDeviceId, System.currentTimeMillis() - TimeUnit.HOURS.toMillis(24))
            val overflow = queue.count(expectedDeviceId) - QueueCap + 1
            if (overflow > 0) queue.dropOldestAccepted(expectedDeviceId, overflow)
            if (queue.count(expectedDeviceId) < QueueCap && queue.insert(SyncQueueItem(id, expectedDeviceId, seq, createdAt, envelope, assetsJson, wechatUserId, replyTarget != null, conversationSendCapable, Queued)) != -1L) {
                replyTarget?.let(replyTargets::insert)
                queued = true
            }
        }
        return queued
    }
    @Synchronized
    fun pending(expectedDeviceId: String): List<SyncQueueItem> {
        check(isCurrentDevice(expectedDeviceId))
        queue.bindLegacy(expectedDeviceId)
        return queue.pending(expectedDeviceId)
    }
    @Synchronized
    fun pendingAssets(expectedDeviceId: String, id: String): String {
        check(isCurrentDevice(expectedDeviceId))
        return readQueuedAssetChunks(checkNotNull(queue.assetLength(expectedDeviceId, id))) { start, count ->
            queue.assetChunk(expectedDeviceId, id, start, count)
        }
    }
    @Synchronized
    fun state(expectedDeviceId: String, id: String, state: String) {
        check(isCurrentDevice(expectedDeviceId))
        queue.state(expectedDeviceId, id, state)
    }
    @Synchronized
    fun i2aKey(expectedDeviceId: String): ByteArray {
        check(isCurrentDevice(expectedDeviceId))
        return unwrap(requireNotNull(preferences.getString(WrappedI2a, null)))
    }
    @Synchronized
    fun replyTarget(messageId: String): ReplyTarget? = replyTargets.find(messageId)
    @Synchronized
    fun pendingReplyAck(expectedDeviceId: String): PendingReplyAck? {
        check(isCurrentDevice(expectedDeviceId))
        val deviceId = preferences.getString(PendingReplyDeviceId, null) ?: return null
        if (deviceId != expectedDeviceId) return null
        val replyId = preferences.getString(PendingReplyId, null) ?: return null
        val status = preferences.getString(PendingReplyStatus, null) ?: return null
        return PendingReplyAck(deviceId, replyId, status)
    }
    @Synchronized
    fun savePendingReplyAck(expectedDeviceId: String, replyId: String, status: String) {
        check(isCurrentDevice(expectedDeviceId))
        val pending = pendingReplyAck(expectedDeviceId)
        check(pending == null || pending.replyId == replyId)
        check(preferences.edit().putString(PendingReplyDeviceId, expectedDeviceId).putString(PendingReplyId, replyId).putString(PendingReplyStatus, status).commit())
    }
    @Synchronized
    fun clearPendingReplyAck(expectedDeviceId: String, replyId: String) {
        check(isCurrentDevice(expectedDeviceId))
        if (preferences.getString(PendingReplyDeviceId, null) != expectedDeviceId) return
        if (preferences.getString(PendingReplyId, null) != replyId) return
        check(preferences.edit().remove(PendingReplyDeviceId).remove(PendingReplyId).remove(PendingReplyStatus).commit())
    }
    @Synchronized
    fun hasQueuedNotificationIdentity(expectedDeviceId: String, identity: String): Boolean {
        check(isCurrentDevice(expectedDeviceId))
        return preferences.getStringSet(QueuedNotificationIdentities, emptySet())?.contains(identity) == true
    }

    @Synchronized
    fun markQueuedNotificationIdentity(expectedDeviceId: String, identity: String) {
        check(isCurrentDevice(expectedDeviceId))
        val identities = LinkedHashSet(preferences.getStringSet(QueuedNotificationIdentities, emptySet()).orEmpty())
        identities.add(identity)
        while (identities.size > MaxQueuedNotificationIdentities) identities.iterator().also { it.next(); it.remove() }
        check(preferences.edit().putStringSet(QueuedNotificationIdentities, identities).commit())
    }

    @Synchronized
    fun <T> withCurrentDevice(expectedDeviceId: String, action: () -> T): T? =
        if (isCurrentDevice(expectedDeviceId)) action() else null

    fun verifyKeyWrapping() {
        val raw = ByteArray(32).also(SecureRandom()::nextBytes)
        val unwrapped = unwrap(wrap(raw))
        try {
            check(MessageDigest.isEqual(raw, unwrapped))
        } finally {
            raw.fill(0)
            unwrapped.fill(0)
        }
    }

    @Synchronized
    fun savePairing(deviceId: String, signingAlias: String, a2i: ByteArray, i2a: ByteArray) {
        val wrappedA2i = wrap(a2i)
        val wrappedI2a = wrap(i2a)
        val replacedDeviceId = replacedDeviceId(preferences.getString(DeviceId, null), deviceId)
        if (replacedDeviceId != null) queue.bindLegacy(replacedDeviceId)
        check(
            preferences.edit()
                .putString(DeviceId, deviceId)
                .putString(SigningAlias, signingAlias)
                .putString(WrappedA2i, wrappedA2i)
                .putString(WrappedI2a, wrappedI2a)
                .remove(Sequence)
                .remove(QueuedNotificationIdentities)
                .remove(PendingReplyDeviceId)
                .remove(PendingReplyId)
                .remove(PendingReplyStatus)
                .commit(),
        )
        if (replacedDeviceId != null) runCatching {
            database.runInTransaction {
                queue.clear(replacedDeviceId)
                replyTargets.clear()
            }
        }
    }

    private fun wrap(raw: ByteArray): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, wrappingKey())
        return SyncProtocol.encode(cipher.iv + cipher.doFinal(raw))
    }
    private fun unwrap(value: String): ByteArray {
        val stored = SyncProtocol.decode(value); require(stored.size > 28)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, wrappingKey(), GCMParameterSpec(128, stored.copyOfRange(0, 12)))
        return cipher.doFinal(stored.copyOfRange(12, stored.size))
    }
    private fun wrappingKey(): javax.crypto.SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (keyStore.getKey(WrapAlias, null) as? javax.crypto.SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(WrapAlias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setKeySize(256).build())
        }.generateKey()
    }

    companion object {
        const val Queued = "QUEUED"; const val Uploading = "UPLOADING"; const val ServerAccepted = "SERVER_ACCEPTED"
        private const val QueueCap = 1000; private const val DeviceId = "device_id"; private const val Sequence = "seq"; private const val SigningAlias = "signing_alias"; private const val WrappedA2i = "wrapped_a2i"; private const val WrappedI2a = "wrapped_i2a"; private const val QueuedNotificationIdentities = "queued_notification_identities"; private const val PendingReplyDeviceId = "pending_reply_device_id"; private const val PendingReplyId = "pending_reply_id"; private const val PendingReplyStatus = "pending_reply_status"; private const val WrapAlias = "awrelay_phase1_wrap"
        // ponytail: arbitrary old identities may be evicted at 64; upgrade to Room UNIQUE identity if replay volume exceeds it.
        private const val MaxQueuedNotificationIdentities = 64
        private val Migration1To2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE sync_queue ADD COLUMN assetsJson TEXT NOT NULL DEFAULT '[]'")
            }
        }
        private val Migration2To3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("CREATE TABLE IF NOT EXISTS reply_targets (messageId TEXT NOT NULL, sbnKey TEXT NOT NULL, postTime INTEGER NOT NULL, actionIndex INTEGER NOT NULL, createdAt INTEGER NOT NULL, PRIMARY KEY(messageId))")
            }
        }
        private val Migration3To4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE sync_queue ADD COLUMN wechatUserId INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE reply_targets ADD COLUMN wechatUserId INTEGER NOT NULL DEFAULT 0")
            }
        }
        private val Migration4To5 = object : Migration(4, 5) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE sync_queue ADD COLUMN replyCapable INTEGER NOT NULL DEFAULT 0")
            }
        }
        private val Migration5To6 = object : Migration(5, 6) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE sync_queue ADD COLUMN deviceId TEXT NOT NULL DEFAULT ''")
            }
        }
        private val Migration6To7 = object : Migration(6, 7) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE reply_targets ADD COLUMN route TEXT NOT NULL DEFAULT 'REMOTE_INPUT'")
                db.execSQL("ALTER TABLE reply_targets ADD COLUMN conversationTitleHash TEXT NOT NULL DEFAULT ''")
            }
        }
        private val Migration7To8 = object : Migration(7, 8) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE sync_queue ADD COLUMN conversationSendCapable INTEGER NOT NULL DEFAULT 0")
            }
        }
        private val Migration8To9 = object : Migration(8, 9) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE reply_targets ADD COLUMN wechatUserSerial INTEGER NOT NULL DEFAULT -1")
            }
        }
        internal fun replacedDeviceId(existingDeviceId: String?, newDeviceId: String): String? =
            existingDeviceId?.takeUnless { it == newDeviceId }
        @Volatile private var instance: SyncStore? = null
        fun get(context: Context): SyncStore = instance ?: synchronized(this) { instance ?: SyncStore(context).also { instance = it } }
    }
}
