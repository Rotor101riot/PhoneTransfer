package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.util.Log
import com.google.gson.Gson
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response

private const val TAG = "AlarmsHandler"

// Known alarm content provider URIs (AOSP DeskClock and Google DeskClock)
private val ALARM_URIS = listOf(
    "content://com.android.deskclock/alarm",
    "content://com.google.android.deskclock/alarm"
)

// daysofweek bitmask: bit 0 = Monday … bit 6 = Sunday (ISO 0-based)
private fun bitmaskToRepeatDays(bitmask: Int): List<Int> {
    val days = mutableListOf<Int>()
    for (bit in 0..6) {
        if (bitmask and (1 shl bit) != 0) days.add(bit)
    }
    return days
}

private fun repeatDaysToBitmask(days: List<*>): Int {
    var mask = 0
    for (d in days) {
        val day = when (d) {
            is Number -> d.toInt()
            is String -> d.toIntOrNull() ?: continue
            else -> continue
        }
        if (day in 0..6) mask = mask or (1 shl day)
    }
    return mask
}

class AlarmsHandler(private val context: Context) {

    private val gson = Gson()

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["extract_alarms"] = { params, server ->
            extractAlarms(server)
        }
    }

    fun registerInject(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["inject_alarms"] = { params, server ->
            @Suppress("UNCHECKED_CAST")
            val items = params["data"] as? List<Map<String, Any?>> ?: emptyList()
            injectAlarms(items, server)
        }
    }

    // -----------------------------------------------------------------------
    // Extract
    // -----------------------------------------------------------------------

    private fun extractAlarms(server: SocketServer): String {
        val alarms = mutableListOf<Map<String, Any?>>()

        val providerUri = resolveAlarmUri() ?: run {
            Log.w(TAG, "No accessible alarm provider found")
            return Response.ok("extract_alarms", mapOf("count" to 0, "alarms" to emptyList<Any>()))
        }

        val projection = arrayOf("_id", "hour", "minutes", "label", "enabled", "daysofweek", "ringtone")

        try {
            context.contentResolver.query(
                Uri.parse(providerUri), projection, null, null, null
            )?.use { cursor ->
                val total = cursor.count
                var done = 0
                while (cursor.moveToNext()) {
                    val hourIdx = cursor.getColumnIndex("hour")
                    val minIdx = cursor.getColumnIndex("minutes")
                    if (hourIdx < 0 || minIdx < 0) continue
                    val hour = cursor.getInt(hourIdx)
                    val minutes = cursor.getInt(minIdx)

                    val labelIdx   = cursor.getColumnIndex("label")
                    val enabledIdx = cursor.getColumnIndex("enabled")
                    val daysIdx    = cursor.getColumnIndex("daysofweek")
                    val ringIdx    = cursor.getColumnIndex("ringtone")

                    val label    = if (labelIdx   >= 0) cursor.getString(labelIdx) ?: "" else ""
                    val enabled  = if (enabledIdx >= 0) cursor.getInt(enabledIdx) != 0  else true
                    val bitmask  = if (daysIdx    >= 0) cursor.getInt(daysIdx)          else 0
                    val ringtone = if (ringIdx    >= 0) cursor.getString(ringIdx) ?: "Default ringtone"
                                  else "Default ringtone"

                    alarms.add(
                        mapOf(
                            "hour" to hour,
                            "minute" to minutes,
                            "label" to label,
                            "enabled" to enabled,
                            "repeat_days" to bitmaskToRepeatDays(bitmask),
                            "sound" to ringtone
                        )
                    )
                    done++
                    if (total > 0 && done % 5 == 0) server.sendProgress("alarms", done, total)
                }
                server.sendProgress("alarms", alarms.size, alarms.size)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to query alarm provider '$providerUri': ${e.message}")
            return Response.error("extract_alarms", "query_failed", e.message ?: "Unknown error")
        }

        return Response.ok("extract_alarms", mapOf("count" to alarms.size, "alarms" to alarms))
    }

    // -----------------------------------------------------------------------
    // Inject
    // -----------------------------------------------------------------------

    private fun injectAlarms(items: List<Map<String, Any?>>, server: SocketServer): String {
        val providerUri = resolveAlarmUri() ?: run {
            Log.w(TAG, "No alarm provider found for injection")
            return Response.error("inject_alarms", "no_provider", "No accessible alarm content provider found")
        }

        val uri = Uri.parse(providerUri)
        var inserted = 0
        var skipped = 0
        val total = items.size

        for ((index, alarm) in items.withIndex()) {
            try {
                val hour = (alarm["hour"] as? Number)?.toInt() ?: continue
                val minute = (alarm["minute"] as? Number)?.toInt() ?: 0
                val label = alarm["label"] as? String ?: ""
                val enabled = alarm["enabled"] as? Boolean ?: true
                @Suppress("UNCHECKED_CAST")
                val repeatDays = alarm["repeat_days"] as? List<*> ?: emptyList<Any>()
                val bitmask = repeatDaysToBitmask(repeatDays)
                val sound = alarm["sound"] as? String ?: ""

                val cv = ContentValues().apply {
                    put("hour", hour)
                    put("minutes", minute)
                    put("label", label)
                    put("enabled", if (enabled) 1 else 0)
                    put("daysofweek", bitmask)
                    if (sound.isNotBlank()) put("ringtone", sound)
                }

                val result = context.contentResolver.insert(uri, cv)
                if (result != null) inserted++ else {
                    Log.w(TAG, "Alarm provider rejected insert for hour=$hour min=$minute — " +
                            "many ROM clock apps block external inserts")
                    skipped++
                }
            } catch (e: SecurityException) {
                Log.w(TAG, "SecurityException inserting alarm: ${e.message}")
                skipped++
            } catch (e: Exception) {
                Log.e(TAG, "Failed to insert alarm: ${e.message}")
                skipped++
            }

            if ((index + 1) % 5 == 0 || index == total - 1) {
                server.sendProgress("alarms", index + 1, total)
            }
        }

        return Response.ok(
            "inject_alarms",
            mapOf("inserted" to inserted, "skipped" to skipped, "total" to total)
        )
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    private fun resolveAlarmUri(): String? {
        for (uriStr in ALARM_URIS) {
            try {
                val cursor = context.contentResolver.query(
                    Uri.parse(uriStr), arrayOf("_id"), null, null, null
                )
                if (cursor != null) {
                    cursor.close()
                    Log.i(TAG, "Using alarm provider: $uriStr")
                    return uriStr
                }
            } catch (_: Exception) {
                // Try next URI
            }
        }
        return null
    }
}
