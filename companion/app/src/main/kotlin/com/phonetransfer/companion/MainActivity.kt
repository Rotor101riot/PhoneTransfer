package com.phonetransfer.companion

import android.Manifest
import android.animation.ObjectAnimator
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView

class MainActivity : AppCompatActivity() {

    // ------------------------------------------------------------------
    // UI state enum
    // ------------------------------------------------------------------

    private enum class UiState {
        MISSING_PERMISSIONS,  // perms not granted
        STOPPED,              // service not running
        WAITING,              // running, no PC connected yet  (amber pulse)
        CONNECTED,            // PC connected, idle           (green solid)
        TRANSFERRING          // transfer in progress          (blue pulse)
    }

    // ------------------------------------------------------------------
    // View references
    // ------------------------------------------------------------------

    private lateinit var statusDot:       View
    private lateinit var statusText:      TextView
    private lateinit var progressCard:    MaterialCardView
    private lateinit var categoryText:    TextView
    private lateinit var progressBar:     ProgressBar
    private lateinit var progressText:    TextView
    private lateinit var activateButton:  MaterialButton
    private lateinit var stopButton:      MaterialButton
    private lateinit var permissionsCard: MaterialCardView
    private lateinit var permissionsButton: MaterialButton

    // ------------------------------------------------------------------
    // Runtime state
    // ------------------------------------------------------------------

    private var currentUiState: UiState = UiState.STOPPED
    private var lastConnected:  Boolean = false
    private var lastAddress:    String? = null
    private var pulseAnimator:  ObjectAnimator? = null

    // Dot colors resolved from resources in onCreate
    private var colorIdle      = 0
    private var colorWaiting   = 0
    private var colorConnected = 0
    private var colorTransfer  = 0

    // ------------------------------------------------------------------
    // Required permissions
    // ------------------------------------------------------------------

    /**
     * Standard runtime permissions requested via the system dialog.
     * MANAGE_EXTERNAL_STORAGE and REQUEST_INSTALL_PACKAGES are handled
     * separately below — they require dedicated Settings intents.
     */
    private val requiredPermissions: Array<String> = buildList {
        add(Manifest.permission.READ_CONTACTS)
        add(Manifest.permission.WRITE_CONTACTS)
        add(Manifest.permission.READ_CALL_LOG)
        add(Manifest.permission.WRITE_CALL_LOG)
        add(Manifest.permission.READ_SMS)
        add("android.permission.WRITE_SMS")
        add(Manifest.permission.READ_CALENDAR)
        add(Manifest.permission.WRITE_CALENDAR)
        add(Manifest.permission.GET_ACCOUNTS)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            add(Manifest.permission.READ_MEDIA_IMAGES)
            add(Manifest.permission.READ_MEDIA_VIDEO)
            add(Manifest.permission.READ_MEDIA_AUDIO)
            add(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            @Suppress("DEPRECATION")
            add(Manifest.permission.READ_EXTERNAL_STORAGE)
        }
    }.toTypedArray()

    // Permission result is handled in onResume (dialog dismissal triggers onResume)
    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { /* handled in onResume */ }

    // ------------------------------------------------------------------
    // LocalBroadcast receiver
    // ------------------------------------------------------------------

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val connected = intent.getBooleanExtra(EXTRA_CONNECTED, false)
            val address   = intent.getStringExtra(EXTRA_ADDRESS)
            val category  = intent.getStringExtra(EXTRA_CATEGORY)
            val done      = intent.getIntExtra(EXTRA_PROGRESS, 0)
            val total     = intent.getIntExtra(EXTRA_TOTAL,    0)

            lastConnected = connected
            lastAddress   = address

