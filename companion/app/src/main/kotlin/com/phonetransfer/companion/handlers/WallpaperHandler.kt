package com.phonetransfer.companion.handlers

import android.app.WallpaperManager
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.ParcelFileDescriptor
import android.graphics.drawable.BitmapDrawable
import android.os.Build
import android.util.Log
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.ByteArrayOutputStream
import java.io.IOException

private const val TAG = "WallpaperHandler"

/**
 * WallpaperHandler — handles socket-based wallpaper extract and inject.
 */
class WallpaperHandler(private val context: Context) {

    private val wallpaperManager: WallpaperManager by lazy {
        WallpaperManager.getInstance(context)
    }

    fun register(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["wallpaper_extract"] = { params, server -> handleExtract(params, server) }
        registry["wallpaper_inject"]  = { params, server -> handleInject(params, server)  }
    }

    // -----------------------------------------------------------------------
    // wallpaper_extract
    // -----------------------------------------------------------------------

    private suspend fun handleExtract(params: Map<String, Any?>, server: SocketServer): String {
        val which = (params["which"] as? String ?: "home").lowercase()

        val (pngBytes, actualWhich) = extractWallpaperBytes(which)
            ?: return Response.error(
                "wallpaper_extract", "unavailable",
                "Could not read wallpaper — WallpaperManager returned null"
            )

        val filename = "wallpaper_$actualWhich.png"
        val size = pngBytes.size.toLong()
        val chunkCount = ((size + CHUNK_SIZE - 1) / CHUNK_SIZE).toInt().coerceAtLeast(1)

        Log.i(TAG, "wallpaper_extract: which=$actualWhich  size=$size  chunks=$chunkCount")

        // ── JSON header ──────────────────────────────────────────────────────
        server.sendJsonFrame(
            Response.ok(
                "wallpaper_extract",
                mapOf(
                    "status" to "ok",
                    "filename" to filename,
                    "size" to size,
                    "chunks" to chunkCount,
                    "which" to actualWhich,
                )
            )
        )

        // ── Binary chunks ────────────────────────────────────────────────────
        var offset = 0
        var chunkIndex = 0
        while (offset < pngBytes.size) {
            val end = minOf(offset + CHUNK_SIZE, pngBytes.size)
            server.sendBinaryFrame(pngBytes.copyOfRange(offset, end))
            offset = end
            chunkIndex++
            Log.v(TAG, "wallpaper_extract: sent chunk $chunkIndex/$chunkCount")
        }

        // ── JSON done (written by handleClient) ──────────────────────────────
        Log.i(TAG, "wallpaper_extract: complete — $size bytes")
        return Response.ok("wallpaper_extract", mapOf("status" to "done", "size" to size))
    }

    private fun extractWallpaperBytes(which: String): Pair<ByteArray, String>? {
        if ((which == "lock" || which == "both") && Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            // getDrawable(int) does not exist — use getWallpaperFile(FLAG_LOCK) which
            // returns a ParcelFileDescriptor for the lock-screen slot (API 24+).
            val bitmap = try {
                val pfd: ParcelFileDescriptor? =
                    wallpaperManager.getWallpaperFile(WallpaperManager.FLAG_LOCK)
                pfd?.use { BitmapFactory.decodeFileDescriptor(it.fileDescriptor) }
            } catch (e: Exception) {
                Log.d(TAG, "Lock wallpaper unavailable: ${e.message}")
                null
            }
            if (bitmap != null) {
                return Pair(encodePng(bitmap), "lock")
            }
            if (which == "lock") {
                Log.w(TAG, "Lock wallpaper requested but unavailable; returning null")
                return null
            }
        }

        val bitmap: Bitmap = try {
            val drawable = wallpaperManager.drawable ?: return null
            (drawable as? BitmapDrawable)?.bitmap ?: return null
        } catch (e: Exception) {
            Log.e(TAG, "Home wallpaper unavailable: ${e.message}")
            return null
        }
        return Pair(encodePng(bitmap), "home")
    }

