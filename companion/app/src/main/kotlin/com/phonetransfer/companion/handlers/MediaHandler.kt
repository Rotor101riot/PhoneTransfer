package com.phonetransfer.companion.handlers

import android.content.Context
import android.net.Uri
import android.provider.MediaStore
import android.util.Log
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.File

private const val TAG = "MediaHandler"

// Common directories where voice memos are stored by various recorder apps
private val VOICE_MEMO_DIRS = listOf(
    "/sdcard/MIUI/sound_recorder",           // Xiaomi/MIUI
    "/sdcard/Recordings",                     // Stock Android / Samsung
    "/sdcard/Music/SoundRecorder",            // Some OEMs
    "/sdcard/Voice Recorder",                 // Samsung
    "/sdcard/AudioRecording",                 // LG
    "/sdcard/SoundRecord",                    // Generic
    "/sdcard/Sounds",                         // Huawei EMUI
    "/sdcard/record",                         // Huawei EMUI (alternate)
    "/sdcard/Recorder",                       // Honor
    "/sdcard/HuaweiBackup/backupFiles"        // Huawei backup recordings
)

/**
 * Huawei EMUI's custom music/playlist ContentProvider URI.
 * This provider stores playlists created in Huawei's built-in Music app,
 * which are NOT visible in the standard MediaStore.Audio.Playlists API.
 */
private val HUAWEI_EMOTION_MEDIA_URI: Uri = Uri.parse("content://EmotionMedia/playlist")

/** Standard Android playlist URI. */
private val MUSIC_PLAYLISTS_URI: Uri = Uri.parse("content://media/external/audio/media_playlists")

private val VOICE_MEMO_EXTENSIONS = setOf("m4a", "aac", "amr", "3gpp", "mp3", "wav", "ogg")

/**
 * MediaHandler — handles the "media_list" command.
 *
 * This handler returns FILE PATHS and metadata only; it does NOT transfer
 * file content over the socket. The PC side uses `adb pull <path>` to
 * retrieve each file after receiving the list.
 *
 * Supported media_type values:
 *   "photos"      — images from MediaStore.Images
 *   "videos"      — videos from MediaStore.Video
 *   "ringtones"   — audio marked is_ringtone=1
 *   "voice_memos" — audio not in music/ringtone/alarm/notification/podcast,
 *                   plus filesystem scan of common recorder directories
 */
