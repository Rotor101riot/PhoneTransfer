package com.phonetransfer.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.localbroadcastmanager.content.LocalBroadcastManager

/**
 * Receives ADB-injected broadcasts from the PhoneTransfer PC application so the
 * companion UI updates even during ADB-based data transfers (where the TCP
 * socket is never connected).
 *
 * The PC side fires this via:
 *
 *     adb -s <serial> shell am broadcast \
 *         -p com.phonetransfer.companion \
 *         -a com.phonetransfer.STATUS \
 *         --es category "Contacts" --ei done 50 --ei total 500
 *
 * Passing ``--ei total 0`` (or omitting ``category``) signals that no transfer
 * is active, reverting the icon to WAITING.
 *
 * This class only relays the intent as a [LocalBroadcast]; [MainActivity]'s
 * existing [statusReceiver] handles all UI state transitions, keeping the two
 * code paths unified.
 */
class AdbStatusReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val category = intent.getStringExtra("category")
        val done     = intent.getIntExtra("done", 0)
        val total    = intent.getIntExtra("total", 0)

        val relay = Intent(ACTION_STATUS).apply {
            if (category != null && total > 0) {
                // Active transfer — drive the TRANSFERRING ui state.
                putExtra(EXTRA_CONNECTED, true)
                putExtra(EXTRA_CATEGORY,  category)
                putExtra(EXTRA_PROGRESS,  done)
                putExtra(EXTRA_TOTAL,     total)
            } else {
                // No active transfer — let statusReceiver fall through to WAITING
                // (TransferService.isRunning == true) or STOPPED.
                putExtra(EXTRA_CONNECTED, false)
            }
        }

        LocalBroadcastManager.getInstance(context).sendBroadcast(relay)
    }
}
