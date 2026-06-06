package com.phonetransfer.companion.sms

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Stub MMS receiver required by Android to register as a valid SMS application.
 *
 * The system refuses to grant the SMS role unless the app declares a receiver
 * for [android.provider.Telephony.Sms.Intents.WAP_PUSH_DELIVER_ACTION] with
 * MIME type `application/vnd.wap.mms-message`.
 *
 * This receiver intentionally does nothing — MMS handling is not needed for
 * PhoneTransfer's SMS injection workflow. It only needs to exist in the
 * manifest.
 */
class MmsReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "MmsReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        Log.i(TAG, "WAP_PUSH_DELIVER received (stub — no-op)")
    }
}
