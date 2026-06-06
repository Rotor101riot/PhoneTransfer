package com.phonetransfer.companion.sms

import android.app.Activity
import android.app.role.RoleManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.provider.Telephony
import android.util.Log

/**
 * Transparent activity that prompts the user to make this app the default
 * SMS application.
 *
 * On Android 10+ (API 29+) it uses [RoleManager.createRequestRoleIntent].
 * On Android 8-9 (API 26-28) it falls back to the legacy
 * [Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT] broadcast.
 *
 * **Xiaomi/MIUI workaround** (Item #14):
 * MIUI 10+ on Xiaomi/Redmi/POCO devices intercepts the standard
 * RoleManager and ACTION_CHANGE_DEFAULT intents, routing them through
 * MIUI's own permissions manager which can silently fail or show a
 * different dialog.  When MIUI is detected, we first try MIUI's
 * dedicated SMS default-app Settings panel
 * (`com.android.settings/.SmsDefaultDialog`) before falling back to
 * the AOSP path.  This mirrors the Wondershare/Dr.Fone
 * "ChinessXiaomi10ChangeSmsApp" pattern identified in analysis.
 *
 * Launch with an explicit intent:
 * ```
 * am start -n com.phonetransfer.companion.debug/.sms.ChangeDefaultSmsActivity
 * ```
 *
 * The activity finishes as soon as the user responds to the system dialog.
 * The result code is [Activity.RESULT_OK] if the role was granted,
 * [Activity.RESULT_CANCELED] otherwise — callers can read it via
 * `onActivityResult` or the launcher result API.
 */
class ChangeDefaultSmsActivity : Activity() {

    companion object {
        private const val TAG = "ChangeDefaultSms"
        private const val REQUEST_CODE_DEFAULT_SMS = 100
        private const val REQUEST_CODE_MIUI_SMS = 101

        // MIUI Settings components that handle default SMS app changes.
        // Xiaomi uses these instead of the AOSP RoleManager on MIUI 10+.
        private const val MIUI_SMS_DIALOG_PKG = "com.android.settings"
        private const val MIUI_SMS_DIALOG_CLS = "com.android.settings.SmsDefaultDialog"

        // Fallback: MIUI Security Center permissions management
        private const val MIUI_SECURITY_PKG = "com.miui.securitycenter"
        private const val MIUI_PERMISSION_CLS =
            "com.miui.permcenter.permissions.PermissionsEditorActivity"

        /**
         * Convenience helper to build a launch intent for this activity.
         */
        fun intent(context: Context): Intent =
            Intent(context, ChangeDefaultSmsActivity::class.java)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Already the default?
        if (isDefaultSmsApp()) {
            Log.i(TAG, "Already the default SMS app")
            setResult(RESULT_OK)
            finish()
            return
        }

        // Xiaomi/MIUI path: try the MIUI-specific intent first
        if (isMiuiDevice()) {
            Log.i(TAG, "MIUI detected — trying Xiaomi SMS default dialog")
            if (tryMiuiSmsDialog()) return
            Log.w(TAG, "MIUI SMS dialog unavailable, falling back to AOSP path")
        }

        // Standard AOSP path
        requestDefaultSmsAosp()
    }

    // ------------------------------------------------------------------
    // MIUI-specific SMS default app flow (Item #14)
    // ------------------------------------------------------------------

