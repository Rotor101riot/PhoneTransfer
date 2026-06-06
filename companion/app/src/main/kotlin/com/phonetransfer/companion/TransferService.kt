package com.phonetransfer.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.phonetransfer.companion.handlers.registerAllHandlers
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel

private const val TAG = "TransferService"

const val ACTION_STATUS = "com.phonetransfer.companion.STATUS"
const val EXTRA_CONNECTED = "connected"
const val EXTRA_ADDRESS = "address"
const val EXTRA_CATEGORY = "category"
const val EXTRA_PROGRESS = "progress"
const val EXTRA_TOTAL = "total"

private const val NOTIFICATION_ID = 1001
private const val CHANNEL_ID = "transfer_channel"
private const val CHANNEL_NAME = "PhoneTransfer"

/**
 * Foreground service that owns the [SocketServer] lifecycle.
 *
 * Start it with a plain [startForegroundService] / [startService] intent.
 * Stop it by sending an intent with action [ACTION_STOP_SERVICE], or by
 * calling [stopSelf] / [stopService] from any component.
 */
class TransferService : Service() {

    companion object {
        const val ACTION_STOP_SERVICE = "com.phonetransfer.companion.STOP_SERVICE"

        /**
         * True while this service is alive (onCreate..onDestroy).
         * Read by MainActivity to decide whether to auto-start and which UI state to show.
         * Volatile so reads from the main thread see the value written on the IO thread.
         */
        @Volatile
        var isRunning: Boolean = false
            private set

        /**
         * True when a PC client is currently connected to the socket server.
         * Updated on every connection/disconnection event so MainActivity can
         * read the current state in onResume() without waiting for a broadcast.
         */
        @Volatile
        var isConnected: Boolean = false
            private set

        /**
         * The IP address of the currently connected PC client, or null when
         * no client is connected.
         */
        @Volatile
        var connectedAddress: String? = null
            private set
    }

    // ------------------------------------------------------------------
    // Internal state
    // ------------------------------------------------------------------

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var server: SocketServer
    private lateinit var broadcaster: LocalBroadcastManager
    private lateinit var discoveryManager: DiscoveryManager
    private var wakeLock: PowerManager.WakeLock? = null

    // ------------------------------------------------------------------
    // Service lifecycle
    // ------------------------------------------------------------------

    override fun onCreate() {
        super.onCreate()
        isRunning = true

        // Acquire a partial wake lock so the CPU stays awake for the full
        // transfer even if the screen turns off.  Capped at 4 hours —
        // longer than any realistic transfer — so it is always released.
        val pm = getSystemService(PowerManager::class.java)
        wakeLock = pm?.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "PhoneTransfer:TransferServiceLock",
        )?.also { it.acquire(4 * 60 * 60 * 1000L) }

        server = SocketServer(appContext = applicationContext)
        broadcaster = LocalBroadcastManager.getInstance(this)
        discoveryManager = DiscoveryManager(this)

        createNotificationChannel()

        // Wire up connection status → notification + broadcast
        server.onStatusChange = { connected, address ->
            broadcastStatus(connected = connected, address = address)
            updateNotification(
                if (connected) "Connected: $address" else "Waiting for connection…"
            )
        }

        // Mirror every progress frame sent to the PC into the local UI and notification.
        // Handlers call server.sendProgress() directly; this callback ensures the
        // service layer also hears about it without coupling handlers to TransferService.
        server.onProgressSent = { category, done, total ->
            broadcastProgress(category, done, total)
            updateNotification("Transferring: $category ($done/$total)")
        }

        // Register all category handlers
        server.registerAllHandlers(context = this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP_SERVICE) {
            Log.i(TAG, "Stop intent received")
            stopForegroundService()
            return START_NOT_STICKY
        }

        val notification = buildNotification("Waiting for connection…")
        
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, 
                notification, 
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }

        server.start(serviceScope)
        discoveryManager.register()
        Log.i(TAG, "TransferService started")

        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        isRunning    = false
        isConnected  = false
        connectedAddress = null
        discoveryManager.unregister()
        server.stop()
        serviceScope.cancel()
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        Log.i(TAG, "TransferService destroyed")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun stopForegroundService() {
        discoveryManager.unregister()
        server.stop()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
        stopSelf()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                CHANNEL_NAME,
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "PhoneTransfer data transfer notifications"
            }
            val nm = getSystemService(NotificationManager::class.java)
            nm?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): android.app.Notification {
        val stopIntent = Intent(this, TransferService::class.java).apply {
            action = ACTION_STOP_SERVICE
        }
        val stopPendingIntent = PendingIntent.getService(
            this, 0, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val openIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val openPendingIntent = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("PhoneTransfer Companion")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setContentIntent(openPendingIntent)
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                "Stop",
                stopPendingIntent
            )
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm?.notify(NOTIFICATION_ID, buildNotification(text))
    }

    // ------------------------------------------------------------------
    // Broadcasts
    // ------------------------------------------------------------------

    private fun broadcastStatus(connected: Boolean, address: String?) {
        isConnected = connected
        connectedAddress = if (connected) address else null
        broadcaster.sendBroadcast(Intent(ACTION_STATUS).apply {
            putExtra(EXTRA_CONNECTED, connected)
            if (address != null) putExtra(EXTRA_ADDRESS, address)
        })
    }

    private fun broadcastProgress(category: String, done: Int, total: Int) {
        broadcaster.sendBroadcast(Intent(ACTION_STATUS).apply {
            putExtra(EXTRA_CONNECTED, true)
            putExtra(EXTRA_CATEGORY, category)
            putExtra(EXTRA_PROGRESS, done)
            putExtra(EXTRA_TOTAL, total)
        })
    }
}