    private fun encodePng(bitmap: Bitmap): ByteArray {
        return ByteArrayOutputStream().also { bos ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, bos)
        }.toByteArray()
    }

    // -----------------------------------------------------------------------
    // wallpaper_inject
    // -----------------------------------------------------------------------

    private suspend fun handleInject(params: Map<String, Any?>, server: SocketServer): String {
        val filename = params["filename"] as? String ?: "wallpaper.png"
        val size = when (val raw = params["size"]) {
            is Number -> raw.toLong()
            is String -> raw.toLongOrNull()
            else -> null
        } ?: return Response.error("wallpaper_inject", "missing_param", "param 'size' is required")
        val which = (params["which"] as? String ?: "home").lowercase()

        if (size <= 0L) {
            return Response.error("wallpaper_inject", "invalid_param", "size must be > 0")
        }
        if (size > MAX_WALLPAPER_SIZE) {
            return Response.error(
                "wallpaper_inject", "too_large",
                "Wallpaper size ($size bytes) exceeds the ${MAX_WALLPAPER_SIZE / 1024 / 1024}MB limit"
            )
        }

        Log.i(TAG, "wallpaper_inject: filename=$filename  size=$size  which=$which")

        server.sendJsonFrame(Response.ok("wallpaper_inject", mapOf("status" to "ready")))

        val imageBytes: ByteArray
        try {
            imageBytes = receiveAllBytes(server, size)
        } catch (e: Exception) {
            Log.e(TAG, "wallpaper_inject: receive failed: ${e.message}")
            return Response.error("wallpaper_inject", "receive_error", "Failed to receive image data: ${e.message}")
        }

        Log.i(TAG, "wallpaper_inject: received ${imageBytes.size} bytes")

        val bitmap = BitmapFactory.decodeByteArray(imageBytes, 0, imageBytes.size)
            ?: return Response.error(
                "wallpaper_inject", "decode_error",
                "Could not decode image — ensure the file is a valid PNG or JPEG"
            )

        val appliedWhich = applyWallpaper(bitmap, which)
            ?: return Response.error(
                "wallpaper_inject", "set_error",
                "WallpaperManager.setBitmap failed — check SET_WALLPAPER permission"
            )

        Log.i(TAG, "wallpaper_inject: wallpaper set successfully (which=$appliedWhich)")
        return Response.ok("wallpaper_inject", mapOf("status" to "done", "which" to appliedWhich))
    }

    private fun applyWallpaper(bitmap: Bitmap, which: String): String? {
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                val flag = when (which) {
                    "lock" -> WallpaperManager.FLAG_LOCK
                    "both" -> WallpaperManager.FLAG_SYSTEM or WallpaperManager.FLAG_LOCK
                    else   -> WallpaperManager.FLAG_SYSTEM
                }
                wallpaperManager.setBitmap(bitmap, null, true, flag)
                when (which) {
                    "lock" -> "lock"
                    "both" -> "both"
                    else   -> "home"
                }
            } else {
                @Suppress("DEPRECATION")
                wallpaperManager.setBitmap(bitmap)
                "home"
            }
        } catch (e: IOException) {
            Log.e(TAG, "applyWallpaper failed: ${e.message}")
            null
        }
    }

    // -----------------------------------------------------------------------
    // Shared helpers
    // -----------------------------------------------------------------------

    private fun receiveAllBytes(server: SocketServer, targetSize: Long): ByteArray {
        val out = ByteArrayOutputStream(targetSize.coerceAtMost(Int.MAX_VALUE.toLong()).toInt())
        var received = 0L
        while (received < targetSize) {
            val chunk = server.receiveBinaryFrame()
            out.write(chunk)
            received += chunk.size
        }
        return out.toByteArray()
    }

    companion object {
        private const val CHUNK_SIZE = 512 * 1024
        private const val MAX_WALLPAPER_SIZE = 50L * 1024 * 1024 // 50 MB
    }
}
