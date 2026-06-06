package com.phonetransfer.companion.sms

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log

/**
 * Stub service required by Android to register as a valid SMS application.
 *
 * The system refuses to grant the SMS role unless the app declares a service
 * handling [android.telephony.TelephonyManager.ACTION_RESPOND_VIA_MESSAGE]
 * with sms/smsto/mms/mmsto URI schemes.
 *
 * This service intentionally does nothing. It only needs to exist in the
 * manifest so Android considers this a complete SMS application.
 */
class HeadlessSmsSendService : Service() {

    companion object {
        private const val TAG = "HeadlessSmsSend"
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "RESPOND_VIA_MESSAGE received (stub — stopping)")
        stopSelf()
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
