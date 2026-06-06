package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.provider.CalendarContract
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.phonetransfer.companion.protocol.Response
import com.phonetransfer.companion.SocketServer

class CalendarHandler(private val context: Context) {

    private val gson = Gson()

    companion object {
        private const val PHONETRANSFER_CALENDAR_NAME = "PhoneTransfer"
        private const val PHONETRANSFER_ACCOUNT_NAME = "phonetransfer_local"
        private const val PHONETRANSFER_ACCOUNT_TYPE = "LOCAL"
    }

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_calendar"] = { cmd, server ->
            handleExtract(cmd, server)
        }
    }

    fun registerInject(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["inject_calendar"] = { cmd, server ->
            handleInject(cmd, server)
        }
    }

    private suspend fun handleExtract(cmd: Map<String, Any?>, server: SocketServer): String {
        val events = mutableListOf<Map<String, Any?>>()

        val cursor = context.contentResolver.query(
            CalendarContract.Events.CONTENT_URI,
            arrayOf(
                CalendarContract.Events.TITLE,
                CalendarContract.Events.DTSTART,
                CalendarContract.Events.DTEND,
                CalendarContract.Events.ALL_DAY,
                CalendarContract.Events.EVENT_LOCATION,
                CalendarContract.Events.DESCRIPTION,
                CalendarContract.Events.RRULE,
                CalendarContract.Events._SYNC_ID
            ),
            "${CalendarContract.Events.DELETED} = 0",
            null,
            "${CalendarContract.Events.DTSTART} ASC"
        ) ?: return Response.error("extract_calendar", "QUERY_FAILED", "Failed to query calendar events")

        var processed = 0
        val total = cursor.count

        cursor.use {
            while (it.moveToNext()) {
                val title = it.safeString(CalendarContract.Events.TITLE)
                val dtStart = it.safeLong(CalendarContract.Events.DTSTART)
                val dtEnd = it.safeLongOrNull(CalendarContract.Events.DTEND)
                val allDay = it.safeInt(CalendarContract.Events.ALL_DAY) != 0
                val location = it.safeString(CalendarContract.Events.EVENT_LOCATION)
                val description = it.safeString(CalendarContract.Events.DESCRIPTION)
                val rrule = it.safeString(CalendarContract.Events.RRULE)
                val syncId = it.safeString(CalendarContract.Events._SYNC_ID)

                events.add(
                    mapOf(
                        "title" to (title ?: ""),
                        "start" to dtStart,
                        "end" to dtEnd,
                        "all_day" to allDay,
                        "uid" to syncId,
                        "location" to location,
                        "notes" to description,
                        "recurrence_rule" to rrule
                    )
                )

                processed++
                if (processed % 50 == 0) {
                    server.sendProgress("calendar", processed, total)
                }
            }
        }

        server.sendProgress("calendar", total, total)

        val payload = mapOf(
            "category" to "calendar",
            "count" to events.size,
            "data" to events
        )
        return Response.ok("extract_calendar", payload)
    }

    private suspend fun handleInject(cmd: Map<String, Any?>, server: SocketServer): String {
        @Suppress("UNCHECKED_CAST")
        val dataRaw = cmd["data"] as? List<*>
            ?: return Response.error("inject_calendar", "MISSING_DATA", "No data array provided")

        val dataType = object : TypeToken<List<Map<String, Any?>>>() {}.type
        val dataJson = gson.toJson(dataRaw)
        val events: List<Map<String, Any?>> = gson.fromJson(dataJson, dataType)

        val calendarId = findOrCreatePhoneTransferCalendar()
            ?: return Response.error(
                "inject_calendar",
                "CALENDAR_UNAVAILABLE",
                "Could not find or create the PhoneTransfer local calendar"
            )

        val total = events.size
        var injected = 0
        var failed = 0

        events.forEachIndexed { index, event ->
            try {
                val title = event["title"] as? String ?: ""
                val start = (event["start"] as? Number)?.toLong() ?: System.currentTimeMillis()
                val end = (event["end"] as? Number)?.toLong()
                val allDay = event["all_day"] as? Boolean ?: false
                val location = event["location"] as? String
                val notes = event["notes"] as? String
                val rrule = event["recurrence_rule"] as? String

                val values = ContentValues().apply {
                    put(CalendarContract.Events.CALENDAR_ID, calendarId)
                    put(CalendarContract.Events.TITLE, title)
                    put(CalendarContract.Events.DTSTART, start)
                    put(CalendarContract.Events.ALL_DAY, if (allDay) 1 else 0)
                    put(CalendarContract.Events.EVENT_TIMEZONE, "UTC")

                    if (!rrule.isNullOrEmpty()) {
                        // Recurring events MUST use DURATION, not DTEND (CalendarProvider rejects DTEND+RRULE)
                        val durationMs = if (end != null && end > start) end - start else 3_600_000L
                        val durationSec = durationMs / 1000
                        put(CalendarContract.Events.DURATION, "P${durationSec}S")
                    } else if (end != null) {
                        put(CalendarContract.Events.DTEND, end)
                    } else {
                        put(CalendarContract.Events.DTEND, start + 3_600_000L)
                    }

                    if (!location.isNullOrEmpty()) {
                        put(CalendarContract.Events.EVENT_LOCATION, location)
                    }
                    if (!notes.isNullOrEmpty()) {
                        put(CalendarContract.Events.DESCRIPTION, notes)
                    }
                    if (!rrule.isNullOrEmpty()) {
                        put(CalendarContract.Events.RRULE, rrule)
                    }
                }

                val result = context.contentResolver.insert(
                    CalendarContract.Events.CONTENT_URI,
                    values
                )
                if (result != null) injected++ else failed++
            } catch (e: SecurityException) {
                failed++
            } catch (e: Exception) {
                failed++
            }

            val processed = index + 1
            if (processed % 50 == 0) {
                server.sendProgress("calendar", processed, total)
            }
        }

        server.sendProgress("calendar", total, total)

        val payload = mapOf(
            "category" to "calendar",
            "injected" to injected,
            "failed" to failed,
            "calendar_id" to calendarId
        )
        return Response.ok("inject_calendar", payload)
    }

    /**
     * Finds the existing PhoneTransfer local calendar, or creates one if it does not exist.
     * Returns the calendar _ID, or null if creation failed.
     */
    private fun findOrCreatePhoneTransferCalendar(): Long? {
        // Search for existing PhoneTransfer calendar
        val cursor = context.contentResolver.query(
            CalendarContract.Calendars.CONTENT_URI,
            arrayOf(CalendarContract.Calendars._ID, CalendarContract.Calendars.NAME),
            "${CalendarContract.Calendars.NAME} = ? AND ${CalendarContract.Calendars.ACCOUNT_TYPE} = ?",
            arrayOf(PHONETRANSFER_CALENDAR_NAME, PHONETRANSFER_ACCOUNT_TYPE),
            null
        )

        cursor?.use {
            if (it.moveToFirst()) {
                return it.safeLong(CalendarContract.Calendars._ID)
            }
        }

        // Calendar not found — create it
        return createPhoneTransferCalendar()
    }

    private fun createPhoneTransferCalendar(): Long? {
        val values = ContentValues().apply {
            put(CalendarContract.Calendars.NAME, PHONETRANSFER_CALENDAR_NAME)
            put(CalendarContract.Calendars.CALENDAR_DISPLAY_NAME, PHONETRANSFER_CALENDAR_NAME)
            put(CalendarContract.Calendars.ACCOUNT_NAME, PHONETRANSFER_ACCOUNT_NAME)
            put(CalendarContract.Calendars.ACCOUNT_TYPE, PHONETRANSFER_ACCOUNT_TYPE)
            put(CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL, CalendarContract.Calendars.CAL_ACCESS_OWNER)
            put(CalendarContract.Calendars.OWNER_ACCOUNT, PHONETRANSFER_ACCOUNT_NAME)
            put(CalendarContract.Calendars.VISIBLE, 1)
            put(CalendarContract.Calendars.SYNC_EVENTS, 1)
            put(CalendarContract.Calendars.CALENDAR_COLOR, 0xFF4285F4.toInt())
        }

        val calSyncUri = CalendarContract.Calendars.CONTENT_URI.buildUpon()
            .appendQueryParameter(CalendarContract.CALLER_IS_SYNCADAPTER, "true")
            .appendQueryParameter(CalendarContract.Calendars.ACCOUNT_NAME, PHONETRANSFER_ACCOUNT_NAME)
            .appendQueryParameter(CalendarContract.Calendars.ACCOUNT_TYPE, PHONETRANSFER_ACCOUNT_TYPE)
            .build()

        return try {
            val result = context.contentResolver.insert(calSyncUri, values) ?: return null
            result.lastPathSegment?.toLongOrNull()
        } catch (e: Exception) {
            null
        }
    }

    private fun android.database.Cursor.safeString(col: String): String? {
        val idx = getColumnIndex(col); return if (idx >= 0) getString(idx) else null
    }
    private fun android.database.Cursor.safeLong(col: String): Long {
        val idx = getColumnIndex(col); return if (idx >= 0) getLong(idx) else 0L
    }
    private fun android.database.Cursor.safeInt(col: String): Int {
        val idx = getColumnIndex(col); return if (idx >= 0) getInt(idx) else 0
    }
    private fun android.database.Cursor.safeLongOrNull(col: String): Long? {
        val idx = getColumnIndex(col); return if (idx >= 0 && !isNull(idx)) getLong(idx) else null
    }
}
