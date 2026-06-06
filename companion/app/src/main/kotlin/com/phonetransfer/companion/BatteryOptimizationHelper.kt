package com.phonetransfer.companion

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import android.util.Log

/**
 * Helper for requesting that the OS excludes this app from battery
 * optimisation (the "Doze" and app-standby restrictions introduced in
 * Android 6.0 / API 23).
 *
 * Without this whitelist, aggressive OEM power managers (Xiaomi MIUI,
 * OnePlus OxygenOS, Huawei EMUI, Samsung One UI aggressive mode) can
 * suspend the [TransferService] foreground service mid-transfer, causing
 * the desktop to report a "heartbeat failed / connection reset" error.
 *
 * Usage — call from [MainActivity] before starting [TransferService]:
 * ```
 * BatteryOptimizationHelper.requestWhitelistIfNeeded(this)
 * ```
 */
object BatteryOptimizationHelper {

    private const val TAG = "BatteryOptHelper"

    /**
     * Returns true when this app is already excluded from battery optimisation,
     * or when the API level is below 23 (restriction doesn't exist).
     */
    fun isWhitelisted(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true
        val pm = context.getSystemService(PowerManager::class.java) ?: return true
        return pm.isIgnoringBatteryOptimizations(context.packageName)
    }

    /**
     * If the app is not yet whitelisted, show the system dialog asking the
     * user to exclude it from battery optimisation.
     *
     * This is a no-op on API < 23 or when already whitelisted.
     * The dialog is non-blocking — the user can dismiss it without granting,
     * and the transfer will still proceed (just with a higher risk of being
     * killed mid-way on aggressive OEMs).
     */
    fun requestWhitelistIfNeeded(context: Context) {
        if (isWhitelisted(context)) {
            Log.d(TAG, "Already whitelisted — no prompt needed")
            return
        }
        Log.i(TAG, "App is battery-optimised — showing whitelist prompt")
        try {
            val intent = Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.fromParts("package", context.packageName, null),
            )
            context.startActivity(intent)
        } catch (e: Exception) {
            // Some OEMs (particularly Huawei) block this intent entirely.
            // Fall back to the general battery settings screen.
            Log.w(TAG, "ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS blocked: ${e.message}")
            try {
                context.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (e2: Exception) {
                Log.w(TAG, "Battery settings screen also unavailable: ${e2.message}")
            }
        }
    }
}