            when {
                category != null && total > 0 -> {
                    progressCard.visibility = View.VISIBLE
                    categoryText.text       = getString(R.string.status_transferring, category)
                    progressBar.max         = total
                    progressBar.progress    = done
                    progressText.text       = "$done / $total"
                    applyUiState(UiState.TRANSFERRING)
                }
                connected -> {
                    progressCard.visibility = View.GONE
                    applyUiState(UiState.CONNECTED)
                }
                TransferService.isRunning -> {
                    progressCard.visibility = View.GONE
                    applyUiState(UiState.WAITING)
                }
                else -> {
                    progressCard.visibility = View.GONE
                    applyUiState(UiState.STOPPED)
                }
            }
        }
    }

    private lateinit var broadcaster: LocalBroadcastManager

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        colorIdle      = ContextCompat.getColor(this, R.color.status_idle)
        colorWaiting   = ContextCompat.getColor(this, R.color.status_waiting)
        colorConnected = ContextCompat.getColor(this, R.color.status_connected)
        colorTransfer  = ContextCompat.getColor(this, R.color.status_transfer)

        statusDot         = findViewById(R.id.statusDot)
        statusText        = findViewById(R.id.statusText)
        progressCard      = findViewById(R.id.progressCard)
        categoryText      = findViewById(R.id.categoryText)
        progressBar       = findViewById(R.id.progressBar)
        progressText      = findViewById(R.id.progressText)
        activateButton    = findViewById(R.id.activateButton)
        stopButton        = findViewById(R.id.stopButton)
        permissionsCard   = findViewById(R.id.permissionsCard)
        permissionsButton = findViewById(R.id.permissionsButton)

        broadcaster = LocalBroadcastManager.getInstance(this)

        activateButton.setOnClickListener    { startTransferService() }
        stopButton.setOnClickListener        { stopTransferService()  }
        permissionsButton.setOnClickListener { requestNextMissingPermission() }

        progressCard.visibility = View.GONE
        applyUiState(UiState.STOPPED)
    }

    override fun onResume() {
        super.onResume()
        broadcaster.registerReceiver(statusReceiver, IntentFilter(ACTION_STATUS))
        refreshUi()
    }

    override fun onPause() {
        super.onPause()
        broadcaster.unregisterReceiver(statusReceiver)
        stopPulse()
    }

    // ------------------------------------------------------------------
    // State resolution
    // ------------------------------------------------------------------

    /**
     * Evaluate the correct UI state from scratch.  Called on every onResume
     * so permission changes and service starts/stops are always reflected.
     */
    private fun refreshUi() {
        val missing = missingPermissions()
        when {
            missing.isNotEmpty() -> applyUiState(UiState.MISSING_PERMISSIONS)
            !TransferService.isRunning -> {
                // Permissions granted but service is not running: auto-start silently.
                startTransferService()
                applyUiState(UiState.WAITING)
            }
            // Read live state from the service — avoids relying on stale lastConnected
            // which is only updated when the broadcast receiver is registered (foreground).
            TransferService.isConnected -> {
                lastConnected = true
                lastAddress   = TransferService.connectedAddress
                applyUiState(UiState.CONNECTED)
            }
            else -> applyUiState(UiState.WAITING)
        }
    }

    private fun applyUiState(state: UiState) {
        currentUiState = state
        stopPulse()

        when (state) {

            UiState.MISSING_PERMISSIONS -> {
                setDotColor(colorIdle)
                statusText.text            = getString(R.string.status_needs_permissions)
                activateButton.visibility  = View.GONE
                stopButton.visibility      = View.GONE
                permissionsCard.visibility = View.VISIBLE
            }

            UiState.STOPPED -> {
                setDotColor(colorIdle)
                statusText.text            = getString(R.string.status_inactive)
                activateButton.visibility  = View.VISIBLE
                stopButton.visibility      = View.GONE
                permissionsCard.visibility = View.GONE
            }

            UiState.WAITING -> {
                setDotColor(colorWaiting)
                startPulse()
                statusText.text            = getString(R.string.status_waiting_pc)
                activateButton.visibility  = View.GONE
                stopButton.visibility      = View.VISIBLE
                permissionsCard.visibility = View.GONE
            }

            UiState.CONNECTED -> {
                setDotColor(colorConnected)
                statusText.text            = getString(R.string.status_connected, lastAddress ?: "")
                activateButton.visibility  = View.GONE
                stopButton.visibility      = View.VISIBLE
                permissionsCard.visibility = View.GONE
            }

            UiState.TRANSFERRING -> {
                setDotColor(colorTransfer)
                startPulse()
                // categoryText is set by the broadcast receiver
                activateButton.visibility  = View.GONE
                stopButton.visibility      = View.VISIBLE
                permissionsCard.visibility = View.GONE
            }
        }
    }

    // ------------------------------------------------------------------
    // Dot helpers
    // ------------------------------------------------------------------

    private fun setDotColor(color: Int) {
        statusDot.backgroundTintList = ColorStateList.valueOf(color)
    }

    private fun startPulse() {
        pulseAnimator?.cancel()
        pulseAnimator = ObjectAnimator.ofFloat(statusDot, "alpha", 1f, 0.2f).apply {
            duration    = 900
            repeatCount = ObjectAnimator.INFINITE
            repeatMode  = ObjectAnimator.REVERSE
            start()
        }
    }

    private fun stopPulse() {
        pulseAnimator?.cancel()
        pulseAnimator = null
        statusDot.alpha = 1f
    }

    // ------------------------------------------------------------------
    // Permission helpers
    // ------------------------------------------------------------------

    private fun missingPermissions(): List<String> {
        val missing = requiredPermissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }.toMutableList()

        // MANAGE_EXTERNAL_STORAGE — special check, not a standard runtime permission
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                missing.add(Manifest.permission.MANAGE_EXTERNAL_STORAGE)
            }
        }

        // REQUEST_INSTALL_PACKAGES — special check
        if (!packageManager.canRequestPackageInstalls()) {
            missing.add(Manifest.permission.REQUEST_INSTALL_PACKAGES)
        }

        return missing
    }

    /**
     * Routes the user to the correct grant screen for the first missing permission.
     * Standard permissions go through the system dialog; special permissions
     * (MANAGE_EXTERNAL_STORAGE, REQUEST_INSTALL_PACKAGES) require Settings intents.
     */
    private fun requestNextMissingPermission() {
        val missing = missingPermissions()
        when {
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.R &&
            missing.contains(Manifest.permission.MANAGE_EXTERNAL_STORAGE) -> {
                // Must send user to Settings — cannot request programmatically
                val intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).apply {
                    data = Uri.fromParts("package", packageName, null)
                }
                startActivity(intent)
            }
            missing.contains(Manifest.permission.REQUEST_INSTALL_PACKAGES) -> {
                val intent = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES).apply {
                    data = Uri.fromParts("package", packageName, null)
                }
                startActivity(intent)
            }
            missing.isNotEmpty() -> {
                // All remaining are standard runtime permissions
                permissionLauncher.launch(missing.toTypedArray())
            }
        }
    }

    // ------------------------------------------------------------------
    // Service helpers
    // ------------------------------------------------------------------

    private fun startTransferService() {
        // Ask the user to whitelist us from battery optimisation before the
        // service starts — this prevents OEM power managers from killing the
        // service mid-transfer on Xiaomi, Huawei, OnePlus, and Samsung
        // devices running aggressive battery-saver profiles.
        BatteryOptimizationHelper.requestWhitelistIfNeeded(this)

        val intent = Intent(this, TransferService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    private fun stopTransferService() {
        val intent = Intent(this, TransferService::class.java).apply {
            action = TransferService.ACTION_STOP_SERVICE
        }
        startService(intent)
        lastConnected           = false
        lastAddress             = null
        progressCard.visibility = View.GONE
        applyUiState(UiState.STOPPED)
    }
}
