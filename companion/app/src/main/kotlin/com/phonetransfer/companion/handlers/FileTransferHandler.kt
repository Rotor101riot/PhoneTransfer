package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import android.webkit.MimeTypeMap
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.File
import java.io.FileInputStream
import java.io.IOException
import java.io.OutputStream
import java.security.MessageDigest

private const val TAG = "FileTransferHandler"

/**
 * Transfer chunk size used for both file_pull (APK → PC) and file_push (PC → APK).
 * 2 MB matches Dr.Fone's dominant I/O constant (0x200000) — big enough for 4×
 * fewer round-trips than 512 KB, small enough to avoid single-buffer allocation
 * pressure on mid-range Android devices.  Must match PC-side `_CHUNK_SIZE`.
 */
private const val CHUNK_SIZE = 2 * 1024 * 1024

/**
 * Relative MediaStore path for transferred photos and videos.
 */
private const val MEDIA_RELATIVE_PATH = "DCIM/PhoneTransfer"

/**
 * FileTransferHandler — handles socket-based binary file transfer.
 */
class FileTransferHandler(private val context: Context) {

    fun register(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["file_pull"] = { params, server -> handleFilePull(params, server) }
        registry["file_push"] = { params, server -> handleFilePush(params, server) }
    }

    // -----------------------------------------------------------------------
    // file_pull — stream a device file to the PC
    // -----------------------------------------------------------------------

    private suspend fun handleFilePull(params: Map<String, Any?>, server: SocketServer): String {
        val path = params["path"] as? String
            ?: return Response.error("file_pull", "missing_param", "param 'path' is required")

        val file = File(path)
        if (!file.exists()) {
            return Response.error("file_pull", "not_found", "File not found: $path")
        }
        if (!file.isFile) {
            return Response.error("file_pull", "not_a_file", "Path is not a regular file: $path")
        }
        if (!file.canRead()) {
            return Response.error("file_pull", "permission_denied", "Cannot read file: $path")
        }

        val size = file.length()

        // Resume support: if the client already has `offset` bytes, skip them
        // but still hash the full file for MD5 verification.
        val requestedOffset = when (val raw = params["offset"]) {
            is Number -> raw.toLong().coerceIn(0, size)
            is String -> (raw.toLongOrNull() ?: 0L).coerceIn(0, size)
            else -> 0L
        }

        val bytesToSend = size - requestedOffset
        val chunkCount = ((bytesToSend + CHUNK_SIZE - 1) / CHUNK_SIZE).toInt().coerceAtLeast(1)

        Log.i(TAG, "file_pull: $path  size=$size  offset=$requestedOffset  remaining=$bytesToSend  chunks=$chunkCount")

        val headerJson = Response.ok(
            "file_pull",
            mapOf(
                "status" to "ok",
                "filename" to file.name,
                "size" to size,
                "offset" to requestedOffset,
                "chunks" to chunkCount,
            )
        )
        server.sendJsonFrame(headerJson)

        var bytesSent = 0L
        // MD5 of the FULL file (not just the resumed portion) so the client
        // can verify integrity of the complete reconstructed file.
        val md5 = MessageDigest.getInstance("MD5")
        try {
            FileInputStream(file).use { fis ->
                val buf = ByteArray(CHUNK_SIZE)

                // Hash the skipped portion (for full-file MD5) without sending it
                if (requestedOffset > 0) {
                    var hashed = 0L
                    while (hashed < requestedOffset) {
                        val toRead = minOf(CHUNK_SIZE.toLong(), requestedOffset - hashed).toInt()
                        val read = fis.read(buf, 0, toRead)
                        if (read == -1) break
                        md5.update(buf, 0, read)
                        hashed += read
                    }
                    Log.d(TAG, "file_pull: hashed $hashed skipped bytes for full-file MD5")
                }

                // Stream the remaining bytes
                var chunkIndex = 0
                while (true) {
                    val read = fis.read(buf)
                    if (read == -1) break
                    md5.update(buf, 0, read)
                    val chunk = if (read == CHUNK_SIZE) buf else buf.copyOf(read)
                    server.sendBinaryFrame(chunk)
                    bytesSent += read
                    chunkIndex++
                    Log.v(TAG, "file_pull: sent chunk $chunkIndex/$chunkCount ($bytesSent/$bytesToSend bytes)")
                }
            }
        } catch (e: IOException) {
            Log.e(TAG, "file_pull: I/O error reading $path: ${e.message}")
            return Response.error("file_pull", "io_error", "Read failed after $bytesSent bytes: ${e.message}")
        }

        val md5Hex = md5.digest().joinToString("") { "%02x".format(it) }
        Log.i(TAG, "file_pull: complete — sent $bytesSent bytes (offset=$requestedOffset) for $path  md5=$md5Hex")
        return Response.ok("file_pull", mapOf(
            "status" to "done",
            "size" to bytesSent,
            "total_size" to size,
            "offset" to requestedOffset,
            "md5" to md5Hex
        ))
    }

