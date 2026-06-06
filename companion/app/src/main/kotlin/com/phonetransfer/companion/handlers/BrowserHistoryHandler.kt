package com.phonetransfer.companion.handlers

import android.content.Context
import android.database.Cursor
import android.net.Uri
import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response

private const val TAG = "BrowserHistoryHandler"

private val HISTORY_URIS = listOf(
    "content://com.android.chrome.browser/history",
    "content://com.android.browser/bookmarks"
)

class BrowserHistoryHandler(private val context: Context) {

    private val gson = Gson()

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_browser_history"] = { cmd, server ->
            handleExtract(server)
        }
    }

    fun registerInject(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["inject_browser_history"] = { cmd, server ->
            handleInject(cmd, server)
        }
    }

    private suspend fun handleExtract(server: SocketServer): String {
        val entries = mutableListOf<Map<String, Any?>>()

        for (uriStr in HISTORY_URIS) {
            val result = tryExtractFromUri(Uri.parse(uriStr), server)
            if (result != null) {
                entries.addAll(result)
                break
            }
        }

        if (entries.isEmpty()) {
            Log.i(TAG, "No accessible browser history provider found")
        }

        server.sendProgress("browser_history", entries.size, entries.size)

        val payload = mapOf(
            "category" to "browser_history",
            "count" to entries.size,
            "data" to entries,
            "note" to if (entries.isEmpty())
                "Browser history extraction requires Chrome content provider access"
            else null
        )
        return Response.ok("extract_browser_history", payload)
    }

    private fun tryExtractFromUri(
        uri: Uri,
        server: SocketServer
    ): List<Map<String, Any?>>? {
        val projection = arrayOf("_id", "title", "url", "visits", "date")

        // For the legacy browser provider, history entries have bookmark=0
        val selection = if (uri.toString().contains("browser/bookmarks")) "bookmark = 0" else null

        val cursor: Cursor
        try {
            cursor = context.contentResolver.query(
                uri, projection, selection, null, "date DESC"
            ) ?: return null
        } catch (e: SecurityException) {
            Log.w(TAG, "SecurityException querying $uri: ${e.message}")
            return null
        } catch (e: Exception) {
            Log.w(TAG, "Failed to query $uri: ${e.message}")
            return null
        }

        val entries = mutableListOf<Map<String, Any?>>()
        var processed = 0

        cursor.use {
            val total = it.count
            while (it.moveToNext()) {
                val id = it.safeLong("_id")
                val title = it.safeString("title") ?: ""
                val url = it.safeString("url") ?: ""
                val visitCount = it.safeInt("visits")
                val lastVisited = it.safeLong("date")

                if (url.isNotBlank()) {
                    entries.add(
                        mapOf(
                            "id" to id,
                            "title" to title,
                            "url" to url,
                            "visit_count" to visitCount,
                            "last_visited" to lastVisited
                        )
                    )
                }

                processed++
                if (processed % 100 == 0) {
                    server.sendProgress("browser_history", processed, total)
                }
            }
        }

        Log.i(TAG, "Extracted ${entries.size} history entries from $uri")
        return entries
    }

    private suspend fun handleInject(cmd: Map<String, Any?>, server: SocketServer): String {
        @Suppress("UNCHECKED_CAST")
        val dataRaw = cmd["data"] as? List<*>
            ?: return Response.error("inject_browser_history", "MISSING_DATA", "No data array provided")

        val dataType = object : TypeToken<List<Map<String, Any?>>>() {}.type
        val dataJson = gson.toJson(dataRaw)
        val entries: List<Map<String, Any?>> = gson.fromJson(dataJson, dataType)

        // Browser history injection is very limited on modern Android.
        // We acknowledge receipt so the PC side can store it.
        val payload = mapOf(
            "category" to "browser_history",
            "received" to entries.size,
            "note" to "Browser history injection is not supported on modern Android; data preserved for reference"
        )
        return Response.ok("inject_browser_history", payload)
    }

    private fun Cursor.safeString(col: String): String? {
        val idx = getColumnIndex(col); return if (idx >= 0) getString(idx) else null
    }
    private fun Cursor.safeLong(col: String): Long {
        val idx = getColumnIndex(col); return if (idx >= 0) getLong(idx) else 0L
    }
    private fun Cursor.safeInt(col: String): Int {
        val idx = getColumnIndex(col); return if (idx >= 0) getInt(idx) else 0
    }
}
