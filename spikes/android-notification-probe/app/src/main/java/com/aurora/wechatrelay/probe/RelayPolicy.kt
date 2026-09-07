package com.aurora.wechatrelay.probe

import org.json.JSONObject
import java.util.Calendar
import java.util.TimeZone

data class RelayPolicy(
    val enabled: Boolean,
    val scheduleEnabled: Boolean,
    val weekdaysMask: Int,
    val startMinutes: Int,
    val endMinutes: Int,
    val updatedAt: Long,
) {
    fun active(nowMillis: Long = System.currentTimeMillis()): Boolean {
        if (!enabled) return false
        if (!scheduleEnabled) return true
        val calendar = Calendar.getInstance(ChinaTimeZone).apply { timeInMillis = nowMillis }
        val day = if (calendar.get(Calendar.DAY_OF_WEEK) == Calendar.SUNDAY) 7 else calendar.get(Calendar.DAY_OF_WEEK) - 1
        val minutes = calendar.get(Calendar.HOUR_OF_DAY) * 60 + calendar.get(Calendar.MINUTE)
        return weekdaysMask and (1 shl (day - 1)) == 0 || minutes < startMinutes || minutes >= endMinutes
    }

    companion object {
        val Default = RelayPolicy(true, false, 31, 570, 1080, 0L)
        private val ChinaTimeZone = TimeZone.getTimeZone("Asia/Shanghai")

        fun fromJson(value: JSONObject): RelayPolicy {
            val weekdays = value.getJSONArray("weekdays")
            var mask = 0
            for (index in 0 until weekdays.length()) {
                val day = weekdays.getInt(index)
                require(day in 1..7)
                mask = mask or (1 shl (day - 1))
            }
            val start = parseTime(value.getString("start"))
            val end = parseTime(value.getString("end"))
            require(mask != 0 && start < end && value.getString("timezone") == "Asia/Shanghai")
            return RelayPolicy(value.getBoolean("enabled"), value.getBoolean("scheduleEnabled"), mask, start, end, value.getLong("updatedAt"))
        }

        private fun parseTime(value: String): Int {
            require(Regex("^(?:[01]\\d|2[0-3]):[0-5]\\d$").matches(value))
            val parts = value.split(':')
            return parts[0].toInt() * 60 + parts[1].toInt()
        }
    }
}
