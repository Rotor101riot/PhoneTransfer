package com.phonetransfer.companion.protocol

import com.google.gson.Gson
import com.google.gson.reflect.TypeToken

// ---------------------------------------------------------------------------
// Protocol version
// ---------------------------------------------------------------------------

/**
 * Current protocol version.  Negotiated during the handshake:
 * - PC sends `{"cmd": "hello", "_v": 2}`.
 * - APK responds with `{"status": "ok", "cmd": "hello", "_v": 2, ...}`.
 * - If the PC omits `_v` the APK treats the session as v1 (legacy).
 *
 * v1: flat JSON commands, no sequence IDs, no event subscriptions.
 * v2: adds `_v`, `_type`, `_seq` fields; event subscriptions; heartbeat.
 */
const val PROTOCOL_VERSION = 2

// ---------------------------------------------------------------------------
// Message types  (v2 `_type` field)
// ---------------------------------------------------------------------------

/**
 * Message type constants used in the `_type` field of v2 frames.
 *
 * Modelled after XMPP stanza types observed in the Wondershare/Dr.Fone
 * protocol analysis:
 * - **iq**: Info/Query — request/response pairs (like XMPP `<iq>`).
 * - **msg**: Unsolicited push from APK to PC (like XMPP `<message>`).
 * - **event**: Device state change notification (battery, storage, screen, …).
 *
 * v1 frames have no `_type`; the server infers `iq` for backward compat.
 */
object MessageType {
    const val IQ    = "iq"     // request → response (default for v1 compat)
    const val MSG   = "msg"    // unsolicited push (progress, status updates)
    const val EVENT = "event"  // device-state change notification
}

// ---------------------------------------------------------------------------
// Event namespaces  (v2 event system)
// ---------------------------------------------------------------------------

/**
 * Namespace strings for the event subscription / push system.
 * The PC subscribes with `{"cmd": "subscribe", "ns": ["battery", "storage"]}`.
 * The APK pushes events as `{"_type": "event", "ns": "battery", "data": {…}}`.
 */
object EventNamespace {
    const val BATTERY  = "battery"   // level, charging state, temperature
    const val STORAGE  = "storage"   // total/used per type (app, photo, video, …)
    const val SCREEN   = "screen"    // on/off/locked
    const val NOTIFY   = "notify"    // generic notification from device
    const val APP      = "app"       // app install/uninstall/update
    const val NETWORK  = "network"   // wifi/mobile connectivity changes

    val ALL = listOf(BATTERY, STORAGE, SCREEN, NOTIFY, APP, NETWORK)
}

// ---------------------------------------------------------------------------
// Supported categories
// ---------------------------------------------------------------------------

/**
 * Structured-data categories that have registered extract/inject handlers
 * in HandlerRegistry.  Names match the Python pipeline's ALL_CATEGORIES list.
 *
 * NOTE: "calls" maps to the "call_log" content provider on Android but the
 * category key agreed with the Python side is "calls" — handler keys are
 * "extract_call_log" / "inject_call_log" (internal) but the capabilities
 * response advertises the Python-facing name.
 */
val SUPPORTED_CATEGORIES = listOf(
    "contacts",
    "contact_groups",
    "blocked",
    "sms",
    "calls",        // handler keys: extract_call_log / inject_call_log
    "calendar",
    "reminders",
    "notes",
    "alarms",
    "bookmarks",
    "browser_history",
    "clipboard",
    "installed_apps",
    "mail_accounts"
)

/** Media categories whose actual bytes are transferred via adb pull/push. */
val SUPPORTED_MEDIA_TYPES = listOf("photos", "videos", "ringtones", "voice_memos")

/**
 * True — the APK supports wallpaper extract/inject via the socket protocol
 * ([WallpaperHandler] registers `wallpaper_extract` and `wallpaper_inject`).
 * Advertised in the `capabilities` response so the PC skips the unavailable
 * warning that was previously shown for wallpaper.
 */
const val SUPPORTED_WALLPAPER = true

/** Categories not implemented in this APK (handled fully on the PC/iOS side). */
val UNSUPPORTED_CATEGORIES = listOf("whatsapp", "signal")

// ---------------------------------------------------------------------------
// PC → APK command data classes  (field: "cmd")
// ---------------------------------------------------------------------------

data class PingCommand(
    val cmd: String = "ping"
)

data class CapabilitiesCommand(
    val cmd: String = "capabilities"
)

data class ExtractCommand(
    val cmd: String = "extract",
    val category: String = "",
    val session_id: String = "",
    val limit: Int = 0
)

data class InjectCommand(
    val cmd: String = "inject",
    val category: String = "",
    val session_id: String = "",
    val data: List<Map<String, Any>> = emptyList()
)

data class MediaListCommand(
    val cmd: String = "media_list",
    /** One of: "photos", "videos", "ringtones", "voice_memos" */
    val media_type: String = "",
    val session_id: String = ""
)

