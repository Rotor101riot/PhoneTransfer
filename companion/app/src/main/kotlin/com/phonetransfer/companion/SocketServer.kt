package com.phonetransfer.companion

import android.content.Context
import android.util.Log
import com.phonetransfer.companion.protocol.CommandParser
import com.phonetransfer.companion.protocol.EventManager
import com.phonetransfer.companion.protocol.EventNamespace
import com.phonetransfer.companion.protocol.Frame
import com.phonetransfer.companion.protocol.MessageType
import com.phonetransfer.companion.protocol.PROTOCOL_VERSION
import com.phonetransfer.companion.protocol.Response
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.asContextElement
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.Semaphore

private const val TAG = "SocketServer"
private const val PORT = 7337

/**
 * Maximum concurrent client connections.  The primary connection handles
 * all command types; additional connections enable parallel file transfers.
 */
private const val MAX_CONCURRENT_CLIENTS = 4

/**
 * Type alias for command handler lambdas.
 *
 * Each handler receives the parsed command map and a reference to the
 * [SocketServer] (so it can call [SocketServer.sendProgress]) and returns
 * a JSON response string.
 */
typealias CommandHandler = suspend (params: Map<String, Any?>, server: SocketServer) -> String

/**
 * Per-connection state.  Every active client gets its own [ClientSession],
 * stored in a coroutine-bound [ThreadLocal] so public I/O methods on
 * [SocketServer] transparently route to the correct socket.
 */
class ClientSession(
    val socket: Socket,
    val input: InputStream,
    val output: OutputStream,
) {
    val writeLock = Any()
    @Volatile var protocolVersion: Int = 1
    @Volatile var eventManager: EventManager? = null
    /** True when the client has negotiated zlib compression for JSON frames. */
    @Volatile var compressJson: Boolean = false
}

/**
 * Multi-client TCP server that speaks the PhoneTransfer frame protocol.
 *
 * Lifecycle:
 *  1. Call [start] with a [CoroutineScope] to begin listening.
 *  2. Up to [MAX_CONCURRENT_CLIENTS] clients are served concurrently,
 *     enabling parallel file transfers from the PC side.
 *  3. Call [stop] to shut down gracefully, or send a "stop" command from
 *     the PC side.
 *
 * Thread safety:  public I/O methods ([sendJsonFrame], [sendBinaryFrame],
 * [receiveBinaryFrame]) resolve the calling coroutine's [ClientSession]
 * via a [ThreadLocal] context element, so handlers don't need to know
 * which connection they're running on.
 */
class SocketServer(private val appContext: Context? = null) {

    // ------------------------------------------------------------------
    // Public state
    // ------------------------------------------------------------------

    /** True while the server socket is open and accepting connections. */
    @Volatile
    var isRunning: Boolean = false
        private set

    /**
     * Protocol version of the *most recently negotiated* client.
     * Kept for backward compatibility with code that reads this property.
     */
    @Volatile
    var protocolVersion: Int = 1
        private set

    /**
     * Event manager of the most recently negotiated v2 client.
     * Kept for backward compatibility.
     */
    @Volatile
    var eventManager: EventManager? = null
        private set

    /** Number of currently connected clients. */
    val activeClientCount: Int
        get() = MAX_CONCURRENT_CLIENTS - clientSemaphore.availablePermits()

    /**
     * Called on the IO dispatcher whenever the connection state changes.
     * [connected] is true when a client connects, false when it disconnects.
     * [clientAddress] is the remote IP string (null when disconnected).
     */
    var onStatusChange: ((connected: Boolean, clientAddress: String?) -> Unit)? = null

    /**
     * Called on the IO dispatcher immediately after a progress frame is
     * successfully written to the client socket.
     */
    var onProgressSent: ((category: String, done: Int, total: Int) -> Unit)? = null

    // ------------------------------------------------------------------
    // Handler registry
    // ------------------------------------------------------------------

    /**
     * Map from command name → handler lambda.
     * Populate this before calling [start] (e.g. in [TransferService.onCreate]).
     */
    val handlers: MutableMap<String, CommandHandler> = mutableMapOf()

    // ------------------------------------------------------------------
    // Internal state
    // ------------------------------------------------------------------

    private var serverSocket: ServerSocket? = null
    private var serverJob: Job? = null

    /**
     * Semaphore that limits the number of concurrently served clients.
     */
    private val clientSemaphore = Semaphore(MAX_CONCURRENT_CLIENTS)

    /**
     * Coroutine-bound ThreadLocal holding the [ClientSession] for the
     * current handler.  Using [asContextElement] ensures the value is
     * restored correctly even if the coroutine migrates across threads
     * at suspension points.
     */
    private val sessionLocal = ThreadLocal<ClientSession?>()

    // ------------------------------------------------------------------
    // Start / Stop
    // ------------------------------------------------------------------