    // -----------------------------------------------------------------------
    // file_push — receive a file from the PC
    // -----------------------------------------------------------------------

    private suspend fun handleFilePush(params: Map<String, Any?>, server: SocketServer): String {
        val filename = params["filename"] as? String
            ?: return Response.error("file_push", "missing_param", "param 'filename' is required")
        val size = when (val raw = params["size"]) {
            is Number -> raw.toLong()
            is String -> raw.toLongOrNull()
            else -> null
        } ?: return Response.error("file_push", "missing_param", "param 'size' must be a number")
        val dest = params["dest"] as? String ?: "downloads"
        val dateTakenMs = when (val raw = params["date_taken"]) {
            is Number -> raw.toLong().takeIf { it > 0 }
            is String -> raw.toLongOrNull()?.takeIf { it > 0 }
            else -> null
        }

        // Resume support: client tells us how many bytes it will skip
        val clientOffset = when (val raw = params["offset"]) {
            is Number -> raw.toLong().coerceIn(0, size)
            is String -> (raw.toLongOrNull() ?: 0L).coerceIn(0, size)
            else -> 0L
        }

        if (size <= 0L) {
            return Response.error("file_push", "invalid_param", "size must be > 0")
        }

        val remainingBytes = size - clientOffset
        Log.i(TAG, "file_push: filename=$filename  size=$size  offset=$clientOffset  remaining=$remainingBytes  dest=$dest")

        server.sendJsonFrame(Response.ok("file_push", mapOf(
            "status" to "ready",
            "offset" to clientOffset,
            "remaining" to remainingBytes
        )))

        // When resuming, the client only sends `remainingBytes` worth of data
        val result = when (dest.lowercase()) {
            "photos" -> streamToMediaStore(
                server, filename, remainingBytes, dateTakenMs,
                collectionUri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                mimeBase = "image",
                defaultMime = "image/jpeg"
            )
            "videos" -> streamToMediaStore(
                server, filename, remainingBytes, dateTakenMs,
                collectionUri = MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                mimeBase = "video",
                defaultMime = "video/mp4"
            )
            else -> streamToDownloads(server, filename, remainingBytes)
        }

        return if (result != null) {
            val (bytesWritten, md5Hex) = result
            Log.i(TAG, "file_push: written $bytesWritten bytes (offset=$clientOffset) → dest=$dest  file=$filename  md5=$md5Hex")
            Response.ok("file_push", mapOf(
                "status" to "done",
                "bytes_received" to bytesWritten,
                "offset" to clientOffset,
                "md5" to md5Hex
            ))
        } else {
            Response.error("file_push", "write_error", "Failed to write $filename to $dest")
        }
    }

    // -----------------------------------------------------------------------
    // Streaming receive helpers
    // -----------------------------------------------------------------------

    /**
     * Receive binary frames from the server and write them directly to [outputStream],
     * never accumulating the entire file in memory.
     */
    /**
     * Receive binary frames from the server and write them directly to [outputStream],
     * never accumulating the entire file in memory. Computes MD5 digest incrementally.
     *
     * @return Pair of (bytes received, MD5 hex string)
     */
    private fun streamChunksToOutput(
        server: SocketServer,
        outputStream: OutputStream,
        targetSize: Long,
    ): Pair<Long, String> {
        var received = 0L
        val md5 = MessageDigest.getInstance("MD5")
        outputStream.use { os ->
            while (received < targetSize) {
                val chunk = try {
                    server.receiveBinaryFrame()
                } catch (e: java.io.EOFException) {
                    Log.e(TAG, "file_push: connection closed after $received/$targetSize bytes")
                    break
                } catch (e: IOException) {
                    Log.e(TAG, "file_push: I/O error after $received/$targetSize bytes: ${e.message}")
                    break
                }
                os.write(chunk)
                md5.update(chunk)
                received += chunk.size
                Log.v(TAG, "file_push: received $received/$targetSize bytes")
            }
        }
        val md5Hex = md5.digest().joinToString("") { "%02x".format(it) }
        return Pair(received, md5Hex)
    }

