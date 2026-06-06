package com.phonetransfer.companion.handlers

import android.content.Context
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import com.phonetransfer.companion.protocol.SUPPORTED_CATEGORIES
import com.phonetransfer.companion.protocol.SUPPORTED_MEDIA_TYPES
import com.phonetransfer.companion.protocol.SUPPORTED_WALLPAPER
import com.phonetransfer.companion.protocol.PROTOCOL_VERSION
import com.phonetransfer.companion.protocol.EventNamespace
import java.io.File

/**
 * Registers all category handlers into the SocketServer's handler map.
 */
fun SocketServer.registerAllHandlers(context: Context) {
    val registry = handlers

    // ── Base Commands ───────────────────────────────────────────────────────
    
    registry["ping"] = { cmd, _ ->
        Response.ok(cmd["cmd"] as? String ?: "ping")
    }

    registry["capabilities"] = { cmd, _ ->
        val payload = mapOf(
            "categories" to SUPPORTED_CATEGORIES,
            "media_types" to SUPPORTED_MEDIA_TYPES + listOf("playlists"),
            "wallpaper" to SUPPORTED_WALLPAPER,
            "file_transfer" to "socket",   // primary method; adb is PC-side fallback
            "file_transfer_resume" to true, // offset-based resume support (Phase 4)
            "root" to isRooted(),
            "protocol_version" to PROTOCOL_VERSION,
            "device_info" to true,
            "mms_part_pull" to true,
            "sms_role_management" to true,
            "sms_role_xiaomi" to true,      // MIUI-aware SMS role flow (Phase 5)
            "event_namespaces" to EventNamespace.ALL,
            "oem_skin" to detectOemSkin()   // EMUI/MIUI/HyperOS/OneUI (Phase 5)
        )
        Response.ok(cmd["cmd"] as? String ?: "capabilities", payload)
    }

    // ── Dispatchers ─────────────────────────────────────────────────────────

    registry["extract"] = { params, server ->
        val category = params["category"] as? String ?: ""
        val handlerKey = if (category == "calls") "extract_call_log" else "extract_$category"
        val handler = registry[handlerKey]
        if (handler != null) {
            handler(params, server)
        } else {
            Response.error("extract", "unknown_category", "Category '$category' not supported")
        }
    }

    registry["inject"] = { params, server ->
        val category = params["category"] as? String ?: ""
        val handlerKey = if (category == "calls") "inject_call_log" else "inject_$category"
        val handler = registry[handlerKey]
        if (handler != null) {
            handler(params, server)
        } else {
            Response.error("inject", "unknown_category", "Category '$category' not supported")
        }
    }

    // ── Category Handlers ───────────────────────────────────────────────────
    
    ContactsHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    SmsHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    CallLogHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    CalendarHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    AlarmsHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    BlockedHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    ContactGroupsHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    InstalledAppsHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    ClipboardHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    BrowserHistoryHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    BookmarksHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    NotesHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    RemindersHandler(context).apply {
        registerExtract(registry)
        registerInject(registry)
    }

    MailAccountsHandler(context).registerExtract(registry)

    MediaHandler(context).registerExtract(registry)
    RootHandler(context).registerExtract(registry)
    FileTransferHandler(context).register(registry)
    WallpaperHandler(context).register(registry)
    DeviceInfoHandler(context).register(registry)
}

/**
 * Detect the OEM Android skin (EMUI, MIUI, HyperOS, OneUI, etc.)
 * by reading system properties via reflection.
 *
 * Returns a map with the detected skin name and version, or an empty
 * map for stock AOSP / unknown skins.
 */
private fun detectOemSkin(): Map<String, String> {
    val result = mutableMapOf<String, String>()

    fun getProp(key: String): String? {
        return try {
            val clazz = Class.forName("android.os.SystemProperties")
            val method = clazz.getMethod("get", String::class.java, String::class.java)
            val value = method.invoke(null, key, "") as? String
            if (value.isNullOrEmpty()) null else value
        } catch (_: Exception) {
            null
        }
    }

    // Huawei EMUI
    getProp("ro.build.version.emui")?.let {
        result["skin"] = "emui"
        result["emui_version"] = it
    }

    // Xiaomi MIUI
    getProp("ro.miui.ui.version.name")?.let {
        result["skin"] = "miui"
        result["miui_version"] = it
        // Check MIUI version code (e.g. "V14" → 14)
        getProp("ro.miui.ui.version.code")?.let { code ->
            result["miui_version_code"] = code
        }
    }

    // Xiaomi HyperOS (MIUI successor)
    getProp("ro.mi.os.version.name")?.let {
        result["skin"] = "hyperos"
        result["hyperos_version"] = it
    }

    // Samsung OneUI
    getProp("ro.build.version.oneui")?.let {
        result["skin"] = "oneui"
        result["oneui_version"] = it
    }

    // ColorOS (OPPO/Realme/OnePlus)
    getProp("ro.build.version.oplusrom")?.let {
        result["skin"] = "coloros"
        result["coloros_version"] = it
    }

    // OxygenOS / ColorOS on OnePlus
    getProp("ro.oxygen.version")?.let {
        result["skin"] = "oxygenos"
        result["oxygenos_version"] = it
    }

    return result
}

private fun isRooted(): Boolean {
    val binaryPaths = arrayOf(
        "/system/app/Superuser.apk",
        "/sbin/su",
        "/system/bin/su",
        "/system/xbin/su",
        "/data/local/xbin/su",
        "/data/local/bin/su",
        "/system/sd/xbin/su",
        "/working/bin/su",
        "/system/bin/failsafe/su",
        "/data/local/su"
    )
    return binaryPaths.any { File(it).exists() }
}