    /**
     * Open the server socket and start the accept loop on [Dispatchers.IO].
     * Safe to call multiple times — does nothing if already running.
     */
    fun start(scope: CoroutineScope) {
        if (isRunning) return

        serverJob = scope.launch(Dispatchers.IO) {
            try {
                val ss = ServerSocket(PORT).also { serverSocket = it }
                isRunning = true
                Log.i(TAG, "Listening on port $PORT (max $MAX_CONCURRENT_CLIENTS concurrent clients)")

                while (!ss.isClosed) {
                    val client = try {
                        ss.accept()
                    } catch (e: IOException) {
                        if (ss.isClosed) break
                        Log.w(TAG, "Accept failed: ${e.message}")
                        continue
                    }

                    // Each client handled in its own coroutine
                    launch(Dispatchers.IO) {
                        if (clientSemaphore.tryAcquire()) {
                            try {
                                handleClient(client)
                            } finally {
                                clientSemaphore.release()
                            }
                        } else {
                            Log.w(TAG, "Max concurrent clients reached ($MAX_CONCURRENT_CLIENTS), rejecting")
                            try { client.close() } catch (_: IOException) {}
                        }
                    }
                }
            } catch (e: IOException) {
                Log.e(TAG, "Server error: ${e.message}")
            } finally {
                isRunning = false
                onStatusChange?.invoke(false, null)
                Log.i(TAG, "Server stopped")
            }
        }
    }

    /**
     * Gracefully shut down: close all client sockets and the server socket.
     */
    fun stop() {
        try { serverSocket?.close() } catch (_: IOException) {}
        isRunning = false
    }

    // ------------------------------------------------------------------
    // Frame send / receive helpers (available to handler lambdas)
    // ------------------------------------------------------------------

    /**
     * Write a JSON control frame to the client that invoked the current handler.
     *
     * Thread-safe: acquires the session's [ClientSession.writeLock].
     * Does nothing if called outside a handler context.
     */
    fun sendJsonFrame(json: String) {
        val session = sessionLocal.get() ?: return
        synchronized(session.writeLock) {
            try {
                Frame.write(session.output, json, compress = session.compressJson)
            } catch (e: IOException) {
                Log.w(TAG, "sendJsonFrame failed: ${e.message}")
            }
        }
    }

    /**
     * Write a binary data frame to the client that invoked the current handler.
     *
     * Thread-safe: acquires the session's [ClientSession.writeLock].
     */
    fun sendBinaryFrame(data: ByteArray) {
        val session = sessionLocal.get() ?: return
        synchronized(session.writeLock) {
            try {
                Frame.writeBinary(session.output, data)
            } catch (e: IOException) {
                Log.w(TAG, "sendBinaryFrame failed: ${e.message}")
            }
        }
    }

    /**
     * Read one binary frame from the client that invoked the current handler.
     *
     * Blocks until the full frame arrives.  Must only be called from within
     * a handler lambda during a multi-frame sequence (e.g. file_push).
     */
    fun receiveBinaryFrame(): ByteArray {
        val session = sessionLocal.get()
            ?: throw IllegalStateException("receiveBinaryFrame: no active client session")
        return Frame.readBinary(session.input)
    }

    /**
     * Send an unsolicited progress frame to the current client.
     */
    fun sendProgress(category: String, done: Int, total: Int) {
        val session = sessionLocal.get() ?: return
        if (session.socket.isClosed) return
        sendJsonFrame(Response.progress(category, done, total))
        onProgressSent?.invoke(category, done, total)
    }

    // ------------------------------------------------------------------
    // Per-connection logic
    // ------------------------------------------------------------------

    private fun extractSeq(params: Map<String, Any?>): Int {
        val raw = params["_seq"] ?: return 0
        return when (raw) {
            is Number -> raw.toInt()
            is String -> raw.toIntOrNull() ?: 0
            else -> 0
        }
    }