    // -----------------------------------------------------------------------
    // MediaStore injection
    // -----------------------------------------------------------------------

    private fun streamToMediaStore(
        server: SocketServer,
        filename: String,
        targetSize: Long,
        dateTakenMs: Long?,
        collectionUri: Uri,
        mimeBase: String,
        defaultMime: String,
    ): Pair<Long, String>? {
        val mime = guessMimeType(filename, mimeBase) ?: defaultMime

        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, filename)
            put(MediaStore.MediaColumns.MIME_TYPE, mime)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                put(MediaStore.MediaColumns.RELATIVE_PATH, MEDIA_RELATIVE_PATH)
                put(MediaStore.MediaColumns.IS_PENDING, 1)
            }
            dateTakenMs?.let {
                put("datetaken", it)
            }
        }

        var itemUri: Uri? = null
        return try {
            itemUri = context.contentResolver.insert(collectionUri, values)
                ?: return null

            val os = context.contentResolver.openOutputStream(itemUri)
                ?: return null

            val (received, md5Hex) = streamChunksToOutput(server, os, targetSize)
            Log.i(TAG, "streamToMediaStore: $filename  md5=$md5Hex")

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val update = ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }
                context.contentResolver.update(itemUri, update, null, null)
            }

            Pair(received, md5Hex)
        } catch (e: Exception) {
            Log.e(TAG, "streamToMediaStore failed for $filename: ${e.message}")
            // Clear IS_PENDING on failure so the entry doesn't linger as a ghost
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && itemUri != null) {
                try {
                    context.contentResolver.delete(itemUri, null, null)
                } catch (cleanup: Exception) {
                    Log.w(TAG, "streamToMediaStore cleanup failed: ${cleanup.message}")
                }
            }
            null
        }
    }

    @Suppress("DEPRECATION")
    private fun streamToDownloads(server: SocketServer, filename: String, targetSize: Long): Pair<Long, String>? {
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val mime = guessMimeType(filename, null) ?: "application/octet-stream"
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, filename)
                    put(MediaStore.Downloads.MIME_TYPE, mime)
                    put(MediaStore.Downloads.IS_PENDING, 1)
                }
                val uri = context.contentResolver.insert(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, values
                ) ?: return null

                try {
                    val os = context.contentResolver.openOutputStream(uri)
                        ?: return null

                    val (received, md5Hex) = streamChunksToOutput(server, os, targetSize)
                    Log.i(TAG, "streamToDownloads(Q+): $filename  md5=$md5Hex")

                    val update = ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }
                    context.contentResolver.update(uri, update, null, null)

                    Pair(received, md5Hex)
                } catch (e: Exception) {
                    // Clean up the pending entry on failure
                    try { context.contentResolver.delete(uri, null, null) }
                    catch (_: Exception) {}
                    throw e
                }
            } else {
                val downloadsDir = android.os.Environment.getExternalStoragePublicDirectory(
                    android.os.Environment.DIRECTORY_DOWNLOADS
                )
                downloadsDir.mkdirs()
                val outFile = File(downloadsDir, filename)
                val os = outFile.outputStream()
                val (received, md5Hex) = streamChunksToOutput(server, os, targetSize)
                Log.i(TAG, "streamToDownloads(legacy): $filename  md5=$md5Hex")

                android.media.MediaScannerConnection.scanFile(
                    context, arrayOf(outFile.absolutePath), null, null
                )

                Pair(received, md5Hex)
            }
        } catch (e: Exception) {
            Log.e(TAG, "streamToDownloads failed for $filename: ${e.message}")
            null
        }
    }

    private fun guessMimeType(filename: String, mimeBase: String?): String? {
        val ext = filename.substringAfterLast('.', "").lowercase().ifEmpty { return null }
        val guess = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: return null
        return if (mimeBase != null && !guess.startsWith("$mimeBase/")) null else guess
    }
}
