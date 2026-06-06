package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.provider.CallLog
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.phonetransfer.companion.protocol.Response
import com.phonetransfer.companion.SocketServer

class CallLogHandler(private val context: Context) {

    private val gson = Gson()

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_call_log"] = { cmd, server ->
            handleExtract(cmd, server)
        }
    }

    fun registerInject(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["inject_call_log"] = { cmd, server ->
            handleInject(cmd, server)
        }
    }

    private suspend fun handleExtract(cmd: Map<String, Any?>, server: SocketServer): String {
        val calls = mutableListOf<Map<String, Any?>>()

        val cursor = context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            arrayOf(
                CallLog.Calls.NUMBER,
                CallLog.Calls.DATE,
                CallLog.Calls.DURATION,
                CallLog.Calls.TYPE,
                CallLog.Calls.CACHED_NAME
            ),
            null,
            null,
            "${CallLog.Calls.DATE} ASC"
        ) ?: return Response.error("extract_call_log", "QUERY_FAILED", "Failed to query call log")

        var processed = 0
        val total = cursor.count

        cursor.use {
            while (it.moveToNext()) {
                val number = it.safeString(CallLog.Calls.NUMBER) ?: ""
                val date = it.safeLong(CallLog.Calls.DATE)
                val duration = it.safeLong(CallLog.Calls.DURATION)
                val type = it.safeInt(CallLog.Calls.TYPE)
                val cachedName = it.safeString(CallLog.Calls.CACHED_NAME)

                calls.add(
                    mapOf(
                        "number" to number,
                        "timestamp" to date,
                        "duration_seconds" to duration,
                        "call_type" to mapCallType(type),
                        "name" to cachedName
                    )
                )

                processed++
                if (processed % 50 == 0) {
                    server.sendProgress("call_log", processed, total)
                }
            }
        }

        server.sendProgress("call_log", total, total)

        val payload = mapOf(
            "category" to "call_log",
            "count" to calls.size,
            "data" to calls
        )
        return Response.ok("extract_call_log", payload)
    }

    private suspend fun handleInject(cmd: Map<String, Any?>, server: SocketServer): String {
        @Suppress("UNCHECKED_CAST")
        val dataRaw = cmd["data"] as? List<*>
            ?: return Response.error("inject_call_log", "MISSING_DATA", "No data array provided")

        val dataType = object : TypeToken<List<Map<String, Any?>>>() {}.type
        val dataJson = gson.toJson(dataRaw)
        val calls: List<Map<String, Any?>> = gson.fromJson(dataJson, dataType)

        val total = calls.size
        var injected = 0
        var failed = 0

        calls.forEachIndexed { index, call ->
            try {
                val number = call["number"] as? String ?: ""
                val timestamp = (call["timestamp"] as? Number)?.toLong() ?: System.currentTimeMillis()
                val durationSeconds = (call["duration_seconds"] as? Number)?.toLong() ?: 0L
                val callTypeStr = call["call_type"] as? String ?: "incoming"
                val name = call["name"] as? String

                val callTypeInt = mapCallTypeToInt(callTypeStr)

                val values = ContentValues().apply {
                    put(CallLog.Calls.NUMBER, number)
                    put(CallLog.Calls.DATE, timestamp)
                    put(CallLog.Calls.DURATION, durationSeconds)
                    put(CallLog.Calls.TYPE, callTypeInt)
                    if (!name.isNullOrEmpty()) {
                        put(CallLog.Calls.CACHED_NAME, name)
                    }
                }

                val result = context.contentResolver.insert(CallLog.Calls.CONTENT_URI, values)
                if (result != null) injected++ else failed++
            } catch (e: SecurityException) {
                failed++
            } catch (e: Exception) {
                failed++
            }

            val processed = index + 1
            if (processed % 50 == 0) {
                server.sendProgress("call_log", processed, total)
            }
        }

        server.sendProgress("call_log", total, total)

        val payload = mapOf(
            "category" to "call_log",
            "injected" to injected,
            "failed" to failed
        )
        return Response.ok("inject_call_log", payload)
    }

    /**
     * Maps Android CallLog type integer to a human-readable string.
     * 1 = incoming, 2 = outgoing, 3 = missed, anything else defaults to "incoming".
     */
    private fun mapCallType(type: Int): String {
        return when (type) {
            CallLog.Calls.INCOMING_TYPE -> "incoming"
            CallLog.Calls.OUTGOING_TYPE -> "outgoing"
            CallLog.Calls.MISSED_TYPE -> "missed"
            else -> "incoming"
        }
    }

    /**
     * Maps a human-readable call type string back to the Android CallLog integer constant.
     */
    private fun mapCallTypeToInt(callType: String): Int {
        return when (callType.lowercase()) {
            "outgoing" -> CallLog.Calls.OUTGOING_TYPE
            "missed" -> CallLog.Calls.MISSED_TYPE
            else -> CallLog.Calls.INCOMING_TYPE
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
}