class MediaHandler(private val context: Context) {

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["media_list"] = { params, server ->
            val mediaType = params["media_type"] as? String ?: ""
            when (mediaType) {
                "photos" -> extractPhotos(server)
                "videos" -> extractVideos(server)
                "ringtones" -> extractRingtones(server)
                "voice_memos" -> extractVoiceMemos(server)
                "playlists" -> extractPlaylists(server)
                else -> Response.error(
                    "media_list",
                    "unknown_media_type",
                    "media_type must be one of: photos, videos, ringtones, voice_memos, playlists — got '$mediaType'"
                )
            }
        }
    }

    // -----------------------------------------------------------------------
    // Photos
    // -----------------------------------------------------------------------

    private fun extractPhotos(server: SocketServer): String {
        val projection = arrayOf(
            MediaStore.Images.Media.DATA,          // absolute file path
            MediaStore.Images.Media.DISPLAY_NAME,
            MediaStore.Images.Media.SIZE,
            MediaStore.Images.Media.DATE_TAKEN,    // ms since epoch
            MediaStore.Images.Media.BUCKET_DISPLAY_NAME,
            MediaStore.MediaColumns._ID
        )

        val files = queryMediaStore(
            uri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            projection = projection,
            selection = null,
            selectionArgs = null,
            sortOrder = "${MediaStore.Images.Media.DATE_TAKEN} DESC",
            category = "photos",
            server = server
        ) { cursor ->
            mapOf(
                "path" to (cursor.getString(cursor.getColumnIndex(MediaStore.Images.Media.DATA)) ?: ""),
                "name" to (cursor.getString(cursor.getColumnIndex(MediaStore.Images.Media.DISPLAY_NAME)) ?: ""),
                "size" to cursor.getLong(cursor.getColumnIndex(MediaStore.Images.Media.SIZE)),
                "created" to cursor.getLong(cursor.getColumnIndex(MediaStore.Images.Media.DATE_TAKEN)).takeIf { it > 0 },
                "album" to (cursor.getString(cursor.getColumnIndex(MediaStore.Images.Media.BUCKET_DISPLAY_NAME)) ?: ""),
                "_id" to cursor.getLong(cursor.getColumnIndex(MediaStore.MediaColumns._ID)).takeIf { it > 0 }
            )
        }

        return Response.ok(
            "media_list",
            mapOf("media_type" to "photos", "count" to files.size, "files" to files)
        )
    }

    // -----------------------------------------------------------------------
    // Videos
    // -----------------------------------------------------------------------

    private fun extractVideos(server: SocketServer): String {
        val projection = arrayOf(
            MediaStore.Video.Media.DATA,
            MediaStore.Video.Media.DISPLAY_NAME,
            MediaStore.Video.Media.SIZE,
            MediaStore.Video.Media.DATE_TAKEN,
            MediaStore.Video.Media.BUCKET_DISPLAY_NAME,
            MediaStore.Video.Media.DURATION,
            MediaStore.MediaColumns._ID
        )

        val files = queryMediaStore(
            uri = MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
            projection = projection,
            selection = null,
            selectionArgs = null,
            sortOrder = "${MediaStore.Video.Media.DATE_TAKEN} DESC",
            category = "videos",
            server = server
        ) { cursor ->
            mapOf(
                "path" to (cursor.getString(cursor.getColumnIndex(MediaStore.Video.Media.DATA)) ?: ""),
                "name" to (cursor.getString(cursor.getColumnIndex(MediaStore.Video.Media.DISPLAY_NAME)) ?: ""),
                "size" to cursor.getLong(cursor.getColumnIndex(MediaStore.Video.Media.SIZE)),
                "created" to cursor.getLong(cursor.getColumnIndex(MediaStore.Video.Media.DATE_TAKEN)).takeIf { it > 0 },
                "album" to (cursor.getString(cursor.getColumnIndex(MediaStore.Video.Media.BUCKET_DISPLAY_NAME)) ?: ""),
                "duration" to cursor.getLong(cursor.getColumnIndex(MediaStore.Video.Media.DURATION)).takeIf { it > 0 },
                "_id" to cursor.getLong(cursor.getColumnIndex(MediaStore.MediaColumns._ID)).takeIf { it > 0 }
            )
        }

        return Response.ok(
            "media_list",
            mapOf("media_type" to "videos", "count" to files.size, "files" to files)
        )
    }

    // -----------------------------------------------------------------------
    // Ringtones
    // -----------------------------------------------------------------------

    private fun extractRingtones(server: SocketServer): String {
        val projection = arrayOf(
            MediaStore.Audio.Media.DATA,
            MediaStore.Audio.Media.DISPLAY_NAME,
            MediaStore.Audio.Media.SIZE,
            MediaStore.Audio.Media.DATE_ADDED,
            MediaStore.MediaColumns._ID
        )

        val files = queryMediaStore(
            uri = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI,
            projection = projection,
            selection = "${MediaStore.Audio.Media.IS_RINGTONE} = 1",
            selectionArgs = null,
            sortOrder = "${MediaStore.Audio.Media.DISPLAY_NAME} ASC",
            category = "ringtones",
            server = server
        ) { cursor ->
            // DATE_ADDED is in seconds; convert to ms for consistency
            val dateAddedSec = cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Media.DATE_ADDED))
            mapOf(
                "path" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Media.DATA)) ?: ""),
                "name" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Media.DISPLAY_NAME)) ?: ""),
                "size" to cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Media.SIZE)),
                "created" to if (dateAddedSec > 0) dateAddedSec * 1000L else null,
                "album" to null,
                "_id" to cursor.getLong(cursor.getColumnIndex(MediaStore.MediaColumns._ID)).takeIf { it > 0 }
            )
        }

        return Response.ok(
            "media_list",
            mapOf("media_type" to "ringtones", "count" to files.size, "files" to files)
        )
    }

    // -----------------------------------------------------------------------
    // Voice Memos
    // -----------------------------------------------------------------------

    private fun extractVoiceMemos(server: SocketServer): String {
        val projection = arrayOf(
            MediaStore.Audio.Media.DATA,
            MediaStore.Audio.Media.DISPLAY_NAME,
            MediaStore.Audio.Media.SIZE,
            MediaStore.Audio.Media.DATE_ADDED,
            MediaStore.MediaColumns._ID
        )

        // Exclude all standard audio categories — what remains is likely voice recordings
        val selection = """
            ${MediaStore.Audio.Media.IS_MUSIC} = 0
            AND ${MediaStore.Audio.Media.IS_RINGTONE} = 0
            AND ${MediaStore.Audio.Media.IS_ALARM} = 0
            AND ${MediaStore.Audio.Media.IS_NOTIFICATION} = 0
            AND ${MediaStore.Audio.Media.IS_PODCAST} = 0
        """.trimIndent()

        val mediaStoreFiles = queryMediaStore(
            uri = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI,
            projection = projection,
            selection = selection,
            selectionArgs = null,
            sortOrder = "${MediaStore.Audio.Media.DATE_ADDED} DESC",
            category = "voice_memos",
            server = server
        ) { cursor ->
            val dateAddedSec = cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Media.DATE_ADDED))
            mapOf(
                "path" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Media.DATA)) ?: ""),
                "name" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Media.DISPLAY_NAME)) ?: ""),
                "size" to cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Media.SIZE)),
                "created" to if (dateAddedSec > 0) dateAddedSec * 1000L else null,
                "album" to null,
                "_id" to cursor.getLong(cursor.getColumnIndex(MediaStore.MediaColumns._ID)).takeIf { it > 0 }
            )
        }

        // Also scan known voice recorder directories for files not indexed by MediaStore
        val knownPaths = mediaStoreFiles.mapNotNull { it["path"] as? String }.toHashSet()
        val fsScanFiles = scanVoiceMemoDirectories(knownPaths)

        val allFiles = mediaStoreFiles + fsScanFiles

        return Response.ok(
            "media_list",
            mapOf("media_type" to "voice_memos", "count" to allFiles.size, "files" to allFiles)
        )
    }

    private fun scanVoiceMemoDirectories(knownPaths: Set<String>): List<Map<String, Any?>> {
        val results = mutableListOf<Map<String, Any?>>()
        for (dirPath in VOICE_MEMO_DIRS) {
            val dir = File(dirPath)
            if (!dir.exists() || !dir.isDirectory) continue
            val files = dir.listFiles() ?: continue
            for (file in files) {
                if (!file.isFile) continue
                if (file.extension.lowercase() !in VOICE_MEMO_EXTENSIONS) continue
                if (file.absolutePath in knownPaths) continue  // already included from MediaStore
                results.add(
                    mapOf(
                        "path" to file.absolutePath,
                        "name" to file.name,
                        "size" to file.length(),
                        "created" to file.lastModified().takeIf { it > 0 },
                        "album" to null
                    )
                )
            }
        }
        if (results.isNotEmpty()) {
            Log.i(TAG, "Filesystem scan added ${results.size} voice memo files not in MediaStore")
        }
        return results
    }

    // -----------------------------------------------------------------------
    // Playlists (standard MediaStore + Huawei EmotionMedia)
    // -----------------------------------------------------------------------

    private fun extractPlaylists(server: SocketServer): String {
        val playlists = mutableListOf<Map<String, Any?>>()

        // 1. Standard Android MediaStore playlists
        try {
            val stdProjection = arrayOf(
                MediaStore.Audio.Playlists._ID,
                MediaStore.Audio.Playlists.NAME,
                MediaStore.Audio.Playlists.DATE_ADDED,
                MediaStore.Audio.Playlists.DATE_MODIFIED
            )
            context.contentResolver.query(
                MediaStore.Audio.Playlists.EXTERNAL_CONTENT_URI,
                stdProjection,
                null, null,
                "${MediaStore.Audio.Playlists.NAME} ASC"
            )?.use { cursor ->
                val total = cursor.count
                var done = 0
                while (cursor.moveToNext()) {
                    val id = cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Playlists._ID))
                    val name = cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Playlists.NAME)) ?: ""
                    val dateAdded = cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Playlists.DATE_ADDED))

                    // Query tracks in this playlist
                    val tracks = queryPlaylistTracks(id)

                    playlists.add(mapOf(
                        "source" to "mediastore",
                        "playlist_id" to id,
                        "name" to name,
                        "created" to if (dateAdded > 0) dateAdded * 1000L else null,
                        "track_count" to tracks.size,
                        "tracks" to tracks
                    ))
                    done++
                    if (total > 0 && done % 10 == 0) server.sendProgress("playlists", done, total)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Standard playlist query failed: ${e.message}")
        }

        // 2. Huawei EMUI EmotionMedia playlists (only on Huawei/Honor devices)
        if (isHuaweiDevice()) {
            try {
                context.contentResolver.query(
                    HUAWEI_EMOTION_MEDIA_URI,
                    null,  // query all columns — schema is proprietary
                    null, null, null
                )?.use { cursor ->
                    val columns = cursor.columnNames.toList()
                    Log.i(TAG, "Huawei EmotionMedia columns: $columns")
                    while (cursor.moveToNext()) {
                        val row = mutableMapOf<String, Any?>()
                        row["source"] = "huawei_emui"
                        for (col in columns) {
                            val idx = cursor.getColumnIndex(col)
                            if (idx >= 0) {
                                row[col] = try {
                                    cursor.getString(idx)
                                } catch (_: Exception) {
                                    try { cursor.getLong(idx) } catch (_: Exception) { null }
                                }
                            }
                        }
                        playlists.add(row)
                    }
                    Log.i(TAG, "Huawei EmotionMedia: found ${cursor.count} playlists")
                }
            } catch (e: SecurityException) {
                Log.i(TAG, "Huawei EmotionMedia not available (permission denied)")
            } catch (e: Exception) {
                Log.i(TAG, "Huawei EmotionMedia not available: ${e.message}")
            }
        }

        return Response.ok(
            "media_list",
            mapOf("media_type" to "playlists", "count" to playlists.size, "data" to playlists)
        )
    }

    /**
     * Query the tracks within a standard MediaStore playlist.
     */
    private fun queryPlaylistTracks(playlistId: Long): List<Map<String, Any?>> {
        val tracks = mutableListOf<Map<String, Any?>>()
        try {
            val membersUri = MediaStore.Audio.Playlists.Members.getContentUri("external", playlistId)
            context.contentResolver.query(
                membersUri,
                arrayOf(
                    MediaStore.Audio.Playlists.Members.AUDIO_ID,
                    MediaStore.Audio.Playlists.Members.TITLE,
                    MediaStore.Audio.Playlists.Members.ARTIST,
                    MediaStore.Audio.Playlists.Members.ALBUM,
                    MediaStore.Audio.Playlists.Members.DURATION,
                    MediaStore.Audio.Playlists.Members.DATA,
                    MediaStore.Audio.Playlists.Members.PLAY_ORDER
                ),
                null, null,
                "${MediaStore.Audio.Playlists.Members.PLAY_ORDER} ASC"
            )?.use { cursor ->
                while (cursor.moveToNext()) {
                    tracks.add(mapOf(
                        "audio_id" to cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Playlists.Members.AUDIO_ID)),
                        "title" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Playlists.Members.TITLE)) ?: ""),
                        "artist" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Playlists.Members.ARTIST)) ?: ""),
                        "album" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Playlists.Members.ALBUM)) ?: ""),
                        "duration" to cursor.getLong(cursor.getColumnIndex(MediaStore.Audio.Playlists.Members.DURATION)),
                        "path" to (cursor.getString(cursor.getColumnIndex(MediaStore.Audio.Playlists.Members.DATA)) ?: ""),
                        "play_order" to cursor.getInt(cursor.getColumnIndex(MediaStore.Audio.Playlists.Members.PLAY_ORDER))
                    ))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to query playlist $playlistId tracks: ${e.message}")
        }
        return tracks
    }

    /**
     * Detect Huawei/Honor devices for EMUI-specific features.
     * Checks manufacturer and ro.build.version.emui system property.
     */
    private fun isHuaweiDevice(): Boolean {
        val manufacturer = android.os.Build.MANUFACTURER.lowercase()
        if (manufacturer.contains("huawei") || manufacturer.contains("honor")) return true

        // Check EMUI build property
        return try {
            val clazz = Class.forName("android.os.SystemProperties")
            val method = clazz.getMethod("get", String::class.java, String::class.java)
            val emui = method.invoke(null, "ro.build.version.emui", "") as? String
            !emui.isNullOrEmpty()
        } catch (_: Exception) {
            false
        }
    }

    // -----------------------------------------------------------------------
    // Generic MediaStore query helper
    // -----------------------------------------------------------------------

    private fun queryMediaStore(
        uri: Uri,
        projection: Array<String>,
        selection: String?,
        selectionArgs: Array<String>?,
        sortOrder: String?,
        category: String,
        server: SocketServer,
        rowMapper: (android.database.Cursor) -> Map<String, Any?>
    ): List<Map<String, Any?>> {
        val results = mutableListOf<Map<String, Any?>>()
        try {
            context.contentResolver.query(uri, projection, selection, selectionArgs, sortOrder)
                ?.use { cursor ->
                    val total = cursor.count
                    var done = 0
                    while (cursor.moveToNext()) {
                        try {
                            results.add(rowMapper(cursor))
                        } catch (e: Exception) {
                            Log.w(TAG, "Row mapping error for $category: ${e.message}")
                        }
                        done++
                        if (total > 0 && done % 50 == 0) server.sendProgress(category, done, total)
                    }
                    server.sendProgress(category, results.size, results.size)
                }
        } catch (e: SecurityException) {
            Log.e(TAG, "Permission denied querying $uri: ${e.message}")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to query $uri for $category: ${e.message}")
        }
        return results
    }
}
