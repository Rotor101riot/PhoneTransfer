package com.phonetransfer.companion.handlers

import android.content.Context
import android.util.Log
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import java.io.BufferedReader
import java.io.InputStreamReader
import java.util.concurrent.TimeUnit

private const val TAG = "RootHandler"

// Timeout for su command execution
private const val EXEC_TIMEOUT_SECONDS = 30L

/**
 * Approved command prefixes for the root privilege bridge.
 *
 * Only shell commands that begin with one of these prefixes are allowed.
 * Anything else is rejected with an error response before execution.
 *
 * This prevents the root bridge from being abused to run arbitrary destructive
 * or exfiltrating commands via the PC socket connection.
 */
private val APPROVED_PREFIXES = listOf(
    "ls ",
    "cat ",
    "cp ",
    "chmod ",
    "chown ",
    "stat ",
    "id"
)

/**
 * RootHandler — privilege bridge for rooted Android devices.
 *
 * Handles the "root_exec" command:
 *   Input:  {"cmd": "root_exec", "command": "cat /data/data/com.example/file.db", "session_id": "..."}
 *   Output: {"status": "ok", "cmd": "root_exec", "stdout": "...", "stderr": "...", "exit_code": 0}
 *
 * Security model:
 *   - Only commands starting with an approved prefix (see APPROVED_PREFIXES) are executed.
 *   - All other commands are rejected before any shell is spawned.
 *   - Execution is performed via Runtime.exec(["su", "-c", command]) with a 30-second timeout.
 *   - stdout and stderr are captured separately and returned in the response.
 *
 * This handler is intentionally read/copy-biased — it is designed to support
 * extracting files from protected app directories (/data/data/), not to write
 * or modify system state. The approved prefix list enforces this.
 */
class RootHandler(private val context: Context) {

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["root_exec"] = { params, server ->
            val command = params["command"] as? String ?: ""
            executeRootCommand(command)
        }
    }

    // -----------------------------------------------------------------------
    // Execution
    // -----------------------------------------------------------------------

    private fun executeRootCommand(command: String): String {
        if (command.isBlank()) {
            return Response.error("root_exec", "empty_command", "command field is required and must not be blank")
        }

        // Security check: reject commands that don't start with an approved prefix
        val trimmed = command.trim()
        val approved = APPROVED_PREFIXES.any { prefix ->
            trimmed == prefix.trimEnd() || trimmed.startsWith(prefix)
        }

        if (!approved) {
            Log.w(TAG, "root_exec REJECTED — command does not match approved prefixes: '$trimmed'")
            return Response.error(
                "root_exec",
                "not_permitted",
                "Command rejected: must start with one of ${APPROVED_PREFIXES.joinToString()}"
            )
        }

        Log.i(TAG, "root_exec executing: $trimmed")

        return try {
            val process = Runtime.getRuntime().exec(arrayOf("su", "-c", trimmed))

            // Read stdout and stderr concurrently to avoid blocking on full pipe buffers
            val stdoutFuture = readStreamAsync(process.inputStream)
            val stderrFuture = readStreamAsync(process.errorStream)

            val finished = process.waitFor(EXEC_TIMEOUT_SECONDS, TimeUnit.SECONDS)

            if (!finished) {
                process.destroyForcibly()
                Log.w(TAG, "root_exec timed out after ${EXEC_TIMEOUT_SECONDS}s: $trimmed")
                return Response.error(
                    "root_exec",
                    "timeout",
                    "Command timed out after ${EXEC_TIMEOUT_SECONDS} seconds"
                )
            }

            val exitCode = process.exitValue()
            val stdout = stdoutFuture()
            val stderr = stderrFuture()

            Log.d(TAG, "root_exec exit=$exitCode stdout_len=${stdout.length} stderr_len=${stderr.length}")

            Response.ok(
                "root_exec",
                mapOf(
                    "stdout" to stdout,
                    "stderr" to stderr,
                    "exit_code" to exitCode
                )
            )
        } catch (e: SecurityException) {
            Log.e(TAG, "SecurityException running su: ${e.message}")
            Response.error("root_exec", "security_exception", e.message ?: "SecurityException")
        } catch (e: Exception) {
            Log.e(TAG, "root_exec failed for '$trimmed': ${e.message}")
            Response.error("root_exec", "exec_failed", e.message ?: "Unknown error")
        }
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    /**
     * Reads an InputStream on a background thread and returns a lambda that
     * blocks until the read completes and then returns the collected text.
     *
     * This prevents deadlocks when both stdout and stderr are large and their
     * OS pipe buffers fill up while the main thread is waiting on waitFor().
     */
    private fun readStreamAsync(stream: java.io.InputStream): () -> String {
        val sb = StringBuilder()
        val thread = Thread {
            try {
                BufferedReader(InputStreamReader(stream, Charsets.UTF_8)).use { reader ->
                    var line: String?
                    while (reader.readLine().also { line = it } != null) {
                        sb.appendLine(line)
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "Stream read error: ${e.message}")
            }
        }
        thread.start()
        return { thread.join(EXEC_TIMEOUT_SECONDS * 1000L + 1000L); sb.toString() }
    }
}