    private suspend fun handleClient(client: Socket) {
        try { client.keepAlive = true } catch (_: Exception) {}

        val input  = BufferedInputStream(client.getInputStream(), 65536)
        val output = BufferedOutputStream(client.getOutputStream(), 65536)
        val session = ClientSession(
            socket = client,
            input  = input,
            output = output,
        )

        val address = client.inetAddress.hostAddress ?: "unknown"
        Log.i(TAG, "Client connected: $address (active: ${activeClientCount})")
        onStatusChange?.invoke(true, address)

        try {
            // Bind this session to the coroutine so sendJsonFrame/sendBinaryFrame
            // resolve to the correct client even across thread hops.
            withContext(sessionLocal.asContextElement(session)) {
                while (!client.isClosed) {
                    val json = try {
                        Frame.read(input)
                    } catch (e: IOException) {
                        Log.i(TAG, "Client disconnected: ${e.message}")
                        break
                    }

                    Log.d(TAG, "RX [$address]: $json")

                    val params = try {
                        CommandParser.parse(json)
                    } catch (e: Exception) {
                        Log.w(TAG, "Parse error: ${e.message}")
                        val errJson = Response.error("unknown", "parse_error", e.message ?: "JSON parse failed")
                        synchronized(session.writeLock) { Frame.write(output, errJson) }
                        continue
                    }

                    val cmd = params["cmd"] as? String ?: "unknown"
                    val seq = extractSeq(params)

                    // ── Built-in protocol commands ──────────────────────────

                    // v2 handshake
                    if (cmd == "hello") {
                        val clientVersion = (params["_v"] as? Number)?.toInt() ?: 1
                        session.protocolVersion = minOf(clientVersion, PROTOCOL_VERSION)
                        if (session.protocolVersion >= 2 && appContext != null) {
                            session.eventManager = EventManager(appContext, this@SocketServer)
                        }
                        // Negotiate zlib compression for JSON frames
                        val clientWantsCompress = params["compress"] == true ||
                            params["compress"] == "zlib"
                        session.compressJson = clientWantsCompress
                        // Update legacy instance-level state
                        protocolVersion = session.protocolVersion
                        eventManager = session.eventManager
                        Log.i(TAG, "Protocol negotiated: v${session.protocolVersion} " +
                            "(compress=${session.compressJson}, client v$clientVersion)")
                        val payload = mapOf(
                            "_v" to session.protocolVersion,
                            "server_version" to PROTOCOL_VERSION,
                            "device_time" to System.currentTimeMillis(),
                            "max_concurrent_clients" to MAX_CONCURRENT_CLIENTS,
                            "compress" to session.compressJson,
                        )
                        // Don't compress the hello response itself — client
                        // hasn't confirmed support yet
                        synchronized(session.writeLock) {
                            Frame.write(output, Response.ok("hello", payload, seq))
                        }
                        continue
                    }

                    // v2 heartbeat
                    if (cmd == "heartbeat") {
                        synchronized(session.writeLock) {
                            Frame.write(output, Response.ok("heartbeat", emptyMap(), seq))
                        }
                        continue
                    }

                    // v2 event subscription
                    if (cmd == "subscribe") {
                        val namespaces = extractStringList(params, "ns")
                        val em = session.eventManager
                        if (em != null && namespaces.isNotEmpty()) {
                            em.subscribe(namespaces)
                            synchronized(session.writeLock) {
                                Frame.write(output, Response.ok("subscribe", mapOf(
                                    "subscribed" to em.subscriptions.toList()
                                ), seq))
                            }
                        } else {
                            synchronized(session.writeLock) {
                                Frame.write(output, Response.error("subscribe", "not_available",
                                    if (em == null) "Event system requires protocol v2 (send 'hello' first)"
                                    else "No namespaces specified", seq))
                            }
                        }
                        continue
                    }

                    if (cmd == "unsubscribe") {
                        val namespaces = extractStringList(params, "ns")
                        val em = session.eventManager
                        if (em != null) {
                            em.unsubscribe(namespaces)
                            synchronized(session.writeLock) {
                                Frame.write(output, Response.ok("unsubscribe", mapOf(
                                    "subscribed" to em.subscriptions.toList()
                                ), seq))
                            }
                        } else {
                            synchronized(session.writeLock) {
                                Frame.write(output, Response.error("unsubscribe", "not_available",
                                    "Event system requires protocol v2", seq))
                            }
                        }
                        continue
                    }

                    // Built-in "stop" handling
                    if (cmd == "stop") {
                        Log.i(TAG, "Stop command received — shutting down")
                        synchronized(session.writeLock) {
                            Frame.write(output, Response.ok("stop", emptyMap(), seq))
                        }
                        stop()
                        break
                    }

                    // ── Dispatch to registered handlers ─────────────────────

                    val handler = handlers[cmd]
                    val responseJson = if (handler != null) {
                        try {
                            handler(params, this@SocketServer)
                        } catch (e: Exception) {
                            Log.e(TAG, "Handler '$cmd' threw: ${e.message}", e)
                            Response.error(cmd, "handler_error", e.message ?: "Internal handler error", seq)
                        }
                    } else {
                        Log.w(TAG, "No handler for cmd='$cmd'")
                        Response.error(cmd, "unknown_command", "No handler registered for '$cmd'", seq)
                    }

                    if (responseJson.isNotEmpty()) {
                        Log.d(TAG, "TX [$address]: $responseJson")
                        synchronized(session.writeLock) { Frame.write(output, responseJson) }
                    }
                }
            }
        } catch (e: IOException) {
            Log.e(TAG, "Client I/O error: ${e.message}")
        } finally {
            session.eventManager?.clearAll()
            session.eventManager = null
            try { client.close() } catch (_: IOException) {}
            onStatusChange?.invoke(false, null)
            Log.i(TAG, "Client disconnected: $address (active: ${activeClientCount})")
        }
    }

    /**
     * Extract a list of strings from a params map value that could be
     * a `List<*>` or a single string.
     */
    private fun extractStringList(params: Map<String, Any?>, key: String): List<String> {
        return when (val raw = params[key]) {
            is List<*> -> raw.filterIsInstance<String>()
            is String -> listOf(raw)
            else -> emptyList()
        }
    }
}