data class RootExecCommand(
    val cmd: String = "root_exec",
    val command: String = "",
    val session_id: String = ""
)

data class StopCommand(
    val cmd: String = "stop"
)

data class FilePullCommand(
    val cmd: String = "file_pull",
    /** Absolute path to the file on the Android device. */
    val path: String = "",
    val session_id: String = ""
)

data class FilePushCommand(
    val cmd: String = "file_push",
    val filename: String = "",
    val size: Long = 0L,
    /** "photos", "videos", or "downloads" (default). */
    val dest: String = "downloads",
    /** Original creation timestamp in milliseconds since epoch (optional). */
    val date_taken: Long = 0L,
    val session_id: String = ""
)

data class WallpaperExtractCommand(
    val cmd: String = "wallpaper_extract",
    /** "home", "lock", or "both" (default "home"). */
    val which: String = "home",
    val session_id: String = ""
)

data class WallpaperInjectCommand(
    val cmd: String = "wallpaper_inject",
    val filename: String = "",
    val size: Long = 0L,
    /** "home", "lock", or "both" (default "home"). */
    val which: String = "home",
    val session_id: String = ""
)

// ---------------------------------------------------------------------------
// APK → PC response data classes  (field: "status")
// ---------------------------------------------------------------------------

data class OkResponse(
    val status: String = "ok",
    val cmd: String = "",
    val payload: Map<String, Any?> = emptyMap()
)

data class ErrorResponse(
    val status: String = "error",
    val cmd: String = "",
    val error: String = "",
    val message: String = ""
)

data class ProgressResponse(
    val status: String = "progress",
    val category: String = "",
    val done: Int = 0,
    val total: Int = 0
)

// v2 event push
data class EventPush(
    val _type: String = MessageType.EVENT,
    val _v: Int = PROTOCOL_VERSION,
    val ns: String = "",
    val data: Map<String, Any?> = emptyMap(),
    val timestamp: Long = System.currentTimeMillis()
)

// ---------------------------------------------------------------------------
// CommandParser  — parses raw JSON into a generic map for dispatch
// ---------------------------------------------------------------------------

object CommandParser {
    private val gson = Gson()
    private val mapType = object : TypeToken<Map<String, Any?>>() {}.type

    /**
     * Parse a raw JSON frame body into a [Map<String, Any?>].
     * The "cmd" key is used by [SocketServer] to route to the correct handler.
     */
    fun parse(json: String): Map<String, Any?> {
        return gson.fromJson(json, mapType) ?: emptyMap()
    }
}

// ---------------------------------------------------------------------------
// Response  — builder helpers that return serialised JSON strings
// ---------------------------------------------------------------------------

object Response {
    private val gson = Gson()

    /**
     * Build a successful response.
     *
     * @param cmd     The command name being acknowledged.
     * @param payload Arbitrary key/value pairs merged into the top-level object.
     * @param seq     Sequence ID from the request (v2); 0 omits the field.
     */
    fun ok(cmd: String, payload: Map<String, Any?> = emptyMap(), seq: Int = 0): String {
        val map = mutableMapOf<String, Any?>(
            "status" to "ok",
            "cmd" to cmd
        )
        if (seq > 0) {
            map["_seq"] = seq
            map["_v"] = PROTOCOL_VERSION
            map["_type"] = MessageType.IQ
        }
        map.putAll(payload)
        return gson.toJson(map)
    }

    /**
     * Build an error response.
     *
     * @param cmd     The command that failed.
     * @param code    Short machine-readable error code (e.g. "permission_denied").
     * @param msg     Human-readable description.
     * @param seq     Sequence ID from the request (v2); 0 omits the field.
     */
    fun error(cmd: String, code: String, msg: String, seq: Int = 0): String {
        val map = mutableMapOf<String, Any?>(
            "status" to "error",
            "cmd" to cmd,
            "error" to code,
            "message" to msg
        )
        if (seq > 0) {
            map["_seq"] = seq
            map["_v"] = PROTOCOL_VERSION
            map["_type"] = MessageType.IQ
        }
        return gson.toJson(map)
    }

    /**
     * Build an unsolicited progress push.
     *
     * @param category Data category being transferred (e.g. "contacts").
     * @param done     Number of items completed so far.
     * @param total    Total items in the transfer.
     */
    fun progress(category: String, done: Int, total: Int): String {
        return gson.toJson(
            ProgressResponse(
                category = category,
                done = done,
                total = total
            )
        )
    }

    /**
     * Build a v2 event push frame.
     *
     * @param ns   Event namespace (e.g. "battery", "storage").
     * @param data Arbitrary event payload.
     */
    fun event(ns: String, data: Map<String, Any?>): String {
        return gson.toJson(EventPush(ns = ns, data = data))
    }
}
