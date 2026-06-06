package com.phonetransfer.companion

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "DiscoveryManager"

/**
 * Advertises the PhoneTransfer companion service over mDNS (Bonjour/DNS-SD)
 * using Android's [NsdManager] API.
 *
 * The service type `_phonetransfer._tcp` is registered on the local network
 * so that the PC-side Python code can auto-discover the phone's IP address
 * and port via an mDNS query rather than requiring the user to type an IP.
 *
 * ## Registration lifecycle
 *
 *  1. Call [register] when the [TransferService] starts (or when Wi-Fi is
 *     confirmed available).
 *  2. Call [unregister] in [TransferService.onDestroy] or when Wi-Fi is lost.
 *
 * ## Discovery on the PC side (Python)
 *
 * ```python
 * import socket, struct
 * from zeroconf import ServiceBrowser, Zeroconf
 *
 * class Listener:
 *     def add_service(self, zc, type_, name):
 *         info = zc.get_service_info(type_, name)
 *         print("Found:", socket.inet_ntoa(info.addresses[0]), info.port)
 *
 * zc = Zeroconf()
 * ServiceBrowser(zc, "_phonetransfer._tcp.local.", Listener())
 * ```
 *
 * The [serviceName] defaults to the Android device name
 * (`android.provider.Settings.Global.DEVICE_NAME`) and falls back to the
 * model name so the PC UI can show a human-readable label.
 */
class DiscoveryManager(private val context: Context) {

    companion object {
        /** mDNS service type — must end with "._tcp" or "._udp". */
        const val SERVICE_TYPE = "_phonetransfer._tcp"

        /** TCP port the [SocketServer] listens on. */
        const val SERVICE_PORT = 7337
    }

    private val nsdManager: NsdManager by lazy {
        context.getSystemService(Context.NSD_SERVICE) as NsdManager
    }

    private val registered = AtomicBoolean(false)

    @Volatile
    private var registeredServiceName: String? = null

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    /**
     * Start advertising the PhoneTransfer service on the local network.
     *
     * Safe to call multiple times — does nothing if already registered.
     * The actual service name used (NsdManager may append a suffix to avoid
     * collisions) is available in [registeredServiceName] after the
     * [NsdManager.RegistrationListener.onServiceRegistered] callback fires.
     */
    fun register() {
        if (registered.get()) {
            Log.d(TAG, "Already registered — skipping")
            return
        }

        val serviceInfo = NsdServiceInfo().apply {
            serviceName = resolveServiceName()
            serviceType = SERVICE_TYPE
            port = SERVICE_PORT
        }

        Log.i(TAG, "Registering mDNS service: ${serviceInfo.serviceName} on port $SERVICE_PORT")
        nsdManager.registerService(serviceInfo, NsdManager.PROTOCOL_DNS_SD, registrationListener)
    }

    /**
     * Stop advertising the service.  Safe to call even if [register] was
     * never called (or if registration failed).
     */
    fun unregister() {
        if (!registered.compareAndSet(true, false)) return
        try {
            nsdManager.unregisterService(registrationListener)
            Log.i(TAG, "Unregistered mDNS service")
        } catch (e: Exception) {
            Log.w(TAG, "unregisterService threw: ${e.message}")
        } finally {
            registeredServiceName = null
        }
    }

    // -----------------------------------------------------------------------
    // NsdManager callbacks
    // -----------------------------------------------------------------------

    private val registrationListener = object : NsdManager.RegistrationListener {

        override fun onServiceRegistered(info: NsdServiceInfo) {
            // NsdManager may have modified the service name to avoid collisions.
            registeredServiceName = info.serviceName
            registered.set(true)
            Log.i(TAG, "mDNS service registered as '${info.serviceName}' ($SERVICE_TYPE)")
        }

        override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) {
            registered.set(false)
            Log.e(TAG, "mDNS registration failed: errorCode=$errorCode  service=${info.serviceName}")
        }

        override fun onServiceUnregistered(info: NsdServiceInfo) {
            registered.set(false)
            registeredServiceName = null
            Log.i(TAG, "mDNS service unregistered: ${info.serviceName}")
        }

        override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) {
            Log.e(TAG, "mDNS unregistration failed: errorCode=$errorCode")
        }
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    /**
     * Determine a human-readable service name for this device.
     *
     * Tries in order:
     *  1. `Settings.Global.DEVICE_NAME` (user-visible name, set in About Phone)
     *  2. `android.os.Build.MODEL`
     *  3. Fallback string "PhoneTransfer"
     */
    private fun resolveServiceName(): String {
        val deviceName = try {
            android.provider.Settings.Global.getString(
                context.contentResolver,
                "device_name"
            )
        } catch (e: Exception) {
            null
        }
        return when {
            !deviceName.isNullOrBlank() -> deviceName
            android.os.Build.MODEL.isNotBlank() -> android.os.Build.MODEL
            else -> "PhoneTransfer"
        }
    }
}
