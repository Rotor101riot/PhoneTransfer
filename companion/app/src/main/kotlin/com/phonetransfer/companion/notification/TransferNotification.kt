package com.phonetransfer.companion.notification

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.phonetransfer.companion.MainActivity
import com.phonetransfer.companion.R

object TransferNotification {

    const val CHANNEL_ID = "transfer_channel"
    const val NOTIFICATION_ID = 1001

    fun createChannel(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Shows transfer progress from PhoneTransfer Companion"
                enableVibration(false)
                setSound(null, null)
            }
            val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    private fun mainActivityPendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        return PendingIntent.getActivity(
            context,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun baseBuilder(context: Context): NotificationCompat.Builder {
        return NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_transfer)
            .setContentIntent(mainActivityPendingIntent(context))
            .setSilent(true)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
    }

    fun buildWaiting(context: Context): Notification {
        return baseBuilder(context)
            .setContentTitle(context.getString(R.string.app_name))
            .setContentText(context.getString(R.string.notification_waiting))
            .build()
    }

    fun buildConnected(context: Context, address: String): Notification {
        return baseBuilder(context)
            .setContentTitle(context.getString(R.string.app_name))
            .setContentText(context.getString(R.string.notification_connected, address))
            .build()
    }

    fun buildProgress(context: Context, category: String, done: Int, total: Int): Notification {
        return baseBuilder(context)
            .setContentTitle(context.getString(R.string.status_transferring, category))
            .setContentText("$done of $total items")
            .setProgress(total, done, false)
            .build()
    }

    fun buildDone(context: Context): Notification {
        return baseBuilder(context)
            .setContentTitle(context.getString(R.string.status_done))
            .setContentText(context.getString(R.string.notification_done))
            .setOngoing(false)
            .build()
    }

    fun update(context: Context, notification: Notification) {
        NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, notification)
    }
}