    /**
     * Attempt to launch MIUI's SmsDefaultDialog Settings panel.
     * This is the intent that MIUI uses internally when the user taps
     * "Default messaging app" in MIUI Settings → Apps → Default apps.
     *
     * Returns true if the intent was launched successfully.
     */
    private fun tryMiuiSmsDialog(): Boolean {
        // Method 1: MIUI SmsDefaultDialog (most reliable on MIUI 10-14)
        try {
            val miuiIntent = Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT).apply {
                putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, packageName)
                component = ComponentName(MIUI_SMS_DIALOG_PKG, MIUI_SMS_DIALOG_CLS)
            }
            // Verify the component actually exists before launching
            if (miuiIntent.resolveActivity(packageManager) != null) {
                startActivityForResult(miuiIntent, REQUEST_CODE_MIUI_SMS)
                Log.i(TAG, "Launched MIUI SmsDefaultDialog")
                return true
            }
        } catch (e: Exception) {
            Log.w(TAG, "MIUI SmsDefaultDialog failed: ${e.message}")
        }

        // Method 2: Direct ACTION_CHANGE_DEFAULT without component — on MIUI
        // this routes to the MIUI handler automatically
        try {
            val fallbackIntent = Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT).apply {
                putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, packageName)
                // MIUI extra: some MIUI versions check this flag to show the
                // dialog instead of silently ignoring it
                putExtra("android.provider.extra.IS_DEFAULT_SMS_APP", false)
            }
            startActivityForResult(fallbackIntent, REQUEST_CODE_MIUI_SMS)
            Log.i(TAG, "Launched MIUI fallback ACTION_CHANGE_DEFAULT")
            return true
        } catch (e: Exception) {
            Log.w(TAG, "MIUI fallback failed: ${e.message}")
        }

        return false
    }

    // ------------------------------------------------------------------
    // Standard AOSP flow
    // ------------------------------------------------------------------

    private fun requestDefaultSmsAosp() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // Android 10+ — RoleManager API
            val roleManager = getSystemService(RoleManager::class.java)
            if (roleManager != null && !roleManager.isRoleHeld(RoleManager.ROLE_SMS)) {
                val roleIntent = roleManager.createRequestRoleIntent(RoleManager.ROLE_SMS)
                startActivityForResult(roleIntent, REQUEST_CODE_DEFAULT_SMS)
            } else {
                Log.i(TAG, "SMS role already held or RoleManager unavailable")
                setResult(RESULT_OK)
                finish()
            }
        } else {
            // Android 8-9 — legacy default SMS app API
            val currentDefault = Telephony.Sms.getDefaultSmsPackage(this)
            if (currentDefault != packageName) {
                val legacyIntent = Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT).apply {
                    putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, packageName)
                }
                startActivityForResult(legacyIntent, REQUEST_CODE_DEFAULT_SMS)
            } else {
                Log.i(TAG, "Already the default SMS app")
                setResult(RESULT_OK)
                finish()
            }
        }
    }

    // ------------------------------------------------------------------
    // Result handling
    // ------------------------------------------------------------------

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        when (requestCode) {
            REQUEST_CODE_DEFAULT_SMS, REQUEST_CODE_MIUI_SMS -> {
                val granted = isDefaultSmsApp()
                Log.i(TAG, "Default SMS result: granted=$granted (requestCode=$requestCode, resultCode=$resultCode)")

                // MIUI workaround: if the MIUI dialog completed but we still
                // don't have the role, try the AOSP RoleManager as a last resort.
                if (!granted && requestCode == REQUEST_CODE_MIUI_SMS
                    && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                ) {
                    Log.i(TAG, "MIUI dialog did not grant role — falling back to RoleManager")
                    requestDefaultSmsAosp()
                    return
                }

                setResult(if (granted) RESULT_OK else RESULT_CANCELED)
            }
        }
        finish()
    }

    // ------------------------------------------------------------------
    // Detection helpers
    // ------------------------------------------------------------------

    private fun isDefaultSmsApp(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = getSystemService(RoleManager::class.java)
            roleManager?.isRoleHeld(RoleManager.ROLE_SMS) == true
        } else {
            Telephony.Sms.getDefaultSmsPackage(this) == packageName
        }
    }

    /**
     * Detect MIUI by checking the `ro.miui.ui.version.name` system property
     * and the device manufacturer.
     */
    private fun isMiuiDevice(): Boolean {
        val manufacturer = Build.MANUFACTURER.lowercase()
        val isBrandXiaomi = manufacturer.contains("xiaomi")
                || manufacturer.contains("redmi")
                || manufacturer.contains("poco")

        if (!isBrandXiaomi) return false

        // Confirm MIUI is present via system property
        val miuiVersion = getSystemProperty("ro.miui.ui.version.name")
        if (!miuiVersion.isNullOrEmpty()) {
            Log.d(TAG, "MIUI detected: version=$miuiVersion manufacturer=$manufacturer")
            return true
        }

        // Some HyperOS builds clear the MIUI property but still use MIUI internals
        val hyperOs = getSystemProperty("ro.mi.os.version.name")
        if (!hyperOs.isNullOrEmpty()) {
            Log.d(TAG, "HyperOS detected: version=$hyperOs manufacturer=$manufacturer")
            return true
        }

        return false
    }

    private fun getSystemProperty(key: String): String? {
        return try {
            val clazz = Class.forName("android.os.SystemProperties")
            val method = clazz.getMethod("get", String::class.java, String::class.java)
            val value = method.invoke(null, key, "") as? String
            if (value.isNullOrEmpty()) null else value
        } catch (_: Exception) {
            null
        }
    }
}
