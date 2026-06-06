package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.provider.CalendarContract
import android.util.Log
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID

private const val TAG = "RemindersHandler"

// CalendarContract Events URI
private val EVENTS_URI: Uri = CalendarContract.Events.CONTENT_URI

// eventType=2 → VTODO (task/reminder) — same constant as iOS Reminders
private const val EVENT_TYPE_TASK = 2

class RemindersHandler(private val context: Context) {

    private fun getIcsOutputFile(): File {
        val dir = File(context.getExternalFilesDir(null), "PhoneTransfer")
        if (!dir.exists()) dir.mkdirs()
        return File(dir, "reminders_import.ics")
    }

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["extract_reminders"] = { _, server ->
            extractReminders(server)
        }
    }

    fun registerInject(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["inject_reminders"] = { params, server ->
            @Suppress("UNCHECKED_CAST")
            val items = params["data"] as? List<Map<String, Any?>> ?: emptyList()
            injectReminders(items, server)
        }
    }

    // -----------------------------------------------------------------------
    // Extract
    // -----------------------------------------------------------------------

    private fun extractReminders(server: SocketServer): String {
        val reminders = mutableListOf<Map<String, Any?>>()

        val projection = arrayOf(
            CalendarContract.Events._ID,
            CalendarContract.Events.TITLE,
            CalendarContract.Events.DTSTART,
            CalendarContract.Events.DESCRIPTION,
            CalendarContract.Events.STATUS,
            CalendarContract.Events.HAS_ALARM
        )

        val selection = "eventType = $EVENT_TYPE_TASK"

        try {
            context.contentResolver.query(
                EVENTS_URI,
                projection,
                selection,
                null,
                "${CalendarContract.Events.DTSTART} ASC"
            )?.use { cursor ->
                val total = cursor.count
                var done = 0

                while (cursor.moveToNext()) {
                    val title = cursor.safeString(CalendarContract.Events.TITLE)
                    val dtstart = cursor.safeLong(CalendarContract.Events.DTSTART)
                        .takeIf { it > 0 }
                    val description = cursor.getString(
                        cursor.getColumnIndex(CalendarContract.Events.DESCRIPTION)
                    )
                    val status = cursor.getInt(
                        cursor.getColumnIndex(CalendarContract.Events.STATUS)
                    )
                    val hasAlarm = cursor.getInt(
                        cursor.getColumnIndex(CalendarContract.Events.HAS_ALARM)
                    ) != 0

                    // STATUS_CONFIRMED=0, STATUS_TENTATIVE=1, STATUS_CANCELED=2
                    // CalendarContract does not define a "completed" status; treat STATUS_CANCELED as done
                    // Some apps store completed tasks with status=2 (STATUS_CANCELED)
                    val completed = status == CalendarContract.Events.STATUS_CANCELED

                    reminders.add(
                        mapOf(
                            "title" to title,
                            "due" to dtstart,
                            "notes" to description,
                            "completed" to completed,
                            "list_name" to null,
                            "uid" to null,
                            "priority" to 0
                        )
                    )

                    done++
                    if (total > 0 && done % 20 == 0) server.sendProgress("reminders", done, total)
                }
                server.sendProgress("reminders", reminders.size, reminders.size)
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "Permission denied reading reminders: ${e.message}")
            return Response.error("extract_reminders", "permission_denied", e.message ?: "SecurityException")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to query CalendarContract events (VTODO): ${e.message}")
            // Note: many ROMs return 0 rows if no Tasks sync adapter is present — this is expected
            return Response.ok(
                "extract_reminders",
                mapOf("count" to 0, "reminders" to emptyList<Any>())
            )
        }

        return Response.ok(
            "extract_reminders",
            mapOf("count" to reminders.size, "reminders" to reminders)
        )
    }

    // -----------------------------------------------------------------------
    // Inject
    // -----------------------------------------------------------------------

    private fun injectReminders(items: List<Map<String, Any?>>, server: SocketServer): String {
        // Strategy 1: CalendarContract insert (requires a calendar account to insert into)
        val calendarResult = tryInjectCalendarContract(items, server)
        if (calendarResult != null) return calendarResult

        // Strategy 2: write VTODO .ics file to /sdcard for manual import
        return writeIcsFile(items, server)
    }

    private fun tryInjectCalendarContract(
        items: List<Map<String, Any?>>,
        server: SocketServer
    ): String? {
        // Resolve the first available calendar ID
        val calendarId = resolveCalendarId() ?: run {
            Log.w(TAG, "No writable calendar found — falling back to ICS export")
            return null
        }

        var inserted = 0
        var skipped = 0
        val total = items.size

        for ((index, reminder) in items.withIndex()) {
            val title = reminder["title"] as? String ?: run { skipped++; continue }
            val dtstart = (reminder["due"] as? Number)?.toLong() ?: System.currentTimeMillis()
            val description = reminder["notes"] as? String ?: ""
            val completed = reminder["completed"] as? Boolean ?: false

            try {
                val cv = ContentValues().apply {
                    put(CalendarContract.Events.CALENDAR_ID, calendarId)
                    put(CalendarContract.Events.TITLE, title)
                    put(CalendarContract.Events.DTSTART, dtstart)
                    put(CalendarContract.Events.DTEND, dtstart)
                    put(CalendarContract.Events.DESCRIPTION, description)
                    put(CalendarContract.Events.EVENT_TIMEZONE, TimeZone.getDefault().id)
                    put(CalendarContract.Events.ALL_DAY, 0)
                    // eventType=2 marks this as a VTODO/task
                    put("eventType", EVENT_TYPE_TASK)
                    put(
                        CalendarContract.Events.STATUS,
                        if (completed) CalendarContract.Events.STATUS_CANCELED
                        else CalendarContract.Events.STATUS_CONFIRMED
                    )
                }

                val result = context.contentResolver.insert(EVENTS_URI, cv)
                if (result != null) inserted++ else skipped++
            } catch (e: SecurityException) {
                Log.w(TAG, "Permission denied inserting reminder: ${e.message}")
                return null // Fall through to ICS
            } catch (e: Exception) {
                Log.e(TAG, "Failed to insert reminder '$title': ${e.message}")
                skipped++
            }

            if ((index + 1) % 20 == 0 || index == total - 1) {
                server.sendProgress("reminders", index + 1, total)
            }
        }

        return Response.ok(
            "inject_reminders",
            mapOf(
                "method" to "calendar_contract",
                "inserted" to inserted,
                "skipped" to skipped,
                "total" to total
            )
        )
    }

    private fun writeIcsFile(items: List<Map<String, Any?>>, server: SocketServer): String {
        val icsFile = getIcsOutputFile()

        val icsDateFmt = SimpleDateFormat("yyyyMMdd'T'HHmmss'Z'", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }
        val nowStamp = icsDateFmt.format(Date())

        var count = 0
        val total = items.size

        try {
            FileWriter(icsFile, Charsets.UTF_8).use { writer ->
                writer.write("BEGIN:VCALENDAR\r\n")
                writer.write("VERSION:2.0\r\n")
                writer.write("PRODID:-//PhoneTransfer//RemindersExport//EN\r\n")
                writer.write("CALSCALE:GREGORIAN\r\n")
                writer.write("METHOD:PUBLISH\r\n")

                for ((index, reminder) in items.withIndex()) {
                    val title = (reminder["title"] as? String ?: "Untitled").icsEscape()
                    val dueMs = (reminder["due"] as? Number)?.toLong()
                    val notes = (reminder["notes"] as? String ?: "").icsEscape()
                    val completed = reminder["completed"] as? Boolean ?: false
                    val priority = (reminder["priority"] as? Number)?.toInt() ?: 0
                    val uid = (reminder["uid"] as? String) ?: UUID.randomUUID().toString()

                    val dueStamp = if (dueMs != null && dueMs > 0) {
                        icsDateFmt.format(Date(dueMs))
                    } else null

                    writer.write("BEGIN:VTODO\r\n")
                    writer.write("UID:$uid\r\n")
                    writer.write("DTSTAMP:$nowStamp\r\n")
                    writer.write("SUMMARY:$title\r\n")
                    if (dueStamp != null) writer.write("DUE:$dueStamp\r\n")
                    if (notes.isNotBlank()) writer.write("DESCRIPTION:$notes\r\n")
                    writer.write("STATUS:${if (completed) "COMPLETED" else "NEEDS-ACTION"}\r\n")
                    writer.write("PRIORITY:$priority\r\n")
                    writer.write("END:VTODO\r\n")

                    count++
                    if ((index + 1) % 20 == 0 || index == total - 1) {
                        server.sendProgress("reminders", index + 1, total)
                    }
                }

                writer.write("END:VCALENDAR\r\n")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to write ICS file: ${e.message}")
            return Response.error("inject_reminders", "write_failed", e.message ?: "Unknown error")
        }

        return Response.ok(
            "inject_reminders",
            mapOf(
                "method" to "ics_file",
                "file" to icsFile.absolutePath,
                "count" to count
            )
        )
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    private fun resolveCalendarId(): Long? {
        val projection = arrayOf(CalendarContract.Calendars._ID, CalendarContract.Calendars.VISIBLE)
        return try {
            context.contentResolver.query(
                CalendarContract.Calendars.CONTENT_URI,
                projection,
                "${CalendarContract.Calendars.VISIBLE} = 1",
                null,
                null
            )?.use { cursor ->
                if (cursor.moveToFirst()) {
                    cursor.safeLong(CalendarContract.Calendars._ID)
                } else null
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to resolve calendar ID: ${e.message}")
            null
        }
    }

    private fun String.icsEscape(): String =
        replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")

    private fun android.database.Cursor.safeString(col: String): String? {
        val idx = getColumnIndex(col); return if (idx >= 0) getString(idx) else null
    }
    private fun android.database.Cursor.safeLong(col: String): Long {
        val idx = getColumnIndex(col); return if (idx >= 0) getLong(idx) else 0L
    }
    private fun android.database.Cursor.safeInt(col: String): Int {
        val idx = getColumnIndex(col); return if (idx >= 0) getInt(idx) else 0
    }
}
