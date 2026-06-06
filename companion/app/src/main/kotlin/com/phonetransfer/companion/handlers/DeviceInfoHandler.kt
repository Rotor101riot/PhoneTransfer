package com.phonetransfer.companion.handlers

import android.app.ActivityManager
import android.app.usage.StorageStatsManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.os.storage.StorageManager
import android.provider.MediaStore
import android.util.Log
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.File
import java.util.UUID

private const val TAG = "DeviceInfoHandler"

/**
 * Reports comprehensive device information including per-type storage
 * breakdown, modelled after the SocketDeviceInfo properties observed in
 * the Wondershare/Dr.Fone Android device interface DLL analysis:
 *
 * - IMEI, model, manufacturer, OS version
 * - Battery level, charging state
 * - Total/Used RAM
 * - Total/Used storage with per-type breakdown:
 *   App, Audio, Video, Image, Document, System, Other
 */
class DeviceInfoHandler(private val context: Context) {

    fun register(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["device_info"] = { _, _ ->
            handleDeviceInfo()
        }
    }

    private fun handleDeviceInfo(): String {
        val info = mutableMapOf<String, Any?>()

        // ── Device identity ──
        info["manufacturer"] = Build.MANUFACTURER
        info["model"] = Build.MODEL
        info["brand"] = Build.BRAND
        info["device"] = Build.DEVICE
        info["product"] = Build.PRODUCT
        info["os_version"] = Build.VERSION.RELEASE
        info["sdk_int"] = Build.VERSION.SDK_INT
        info["build_display"] = Build.DISPLAY
        info["serial"] = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Build.getSerial() else Build.SERIAL
        } catch (e: SecurityException) { "unavailable" }

        // ── OEM skin detection (Phase 5, Item #15) ──
        // Only include non-null values to keep the response clean
        getSystemProperty("ro.build.version.emui")?.let { info["emui_version"] = it }
        getSystemProperty("ro.miui.ui.version.name")?.let { info["miui_version"] = it }
        getSystemProperty("ro.mi.os.version.name")?.let { info["hyperos_version"] = it }
        getSystemProperty("ro.build.version.oneui")?.let { info["oneui_version"] = it }
        getSystemProperty("ro.build.version.oplusrom")?.let { info["coloros_version"] = it }
        getSystemProperty("ro.oxygen.version")?.let { info["oxygenos_version"] = it }

        // ── Battery ──
        try {
            val bm = context.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
            info["battery_level"] = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            val batteryIntent = context.registerReceiver(null,
                android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED))
            val plugged = batteryIntent?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
            info["battery_charging"] = plugged != 0
            info["battery_temperature"] = (batteryIntent?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0) / 10.0
        } catch (e: Exception) {
            Log.w(TAG, "Battery info error: ${e.message}")
        }

        // ── RAM ──
        try {
            val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            val memInfo = ActivityManager.MemoryInfo()
            am.getMemoryInfo(memInfo)
            info["ram_total"] = memInfo.totalMem
            info["ram_available"] = memInfo.availMem
            info["ram_used"] = memInfo.totalMem - memInfo.availMem
            info["ram_low_memory"] = memInfo.lowMemory
        } catch (e: Exception) {
            Log.w(TAG, "RAM info error: ${e.message}")
        }

        // ── Storage (internal) ──
        try {
            val stat = StatFs(Environment.getDataDirectory().path)
            val totalBytes = stat.blockSizeLong * stat.blockCountLong
            val freeBytes = stat.blockSizeLong * stat.availableBlocksLong
            info["storage_internal_total"] = totalBytes
            info["storage_internal_free"] = freeBytes
            info["storage_internal_used"] = totalBytes - freeBytes
        } catch (e: Exception) {
            Log.w(TAG, "Internal storage error: ${e.message}")
        }

        // ── Storage (external/SD) ──
        try {
            val extStat = StatFs(Environment.getExternalStorageDirectory().path)
            info["storage_external_total"] = extStat.blockSizeLong * extStat.blockCountLong
            info["storage_external_free"] = extStat.blockSizeLong * extStat.availableBlocksLong
            info["storage_external_used"] = (info["storage_external_total"] as Long) - (info["storage_external_free"] as Long)
        } catch (e: Exception) {
            Log.w(TAG, "External storage error: ${e.message}")
        }

        // ── Per-type storage breakdown (API 26+) ──
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            try {
                val ssm = context.getSystemService(Context.STORAGE_STATS_SERVICE) as? StorageStatsManager
                val sm = context.getSystemService(Context.STORAGE_SERVICE) as? StorageManager
                if (ssm != null && sm != null) {
                    val uuid = StorageManager.UUID_DEFAULT
                    info["storage_app_bytes"] = ssm.queryStatsForUid(uuid, android.os.Process.myUid()).appBytes
                    // Total stats for the primary volume
                    info["storage_total_bytes"] = ssm.getTotalBytes(uuid)
                    info["storage_free_bytes"] = ssm.getFreeBytes(uuid)
                }
            } catch (e: Exception) {
                Log.w(TAG, "StorageStatsManager error: ${e.message}")
            }
        }

        // ── Per-media-type storage breakdown via MediaStore aggregation ──
        info["storage_by_type"] = queryMediaStorageSizes()

        // ── App count ──
        try {
            val pm = context.packageManager
            val installedApps = pm.getInstalledApplications(PackageManager.GET_META_DATA)
            info["installed_app_count"] = installedApps.size
            info["installed_app_user_count"] = installedApps.count { app ->
                (app.flags and android.content.pm.ApplicationInfo.FLAG_SYSTEM) == 0
            }
        } catch (e: Exception) {
            Log.w(TAG, "App count error: ${e.message}")
        }

        return Response.ok("device_info", info)
    }

    /**
     * Query MediaStore to aggregate file sizes by media type.
     * Returns a map like: {"images": 1234567890, "videos": 9876543210, ...}
     */
    private fun queryMediaStorageSizes(): Map<String, Long> {
        val sizes = mutableMapOf<String, Long>()

        fun sumSize(uri: android.net.Uri, label: String) {
            try {
                context.contentResolver.query(
                    uri,
                    arrayOf("SUM(${MediaStore.MediaColumns.SIZE}) as total_size"),
                    null, null, null
                )?.use { cursor ->
                    if (cursor.moveToFirst()) {
                        sizes[label] = cursor.getLong(0)
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "MediaStore size query for $label: ${e.message}")
            }
        }

        sumSize(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, "images")
        sumSize(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, "videos")
        sumSize(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, "audio")

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            sumSize(MediaStore.Downloads.EXTERNAL_CONTENT_URI, "downloads")
        }

        // Documents — scan common document directories
        try {
            val docDirs = listOf(
                File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS).path),
                File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).path)
            )
            var docSize = 0L
            for (dir in docDirs) {
                if (dir.exists()) {
                    docSize += dir.walkTopDown()
                        .filter { it.isFile }
                        .sumOf { it.length() }
                }
            }
            sizes["documents"] = docSize
        } catch (e: Exception) {
            Log.w(TAG, "Document size scan error: ${e.message}")
        }

        return sizes
    }

    private fun getSystemProperty(key: String): String? {
        return try {
            val clazz = Class.forName("android.os.SystemProperties")
            val method = clazz.getMethod("get", String::class.java, String::class.java)
            val value = method.invoke(null, key, "") as? String
            if (value.isNullOrEmpty()) null else value
        } catch (_: Exception) {
            null
        }
    }
}
