package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.util.Log
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response

private const val TAG = "BlockedHandler"

// Blocked number provider URIs to try in order
private val BLOCKED_READ_URIS = listOf(
    "content://call_log/call_log_blocked",
    "content://com.android.phone.blockednumber/blockednum"
)

// Preferred injection URI (BlockedNumberContract standard)
private const val BLOCKED_INJECT_URI = "content://com.android.phone.blockednumber/blockednum"

class BlockedHandler(private val context: Context) {

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["extract_blocked"] = { _, server ->
            extractBlocked(server)
        }
    }

    fun registerInject(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["inject_blocked"] = { params, server ->
            @Suppress("UNCHECKED_CAST")
            val items = params["data"] as? List<Map<String, Any?>> ?: emptyList()
            injectBlocked(items, server)
        }
    }

    // -----------------------------------------------------------------------
    // Extract
    // -----------------------------------------------------------------------

    private fun extractBlocked(server: SocketServer): String {
        val numbers = mutableListOf<Map<String, Any?>>()

        val (providerUri, numberCol, nameCol) = resolveBlockedUri()
            ?: run {
                Log.w(TAG, "No accessible blocked-number provider found")
                return Response.ok(
                    "extract_blocked",
                    mapOf("count" to 0, "blocked" to emptyList<Any>())
                )
            }

        val projection = buildList {
            add(numberCol)
            if (nameCol != null) add(nameCol)
        }.toTypedArray()

        try {
            context.contentResolver.query(
                Uri.parse(providerUri), projection, null, null, null
            )?.use { cursor ->
                val total = cursor.count
                var done = 0
                while (cursor.moveToNext()) {
                    val numIdx = cursor.getColumnIndex(numberCol)
                    if (numIdx < 0) continue
                    val number = cursor.getString(numIdx)
                    val name = nameCol?.let {
                        val idx = cursor.getColumnIndex(it)
                        if (idx >= 0) cursor.getString(idx) else null
                    }
                    numbers.add(mapOf("number" to number, "name" to name))
                    done++
                    if (total > 0 && done % 20 == 0) server.sendProgress("blocked", done, total)
                }
                server.sendProgress("blocked", numbers.size, numbers.size)
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "Permission denied reading blocked numbers: ${e.message}")
            return Response.error("extract_blocked", "permission_denied", e.message ?: "SecurityException")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to query blocked provider '$providerUri': ${e.message}")
            return Response.error("extract_blocked", "query_failed", e.message ?: "Unknown error")
        }

        return Response.ok("extract_blocked", mapOf("count" to numbers.size, "blocked" to numbers))
    }

    // -----------------------------------------------------------------------
    // Inject
    // -----------------------------------------------------------------------

    private fun injectBlocked(items: List<Map<String, Any?>>, server: SocketServer): String {
        val uri = Uri.parse(BLOCKED_INJECT_URI)
        var inserted = 0
        var skipped = 0
        val total = items.size

        for ((index, item) in items.withIndex()) {
            val number = item["number"] as? String
            if (number.isNullOrBlank()) {
                skipped++
                continue
            }

            try {
                val cv = ContentValues().apply {
                    // BlockedNumberContract.BlockedNumbers.COLUMN_ORIGINAL_NUMBER = "original_number"
                    put("original_number", number)
                }
                val result = context.contentResolver.insert(uri, cv)
                if (result != null) inserted++ else skipped++
            } catch (e: SecurityException) {
                Log.w(TAG, "SecurityException inserting blocked number '$number': ${e.message}")
                skipped++
            } catch (e: Exception) {
                Log.e(TAG, "Failed to insert blocked number '$number': ${e.message}")
                skipped++
            }

            if ((index + 1) % 20 == 0 || index == total - 1) {
                server.sendProgress("blocked", index + 1, total)
            }
        }

        return Response.ok(
            "inject_blocked",
            mapOf("inserted" to inserted, "skipped" to skipped, "total" to total)
        )
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    /**
     * Try each known URI in order. Returns a Triple of (uri, numberColumn, nameColumn?)
     * for the first one that responds, or null if none work.
     */
    private fun resolveBlockedUri(): Triple<String, String, String?>? {
        // call_log_blocked uses "number" and "name" columns
        // blockednumber uses "original_number" (no name column)
        val candidates = listOf(
            Triple("content://call_log/call_log_blocked", "number", "name"),
            Triple("content://com.android.phone.blockednumber/blockednum", "original_number", null)
        )

        for ((uriStr, numCol, nameCol) in candidates) {
            try {
                val projection = listOfNotNull(numCol, nameCol).toTypedArray()
                val cursor = context.contentResolver.query(
                    Uri.parse(uriStr), projection, null, null, null
                )
                if (cursor != null) {
                    cursor.close()
                    Log.i(TAG, "Using blocked provider: $uriStr")
                    return Triple(uriStr, numCol, nameCol)
                }
            } catch (_: Exception) {
                // Try next
            }
        }
        return null
    }
}
