package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.util.Log
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.File
import java.io.FileWriter
import java.util.concurrent.TimeUnit

private const val TAG = "BookmarksHandler"

// Legacy browser bookmark provider (works on some older ROMs / AOSP Browser)
private const val BROWSER_BOOKMARKS_URI = "content://browser/bookmarks"

// Chrome's on-device Bookmarks JSON (requires root to read)
private const val CHROME_BOOKMARKS_PATH =
    "/data/data/com.android.chrome/app_chrome/Default/Bookmarks"

class BookmarksHandler(private val context: Context) {

    private val gson = Gson()

    private fun getExportFile(context: Context): File {
        val dir = File(context.getExternalFilesDir(null), "PhoneTransfer")
        if (!dir.exists()) dir.mkdirs()
        return File(dir, "bookmarks_import.html")
    }

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["extract_bookmarks"] = { _, server ->
            extractBookmarks(server)
        }
    }

    fun registerInject(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["inject_bookmarks"] = { params, server ->
            @Suppress("UNCHECKED_CAST")
            val items = params["data"] as? List<Map<String, Any?>> ?: emptyList()
            injectBookmarks(items, server)
        }
    }

    // -----------------------------------------------------------------------
    // Extract
    // -----------------------------------------------------------------------

    private fun extractBookmarks(server: SocketServer): String {
        // Strategy 1: root — read Chrome's Bookmarks JSON directly via `su -c cat`
        val rootBookmarks = tryExtractChromeRoot(server)
        if (rootBookmarks != null) {
            return Response.ok(
                "extract_bookmarks",
                mapOf("count" to rootBookmarks.size, "bookmarks" to rootBookmarks)
            )
        }

        // Strategy 2: non-root — legacy content://browser/bookmarks provider
        val legacyBookmarks = tryExtractLegacyProvider(server)
        if (legacyBookmarks != null) {
            return Response.ok(
                "extract_bookmarks",
                mapOf("count" to legacyBookmarks.size, "bookmarks" to legacyBookmarks)
            )
        }

        Log.w(TAG, "No bookmark source accessible")
        return Response.ok("extract_bookmarks", mapOf("count" to 0, "bookmarks" to emptyList<Any>()))
    }

    /** Try reading Chrome's Bookmarks file via `su -c cat`. Returns null if unavailable. */
    private fun tryExtractChromeRoot(server: SocketServer): List<Map<String, Any?>>? {
        return try {
            val proc = Runtime.getRuntime().exec(arrayOf("su", "-c", "cat $CHROME_BOOKMARKS_PATH"))

            // Read stdout and stderr concurrently to avoid pipe buffer deadlock
            val stdoutBuilder = StringBuilder()
            val stderrBuilder = StringBuilder()
            val stdoutThread = Thread {
                try {
                    proc.inputStream.bufferedReader().use { stdoutBuilder.append(it.readText()) }
                } catch (_: Exception) {}
            }
            val stderrThread = Thread {
                try {
                    proc.errorStream.bufferedReader().use { stderrBuilder.append(it.readText()) }
                } catch (_: Exception) {}
            }
            stdoutThread.start()
            stderrThread.start()

            val finished = proc.waitFor(30, TimeUnit.SECONDS)
            if (!finished) {
                proc.destroyForcibly()
                return null
            }

            stdoutThread.join(5000)
            stderrThread.join(5000)

            val output = stdoutBuilder.toString()
            val exitCode = proc.exitValue()
            if (exitCode != 0 || output.isBlank()) return null

            parseChromeBookmarksJson(output, server)
        } catch (e: Exception) {
            Log.d(TAG, "Root Chrome bookmark read failed: ${e.message}")
            null
        }
    }

    /**
     * Parse Chrome's Bookmarks JSON format.
     */
    private fun parseChromeBookmarksJson(json: String, server: SocketServer): List<Map<String, Any?>>? {
        return try {
            val root = gson.fromJson(json, JsonObject::class.java)
            val roots = root.getAsJsonObject("roots") ?: return null
            val results = mutableListOf<Map<String, Any?>>()

            val folderMap = mapOf(
                "bookmark_bar" to "Bookmarks bar",
                "other" to "Other bookmarks",
                "synced" to "Mobile bookmarks"
            )

            for ((key, displayName) in folderMap) {
                val folder = roots.getAsJsonObject(key) ?: continue
                val children = folder.getAsJsonArray("children") ?: continue
                collectChromeNodes(children, displayName, results)
            }

            server.sendProgress("bookmarks", results.size, results.size)
            results
        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse Chrome bookmarks JSON: ${e.message}")
            null
        }
    }

    private fun collectChromeNodes(
        children: com.google.gson.JsonArray,
        folderName: String,
        out: MutableList<Map<String, Any?>>
    ) {
        for (element in children) {
            val node = element.asJsonObject ?: continue
            val type = node.get("type")?.asString ?: continue
            val name = node.get("name")?.asString ?: ""

            when (type) {
                "url" -> {
                    val url = node.get("url")?.asString ?: continue
                    val dateAddedMicros = node.get("date_added")?.asLong ?: 0L
                    val unixMs = if (dateAddedMicros > 0L) {
                        (dateAddedMicros / 1000L) - 11644473600_000L
                    } else {
                        null
                    }
                    out.add(
                        mapOf(
                            "title" to name,
                            "url" to url,
                            "folder" to folderName,
                            "added" to unixMs
                        )
                    )
                }
                "folder" -> {
                    val subChildren = node.getAsJsonArray("children") ?: continue
                    collectChromeNodes(subChildren, name, out)
                }
            }
        }
    }

    /** Try the legacy content://browser/bookmarks provider. Returns null if unavailable. */
    private fun tryExtractLegacyProvider(server: SocketServer): List<Map<String, Any?>>? {
        val projection = arrayOf("title", "url", "created", "bookmark")
        return try {
            val cursor = context.contentResolver.query(
                Uri.parse(BROWSER_BOOKMARKS_URI),
                projection,
                "bookmark=1",  // only real bookmarks, not history
                null,
                null
            ) ?: return null

            val results = mutableListOf<Map<String, Any?>>()
            cursor.use {
                val total = it.count
                var done = 0
                while (it.moveToNext()) {
                    val titleIdx = it.getColumnIndex("title")
                    val urlIdx = it.getColumnIndex("url")
                    val createdIdx = it.getColumnIndex("created")

                    val title = if (titleIdx >= 0) it.getString(titleIdx) else null
                    val url = if (urlIdx >= 0) it.getString(urlIdx) else null
                    if (url == null) continue

                    val created = if (createdIdx >= 0) it.getLong(createdIdx).takeIf { ms -> ms > 0 } else null

                    results.add(
                        mapOf(
                            "title" to (title ?: ""),
                            "url" to url,
                            "folder" to "Bookmarks bar",
                            "added" to created
                        )
                    )
                    done++
                    if (total > 0 && done % 50 == 0) server.sendProgress("bookmarks", done, total)
                }
                server.sendProgress("bookmarks", results.size, results.size)
            }
            results
        } catch (e: Exception) {
            Log.d(TAG, "Legacy browser provider unavailable: ${e.message}")
            null
        }
    }

    // -----------------------------------------------------------------------
    // Inject
    // -----------------------------------------------------------------------

    private fun injectBookmarks(items: List<Map<String, Any?>>, server: SocketServer): String {
        // Strategy 1: legacy browser content provider
        val providerResult = tryInjectLegacyProvider(items, server)
        if (providerResult != null) return providerResult

        // Strategy 2: write Netscape HTML bookmark file for manual import
        return writeNetscapeHtml(items, server)
    }

    private fun tryInjectLegacyProvider(
        items: List<Map<String, Any?>>,
        server: SocketServer
    ): String? {
        val uri = Uri.parse(BROWSER_BOOKMARKS_URI)
        var inserted = 0
        var skipped = 0
        val total = items.size

        return try {
            // Probe the provider first
            context.contentResolver.query(uri, arrayOf("_id"), null, null, null)?.close()
                ?: return null

            for ((index, item) in items.withIndex()) {
                val url = item["url"] as? String ?: run { skipped++; continue }
                val title = item["title"] as? String ?: url

                try {
                    val cv = ContentValues().apply {
                        put("title", title)
                        put("url", url)
                        put("bookmark", 1)
                    }
                    val result = context.contentResolver.insert(uri, cv)
                    if (result != null) inserted++ else skipped++
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to insert bookmark '$url': ${e.message}")
                    skipped++
                }

                if ((index + 1) % 50 == 0 || index == total - 1) {
                    server.sendProgress("bookmarks", index + 1, total)
                }
            }

            Response.ok(
                "inject_bookmarks",
                mapOf(
                    "method" to "content_provider",
                    "inserted" to inserted,
                    "skipped" to skipped,
                    "total" to total
                )
            )
        } catch (e: Exception) {
            Log.d(TAG, "Legacy browser provider inject unavailable: ${e.message}")
            null
        }
    }

    private fun writeNetscapeHtml(items: List<Map<String, Any?>>, server: SocketServer): String {
        val outputFile = getExportFile(context)
        var count = 0

        try {
            FileWriter(outputFile).use { writer ->
                writer.write("<!DOCTYPE NETSCAPE-Bookmark-file-1>\n")
                writer.write("<!-- This is an automatically generated file. It will be read and overwritten. DO NOT EDIT! -->\n")
                writer.write("<META HTTP-EQUIV=\"Content-Type\" CONTENT=\"text/html; charset=UTF-8\">\n")
                writer.write("<TITLE>Bookmarks</TITLE>\n")
                writer.write("<H1>Bookmarks</H1>\n")
                writer.write("<DL><p>\n")

                val total = items.size
                for ((index, item) in items.withIndex()) {
                    val url = item["url"] as? String ?: continue
                    val title = (item["title"] as? String ?: url).htmlEscape()
                    val addedMs = (item["added"] as? Number)?.toLong() ?: 0L
                    val addedSec = addedMs / 1000L

                    writer.write("    <DT><A HREF=\"${url.htmlEscape()}\" ADD_DATE=\"$addedSec\">$title</A>\n")
                    count++

                    if ((index + 1) % 50 == 0 || index == total - 1) {
                        server.sendProgress("bookmarks", index + 1, total)
                    }
                }

                writer.write("</DL><p>\n")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to write Netscape HTML: ${e.message}")
            return Response.error("inject_bookmarks", "write_failed", e.message ?: "Unknown error")
        }

        return Response.ok(
            "inject_bookmarks",
            mapOf(
                "method" to "netscape_html",
                "file" to outputFile.absolutePath,
                "count" to count
            )
        )
    }

    private fun String.htmlEscape(): String =
        replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\"", "&quot;")
            .replace("'", "&#39;")
}
