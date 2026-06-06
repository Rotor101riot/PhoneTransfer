package com.phonetransfer.companion.handlers

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.File
import java.io.FileWriter

private const val TAG = "NotesHandler"

// Known notes content providers
private data class NotesProvider(
    val uri: String,
    val titleCol: String,
    val bodyCol: String,
    val createdCol: String?,
    val modifiedCol: String?,
    val folderCol: String?
)

private val NOTES_PROVIDERS = listOf(
    NotesProvider(
        uri = "content://com.samsung.android.app.notes/notes",
        titleCol = "subject",
        bodyCol = "body_text",
        createdCol = "created_time",
        modifiedCol = "modified_time",
        folderCol = "folder_name"
    ),
    NotesProvider(
        uri = "content://com.socialnmobile.dictapps.notepad.color.note/note",
        titleCol = "title",
        bodyCol = "note",
        createdCol = "created_date",
        modifiedCol = "modified_date",
        folderCol = null
    )
)

// Directories to scan for .txt notes files
private val TXT_SCAN_DIRS = listOf(
    "/sdcard/Documents",
    "/sdcard/Notes"
)

class NotesHandler(private val context: Context) {

    private fun getNotesOutputDir(context: Context): File {
        val dir = File(context.getExternalFilesDir(null), "PhoneTransfer/notes")
        if (!dir.exists()) dir.mkdirs()
        return dir
    }

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["extract_notes"] = { _, server ->
            extractNotes(server)
        }
    }

    fun registerInject(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["inject_notes"] = { params, server ->
            @Suppress("UNCHECKED_CAST")
            val items = params["data"] as? List<Map<String, Any?>> ?: emptyList()
            injectNotes(items, server)
        }
    }

    // -----------------------------------------------------------------------
    // Extract
    // -----------------------------------------------------------------------

    private fun extractNotes(server: SocketServer): String {
        val notes = mutableListOf<Map<String, Any?>>()

        // Strategy 1: try known content providers
        for (provider in NOTES_PROVIDERS) {
            val providerNotes = tryQueryProvider(provider, server)
            if (providerNotes != null) {
                notes.addAll(providerNotes)
                Log.i(TAG, "Got ${providerNotes.size} notes from ${provider.uri}")
                break // use first successful provider
            }
        }

        // Strategy 2: scan filesystem for .txt files
        val txtNotes = scanTxtFiles(server, existingCount = notes.size)
        notes.addAll(txtNotes)

        server.sendProgress("notes", notes.size, notes.size)

        return Response.ok("extract_notes", mapOf("count" to notes.size, "notes" to notes))
    }

    private fun tryQueryProvider(provider: NotesProvider, server: SocketServer): List<Map<String, Any?>>? {
        val projection = buildList {
            add(provider.titleCol)
            add(provider.bodyCol)
            if (provider.createdCol != null) add(provider.createdCol)
            if (provider.modifiedCol != null) add(provider.modifiedCol)
            if (provider.folderCol != null) add(provider.folderCol)
        }.toTypedArray()

        return try {
            val cursor = context.contentResolver.query(
                Uri.parse(provider.uri), projection, null, null, null
            ) ?: return null

            val results = mutableListOf<Map<String, Any?>>()
            cursor.use {
                val total = it.count
                var done = 0
                while (it.moveToNext()) {
                    val title = safeGetString(it, provider.titleCol)
                    val body = safeGetString(it, provider.bodyCol)
                    val created = provider.createdCol?.let { col -> safeGetLong(it, col) }
                    val modified = provider.modifiedCol?.let { col -> safeGetLong(it, col) }
                    val folder = provider.folderCol?.let { col -> safeGetString(it, col) }

                    results.add(
                        mapOf(
                            "title" to title,
                            "body" to body,
                            "created" to created,
                            "modified" to modified,
                            "folder" to folder
                        )
                    )
                    done++
                    if (total > 0 && done % 20 == 0) server.sendProgress("notes", done, total)
                }
            }
            results
        } catch (e: Exception) {
            Log.d(TAG, "Provider ${provider.uri} unavailable: ${e.message}")
            null
        }
    }

    private fun scanTxtFiles(server: SocketServer, existingCount: Int): List<Map<String, Any?>> {
        val results = mutableListOf<Map<String, Any?>>()
        for (dirPath in TXT_SCAN_DIRS) {
            val dir = File(dirPath)
            if (!dir.exists() || !dir.isDirectory) continue

            val txtFiles = dir.walkTopDown()
                .maxDepth(3)
                .filter { f -> f.isFile && f.extension.equals("txt", ignoreCase = true) }
                .toList()

            for (file in txtFiles) {
                try {
                    val body = file.readText(Charsets.UTF_8)
                    val title = file.nameWithoutExtension
                    results.add(
                        mapOf(
                            "title" to title,
                            "body" to body,
                            "created" to null,
                            "modified" to file.lastModified().takeIf { it > 0 },
                            "folder" to null
                        )
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to read txt note '${file.absolutePath}': ${e.message}")
                }
            }
        }
        if (results.isNotEmpty()) {
            Log.i(TAG, "Scanned ${results.size} .txt notes from filesystem")
        }
        return results
    }

    // -----------------------------------------------------------------------
    // Inject
    // -----------------------------------------------------------------------

    private fun injectNotes(items: List<Map<String, Any?>>, server: SocketServer): String {
        val outDir = getNotesOutputDir(context)

        var written = 0
        var skipped = 0
        val total = items.size

        for ((index, note) in items.withIndex()) {
            val title = (note["title"] as? String)?.trim()?.ifBlank { "note_${index + 1}" }
                ?: "note_${index + 1}"
            val body = note["body"] as? String ?: ""

            // Sanitise filename — strip chars that are invalid on FAT32/ext4
            val safeTitle = title.replace(Regex("[\\\\/:*?\"<>|]"), "_").take(200)
            val outFile = resolveUniqueFile(outDir, safeTitle)

            try {
                FileWriter(outFile, Charsets.UTF_8).use { it.write(body) }
                written++
            } catch (e: Exception) {
                Log.e(TAG, "Failed to write note '${outFile.name}': ${e.message}")
                skipped++
            }

            if ((index + 1) % 20 == 0 || index == total - 1) {
                server.sendProgress("notes", index + 1, total)
            }
        }

        // Broadcast media scan so the new files are visible in file managers
        broadcastMediaScan(outDir)

        return Response.ok(
            "inject_notes",
            mapOf(
                "directory" to outDir.absolutePath,
                "written" to written,
                "skipped" to skipped,
                "total" to total
            )
        )
    }

    /** Return a File that does not yet exist, appending _2, _3 … if needed. */
    private fun resolveUniqueFile(dir: File, baseName: String): File {
        var candidate = File(dir, "$baseName.txt")
        var counter = 2
        while (candidate.exists()) {
            candidate = File(dir, "${baseName}_$counter.txt")
            counter++
        }
        return candidate
    }

    private fun broadcastMediaScan(dir: File) {
        try {
            val intent = Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE).apply {
                data = Uri.fromFile(dir)
            }
            context.sendBroadcast(intent)
        } catch (e: Exception) {
            Log.w(TAG, "Media scan broadcast failed: ${e.message}")
        }
    }

    // -----------------------------------------------------------------------
    // Cursor helpers
    // -----------------------------------------------------------------------

    private fun safeGetString(cursor: android.database.Cursor, column: String): String? {
        val idx = cursor.getColumnIndex(column)
        return if (idx >= 0) cursor.getString(idx) else null
    }

    private fun safeGetLong(cursor: android.database.Cursor, column: String): Long? {
        val idx = cursor.getColumnIndex(column)
        return if (idx >= 0) cursor.getLong(idx).takeIf { it > 0 } else null
    }
}
