package com.phonetransfer.companion.protocol

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.util.Log
import com.phonetransfer.companion.SocketServer
import java.util.concurrent.CopyOnWriteArraySet

private const val TAG = "EventManager"

/**
 * Manages v2 event subscriptions and pushes device state changes to the
 * connected PC client.
 *
 * Inspired by the Wondershare/Dr.Fone XMPP-style push notification system
 * where the daemon pushes battery/storage/screen events without polling.
 *
 * Usage:
 * 1. PC sends `{"cmd": "subscribe", "ns": ["battery", "storage"]}`.
 * 2. APK registers OS-level listeners for those namespaces.
 * 3. APK pushes `{"_type": "event", "ns": "battery", "data": {…}}` whenever
 *    the subscribed state changes.
 * 4. PC sends `{"cmd": "unsubscribe", "ns": ["battery"]}` or disconnect
 *    clears all subscriptions.
 */
class EventManager(
    private val context: Context,
    private val server: SocketServer,
) {
    private val subscribedNamespaces = CopyOnWriteArraySet<String>()
    private var batteryReceiver: BroadcastReceiver? = null
    private var screenReceiver: BroadcastReceiver? = null

    /** Currently active subscriptions. */
    val subscriptions: Set<String> get() = subscribedNamespaces.toSet()

    // ------------------------------------------------------------------
    // Subscribe / Unsubscribe
    // ------------------------------------------------------------------

    fun subscribe(namespaces: List<String>) {
        for (ns in namespaces) {
            if (ns !in EventNamespace.ALL) {
                Log.w(TAG, "Ignoring unknown namespace: $ns")
                continue
            }
            if (subscribedNamespaces.add(ns)) {
                Log.i(TAG, "Subscribed: $ns")
                registerListener(ns)
                // Push initial state immediately on subscribe
                pushCurrentState(ns)
            }
        }
    }

    fun unsubscribe(namespaces: List<String>) {
        for (ns in namespaces) {
            if (subscribedNamespaces.remove(ns)) {
                Log.i(TAG, "Unsubscribed: $ns")
                unregisterListener(ns)
            }
        }
    }

    /**
     * Clear all subscriptions and unregister all OS listeners.
     * Called on client disconnect.
     */
    fun clearAll() {
        for (ns in subscribedNamespaces.toList()) {
            unregisterListener(ns)
        }
        subscribedNamespaces.clear()
        Log.i(TAG, "All subscriptions cleared")
    }

    // ------------------------------------------------------------------
    // Push an event to the PC
    // ------------------------------------------------------------------

    private fun pushEvent(ns: String, data: Map<String, Any?>) {
        if (ns !in subscribedNamespaces) return
        val json = Response.event(ns, data)
        server.sendJsonFrame(json)
        Log.d(TAG, "Pushed event: ns=$ns")
    }

    // ------------------------------------------------------------------
    // Push current state (on subscribe, gives PC the baseline)
    // ------------------------------------------------------------------

    private fun pushCurrentState(ns: String) {
        when (ns) {
            EventNamespace.BATTERY -> pushBatteryState()
            EventNamespace.STORAGE -> pushStorageState()
            EventNamespace.SCREEN  -> pushScreenState()
            // NOTIFY, APP, NETWORK — no initial state to push
        }
    }

    // ------------------------------------------------------------------
    // Battery
    // ------------------------------------------------------------------

    private fun pushBatteryState() {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
        val level = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: -1
        val intentFilter = IntentFilter(Intent.ACTION_BATTERY_CHANGED)
        val batteryStatus = context.registerReceiver(null, intentFilter)
        val plugged = batteryStatus?.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1) ?: -1
        val charging = plugged == BatteryManager.BATTERY_PLUGGED_AC
                || plugged == BatteryManager.BATTERY_PLUGGED_USB
                || plugged == BatteryManager.BATTERY_PLUGGED_WIRELESS
        val temp = (batteryStatus?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0) / 10.0

        pushEvent(EventNamespace.BATTERY, mapOf(
            "level" to level,
            "charging" to charging,
            "plugged" to when (plugged) {
                BatteryManager.BATTERY_PLUGGED_AC -> "ac"
                BatteryManager.BATTERY_PLUGGED_USB -> "usb"
                BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
                else -> "none"
            },
            "temperature" to temp
        ))
    }

    private fun registerBatteryListener() {
        if (batteryReceiver != null) return
        batteryReceiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context?, intent: Intent?) {
                pushBatteryState()
            }
        }
        context.registerReceiver(batteryReceiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        Log.d(TAG, "Battery listener registered")
    }

    private fun unregisterBatteryListener() {
        batteryReceiver?.let {
            try { context.unregisterReceiver(it) } catch (_: Exception) {}
            batteryReceiver = null
            Log.d(TAG, "Battery listener unregistered")
        }
    }

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------

    private fun pushStorageState() {
        try {
            val stat = StatFs(Environment.getDataDirectory().path)
            val totalBytes = stat.blockSizeLong * stat.blockCountLong
            val freeBytes = stat.blockSizeLong * stat.availableBlocksLong
            val usedBytes = totalBytes - freeBytes

            val extStat = StatFs(Environment.getExternalStorageDirectory().path)
            val extTotal = extStat.blockSizeLong * extStat.blockCountLong
            val extFree = extStat.blockSizeLong * extStat.availableBlocksLong

            pushEvent(EventNamespace.STORAGE, mapOf(
                "internal_total" to totalBytes,
                "internal_used" to usedBytes,
                "internal_free" to freeBytes,
                "external_total" to extTotal,
                "external_free" to extFree,
                "external_used" to extTotal - extFree
            ))
        } catch (e: Exception) {
            Log.w(TAG, "Failed to read storage stats: ${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // Screen
    // ------------------------------------------------------------------

    private fun pushScreenState() {
        val pm = context.getSystemService(Context.POWER_SERVICE) as? android.os.PowerManager
        val interactive = pm?.isInteractive ?: true
        pushEvent(EventNamespace.SCREEN, mapOf(
            "on" to interactive
        ))
    }

    private fun registerScreenListener() {
        if (screenReceiver != null) return
        screenReceiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context?, intent: Intent?) {
                val on = intent?.action == Intent.ACTION_SCREEN_ON
                pushEvent(EventNamespace.SCREEN, mapOf("on" to on))
            }
        }
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
        }
        context.registerReceiver(screenReceiver, filter)
        Log.d(TAG, "Screen listener registered")
    }

    private fun unregisterScreenListener() {
        screenReceiver?.let {
            try { context.unregisterReceiver(it) } catch (_: Exception) {}
            screenReceiver = null
            Log.d(TAG, "Screen listener unregistered")
        }
    }

    // ------------------------------------------------------------------
    // Listener dispatch
    // ------------------------------------------------------------------

    private fun registerListener(ns: String) {
        when (ns) {
            EventNamespace.BATTERY -> registerBatteryListener()
            EventNamespace.SCREEN  -> registerScreenListener()
            // STORAGE, NOTIFY, APP, NETWORK — polled on demand or future OS callbacks
        }
    }

    private fun unregisterListener(ns: String) {
        when (ns) {
            EventNamespace.BATTERY -> unregisterBatteryListener()
            EventNamespace.SCREEN  -> unregisterScreenListener()
        }
    }
}
