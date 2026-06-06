package com.phonetransfer.companion.sms

import android.content.BroadcastReceiver
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.telephony.SmsMessage
import android.util.Log

/**
 * Receives incoming SMS when this app is the default SMS handler.
 *
 * Required by Android to register as a valid SMS application — the system
 * refuses to grant the SMS role unless an app declares a receiver for
 * [android.provider.Telephony.Sms.Intents.SMS_DELIVER_ACTION].
 *
 * When invoked, it writes the incoming message into the system SMS content
 * provider so the message is not lost while we temporarily hold the role.
 * This mirrors the approach used by Wondershare's MobileGo connector.
 */
class SmsReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "SmsReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        Log.i(TAG, "SMS_DELIVER received")

        val extras = intent.extras ?: return
        val pdus = extras.get("pdus") as? Array<*> ?: return
        if (pdus.isEmpty()) return

        val contentResolver = context.contentResolver

        for (pdu in pdus) {
            try {
                val bytes = pdu as? ByteArray ?: continue
                val format = extras.getString("format") ?: "3gpp"
                val sms = SmsMessage.createFromPdu(bytes, format)

                val values = ContentValues().apply {
                    put("address", sms.originatingAddress)
                    put("body", sms.messageBody)
                    put("date", sms.timestampMillis)
                    put("read", 0)
                    put("type", 1)   // MESSAGE_TYPE_INBOX
                    put("locked", 0)
                }

                contentResolver.insert(Uri.parse("content://sms"), values)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to persist incoming SMS", e)
            }
        }
    }
}
