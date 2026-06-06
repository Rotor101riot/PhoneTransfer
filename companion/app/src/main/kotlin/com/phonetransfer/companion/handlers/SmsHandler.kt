package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.Telephony
import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.phonetransfer.companion.protocol.Response
import com.phonetransfer.companion.SocketServer

class SmsHandler(private val context: Context) {

    companion object {
        private const val TAG = "SmsHandler"
    }

    private val gson = Gson()

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_sms"] = { cmd, server ->
            handleExtract(cmd, server)
        }

        // Stream a single MMS part's binary data (image/audio/video attachment)
        registry["mms_part_pull"] = { params, server ->
            handleMmsPartPull(params, server)
        }
    }

    fun registerInject(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["inject_sms"] = { cmd, server ->
            handleInject(cmd, server)
        }

        registry["request_sms_role"] = { _, _ ->
            handleRequestSmsRole()
        }

        registry["check_sms_role"] = { _, _ ->
            handleCheckSmsRole()
        }

        // Automated takeover + restore flow
        registry["acquire_sms_role"] = { _, _ ->
            handleAcquireSmsRole()
        }

        registry["release_sms_role"] = { _, _ ->
            handleReleaseSmsRole()
        }
    }

    private suspend fun handleExtract(cmd: Map<String, Any?>, server: SocketServer): String {
        val messages = mutableListOf<Map<String, Any?>>()

        messages.addAll(extractSms(server))
        messages.addAll(extractMms(server))

        val payload = mapOf(
            "category" to "sms",
            "count" to messages.size,
            "data" to messages
        )
        return Response.ok("extract_sms", payload)
    }

    private fun extractSms(server: SocketServer): List<Map<String, Any?>> {
        val messages = mutableListOf<Map<String, Any?>>()

        val cursor = context.contentResolver.query(
            Telephony.Sms.CONTENT_URI,
            arrayOf(
                Telephony.Sms._ID,
                Telephony.Sms.ADDRESS,
                Telephony.Sms.BODY,
                Telephony.Sms.DATE,
                Telephony.Sms.TYPE,
                Telephony.Sms.READ,
                Telephony.Sms.THREAD_ID,
                Telephony.Sms.STATUS
            ),
            null,
            null,
            "${Telephony.Sms.DATE} ASC"
        ) ?: return messages

        cursor.use {
            val total = it.count
            var done = 0
            while (it.moveToNext()) {
                val id = it.safeString(Telephony.Sms._ID) ?: ""
                val address = it.safeString(Telephony.Sms.ADDRESS) ?: ""
                val body = it.safeString(Telephony.Sms.BODY) ?: ""
                val date = it.safeLong(Telephony.Sms.DATE)
                val type = it.safeInt(Telephony.Sms.TYPE)
                val read = it.safeInt(Telephony.Sms.READ) != 0
                val threadId = it.safeInt(Telephony.Sms.THREAD_ID)
                val status = it.safeInt(Telephony.Sms.STATUS)

                // type 1=inbox, 2=sent, 3=draft, 4=outbox, 5=failed, 6=queued
                val isSent = type != Telephony.Sms.MESSAGE_TYPE_INBOX
                val sender = if (isSent) "self" else address
                val recipient = if (isSent) address else "self"

                messages.add(
                    mapOf(
                        "platform_id" to id,
                        "sender" to sender,
                        "recipient" to recipient,
                        "body" to body,
                        "timestamp" to date,
                        "is_sent" to isSent,
                        "service" to "sms",
                        "read" to read,
                        "sms_type" to type,
                        "thread_id" to threadId,
                        "status" to status
                    )
                )
                done++
                if (done % 100 == 0) server.sendProgress("sms", done, total)
            }
            server.sendProgress("sms", messages.size, messages.size)
        }
        return messages
    }

    private fun extractMms(server: SocketServer): List<Map<String, Any?>> {
        val messages = mutableListOf<Map<String, Any?>>()

        val mmsCursor = context.contentResolver.query(
            Telephony.Mms.CONTENT_URI,
            arrayOf(
                Telephony.Mms._ID,
                Telephony.Mms.DATE,
                Telephony.Mms.MESSAGE_BOX,
                Telephony.Mms.READ,
                Telephony.Mms.SUBJECT,
                Telephony.Mms.CONTENT_TYPE,
                Telephony.Mms.THREAD_ID
            ),
            null,
            null,
            "${Telephony.Mms.DATE} ASC"
        ) ?: return messages

        mmsCursor.use { cursor ->
            val total = cursor.count
            var done = 0
            while (cursor.moveToNext()) {
                val mmsId = cursor.safeString(Telephony.Mms._ID) ?: continue
                val dateSeconds = cursor.safeLong(Telephony.Mms.DATE)
                val messageBox = cursor.safeInt(Telephony.Mms.MESSAGE_BOX)
                val read = cursor.safeInt(Telephony.Mms.READ) != 0
                val subject = cursor.safeString(Telephony.Mms.SUBJECT)
                val threadId = cursor.safeInt(Telephony.Mms.THREAD_ID)

                val isSent = messageBox == Telephony.Mms.MESSAGE_BOX_SENT

                // Get ALL addresses (FROM, TO, CC, BCC) for this MMS
                val addresses = queryMmsAddresses(mmsId)
                val fromAddr = addresses.filter { it["type"] == 137 }.mapNotNull { it["address"] as? String }
                val toAddr = addresses.filter { it["type"] == 151 }.mapNotNull { it["address"] as? String }
                val ccAddr = addresses.filter { it["type"] == 130 }.mapNotNull { it["address"] as? String }

                val primaryFrom = fromAddr.firstOrNull() ?: ""
                val primaryTo = toAddr.firstOrNull() ?: ""
                val sender = if (isSent) "self" else primaryFrom
                val recipient = if (isSent) primaryTo else "self"

                // Get ALL parts (text + attachments)
                val parts = queryMmsParts(mmsId)
                val textBody = parts
                    .filter { it["content_type"] == "text/plain" }
                    .mapNotNull { it["text"] as? String }
                    .joinToString("")

                // Attachment metadata (non-text, non-SMIL parts)
                val attachments = parts.filter { part ->
                    val ct = part["content_type"] as? String ?: ""
                    ct != "text/plain" && ct != "application/smil"
                }.map { part ->
                    mapOf(
                        "part_id" to part["_id"],
                        "content_type" to part["content_type"],
                        "filename" to (part["filename"] ?: part["name"] ?: ""),
                        "charset" to part["charset"],
                        "content_id" to part["content_id"],
                        "content_location" to part["content_location"],
                        "data_size" to part["data_size"]
                    )
                }

                messages.add(
                    mapOf(
                        "platform_id" to "mms_$mmsId",
                        "sender" to sender,
                        "recipient" to recipient,
                        "body" to textBody,
                        "timestamp" to dateSeconds * 1000L,
                        "is_sent" to isSent,
                        "service" to "mms",
                        "read" to read,
                        "subject" to (subject ?: ""),
                        "thread_id" to threadId,
                        "message_box" to messageBox,
                        // Full address lists for group MMS
                        "from_addresses" to fromAddr,
                        "to_addresses" to toAddr,
                        "cc_addresses" to ccAddr,
                        // Attachment metadata
                        "attachments" to attachments,
                        "has_attachments" to attachments.isNotEmpty()
                    )
                )
                done++
                if (done % 50 == 0) server.sendProgress("mms", done, total)
            }
            server.sendProgress("mms", messages.size, messages.size)
        }
        return messages
    }

    /**
     * Query ALL addresses for an MMS message.
     * Returns a list of maps with "address", "type", "charset" keys.
     * Type values: 137=FROM, 151=TO, 130=CC, 129=BCC
     */
    private fun queryMmsAddresses(mmsId: String): List<Map<String, Any?>> {
        val addrUri = Uri.parse("content://mms/$mmsId/addr")
        val cursor = context.contentResolver.query(
            addrUri,
            arrayOf("address", "type", "charset"),
            null,
            null,
            null
        ) ?: return emptyList()

        val addresses = mutableListOf<Map<String, Any?>>()
        cursor.use {
            while (it.moveToNext()) {
                val address = it.safeString("address") ?: continue
                // Skip the "insert-address-token" placeholder Android uses
                if (address == "insert-address-token") continue
                addresses.add(mapOf(
                    "address" to address,
                    "type" to it.safeInt("type"),
                    "charset" to it.safeInt("charset")
                ))
            }
        }
        return addresses
    }

    /**
     * Query ALL parts for an MMS message — text bodies AND attachment metadata.
     * For binary attachments, includes size info but not the binary data itself
     * (the PC side can request attachment data via file_pull if needed).
     */
    private fun queryMmsParts(mmsId: String): List<Map<String, Any?>> {
        val partUri = Uri.parse("content://mms/part")
        val cursor = context.contentResolver.query(
            partUri,
            arrayOf("_id", "ct", "text", "name", "_data", "cl", "cid", "chset", "fn"),
            "mid = ?",
            arrayOf(mmsId),
            null
        ) ?: return emptyList()

        val parts = mutableListOf<Map<String, Any?>>()
        cursor.use {
            while (it.moveToNext()) {
                val partId = it.safeString("_id") ?: continue
                val contentType = it.safeString("ct") ?: continue
                val text = it.safeString("text")
                val name = it.safeString("name")
                val data = it.safeString("_data")
                val contentLocation = it.safeString("cl")
                val contentId = it.safeString("cid")
                val charset = it.safeInt("chset")
                val filename = it.safeString("fn")

                // For binary parts, calculate data size if _data path exists.
                // Note: InputStream.available() is unreliable on ContentResolver
                // streams — it returns the buffer size, not total size.  We read
                // the full stream to get the true byte count.
                var dataSize: Long = 0
                if (data != null && contentType != "text/plain") {
                    try {
                        val partDataUri = Uri.parse("content://mms/part/$partId")
                        context.contentResolver.openInputStream(partDataUri)?.use { stream ->
                            val buf = ByteArray(8192)
                            var total = 0L
                            var n: Int
                            while (stream.read(buf).also { n = it } != -1) {
                                total += n
                            }
                            dataSize = total
                        }
                    } catch (e: Exception) {
                        Log.w(TAG, "Could not read MMS part $partId size: ${e.message}")
                    }
                }

                parts.add(mapOf(
                    "_id" to partId,
                    "content_type" to contentType,
                    "text" to text,
                    "name" to name,
                    "data_path" to data,
                    "content_location" to contentLocation,
                    "content_id" to contentId,
                    "charset" to charset,
                    "filename" to filename,
                    "data_size" to dataSize
                ))
            }
        }
        return parts
    }

    /**
     * Check whether this app currently holds the default SMS role.
     */
    private fun handleCheckSmsRole(): String {
        val isDefault = isDefaultSmsApp()
        val payload = mapOf(
            "is_default_sms" to isDefault,
            "current_default" to (Telephony.Sms.getDefaultSmsPackage(context) ?: "unknown")
        )
        return Response.ok("check_sms_role", payload)
    }

    /**
     * Launch the ChangeDefaultSmsActivity to prompt the user for the SMS role.
     * This is fire-and-forget — the PC should poll [handleCheckSmsRole] afterwards.
     */
    private fun handleRequestSmsRole(): String {
        return try {
            val intent = android.content.Intent(
                context,
                com.phonetransfer.companion.sms.ChangeDefaultSmsActivity::class.java
            ).apply {
                addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            Response.ok("request_sms_role", mapOf("launched" to true))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to launch ChangeDefaultSmsActivity", e)
            Response.error("request_sms_role", "LAUNCH_FAILED", e.message ?: "Unknown error")
        }
    }

    private fun isDefaultSmsApp(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = context.getSystemService(android.app.role.RoleManager::class.java)
            roleManager?.isRoleHeld(android.app.role.RoleManager.ROLE_SMS) == true
        } else {
            Telephony.Sms.getDefaultSmsPackage(context) == context.packageName
        }
    }

    private suspend fun handleInject(cmd: Map<String, Any?>, server: SocketServer): String {
        @Suppress("UNCHECKED_CAST")
        val dataRaw = (cmd["data"] as? List<*>) ?: (cmd["items"] as? List<*>)
            ?: return Response.error("inject_sms", "MISSING_DATA", "No data/items array provided")

        val dataType = object : TypeToken<List<Map<String, Any?>>>() {}.type
        val dataJson = gson.toJson(dataRaw)
        val messages: List<Map<String, Any?>> = gson.fromJson(dataJson, dataType)

        val total = messages.size
        var injected = 0
        var failed = 0
        var securityBlocked = false

        messages.forEachIndexed { index, message ->
            try {
                val isSent = message["is_sent"] as? Boolean ?: false
                val sender = message["sender"] as? String ?: ""
                val recipient = message["recipient"] as? String ?: ""
                val body = message["body"] as? String ?: ""
                val timestamp = (message["timestamp"] as? Number)?.toLong() ?: System.currentTimeMillis()
                val read = message["read"] as? Boolean ?: true
                val threadId = (message["thread_id"] as? Number)?.toInt() ?: 0
                val status = (message["status"] as? Number)?.toInt() ?: -1

                // sms_type: 1=inbox, 2=sent, 3=draft, 4=outbox, 5=failed, 6=queued
                // If not provided, auto-detect from is_sent
                val smsType = (message["sms_type"] as? Number)?.toInt()
                    ?: if (isSent) Telephony.Sms.MESSAGE_TYPE_SENT
                       else Telephony.Sms.MESSAGE_TYPE_INBOX

                val address = if (isSent) recipient else sender

                // Route to correct sub-URI based on SMS type (matching imobie pattern)
                val targetUri = when (smsType) {
                    Telephony.Sms.MESSAGE_TYPE_INBOX  -> Telephony.Sms.Inbox.CONTENT_URI
                    Telephony.Sms.MESSAGE_TYPE_SENT   -> Telephony.Sms.Sent.CONTENT_URI
                    Telephony.Sms.MESSAGE_TYPE_DRAFT  -> Telephony.Sms.Draft.CONTENT_URI
                    Telephony.Sms.MESSAGE_TYPE_OUTBOX -> Uri.parse("content://sms/outbox")
                    Telephony.Sms.MESSAGE_TYPE_FAILED -> Uri.parse("content://sms/failed")
                    Telephony.Sms.MESSAGE_TYPE_QUEUED -> Uri.parse("content://sms/queued")
                    else -> if (isSent) Telephony.Sms.Sent.CONTENT_URI
                            else Telephony.Sms.Inbox.CONTENT_URI
                }

                val values = ContentValues().apply {
                    put(Telephony.Sms.ADDRESS, address)
                    put(Telephony.Sms.BODY, body)
                    put(Telephony.Sms.DATE, timestamp)
                    put(Telephony.Sms.READ, if (read) 1 else 0)
                    put(Telephony.Sms.TYPE, smsType)
                    put(Telephony.Sms.STATUS, status)
                    if (threadId > 0) {
                        put(Telephony.Sms.THREAD_ID, threadId)
                    }
                }

                val result = context.contentResolver.insert(targetUri, values)
                if (result != null) injected++ else failed++
            } catch (e: SecurityException) {
                securityBlocked = true
                failed++
            } catch (e: Exception) {
                failed++
            }

            val processed = index + 1
            if (processed % 50 == 0) {
                server.sendProgress("sms", processed, total)
            }
        }

        server.sendProgress("sms", total, total)

        if (securityBlocked && injected == 0) {
            return Response.error(
                "inject_sms",
                "PERMISSION_DENIED",
                "SMS injection requires this app to be set as the default SMS app, or the device to grant WRITE_SMS permission. " +
                "This app will not attempt to become the default SMS app. " +
                "Please use a dedicated SMS restore app, or grant the necessary permissions manually."
            )
        }

        val payload = mapOf(
            "category" to "sms",
            "injected" to injected,
            "failed" to failed,
            "security_blocked" to securityBlocked
        )
        return Response.ok("inject_sms", payload)
    }

    // ------------------------------------------------------------------
    // MMS part binary pull (streams attachment data over socket)
    // ------------------------------------------------------------------

    /**
     * Stream MMS part binary data to the PC.
     * PC sends: {"cmd": "mms_part_pull", "part_id": "123"}
     * APK sends: JSON header → N binary chunks → JSON done frame
     * Returns "" to suppress outer response (handler manages own framing).
     */
    private fun handleMmsPartPull(params: Map<String, Any?>, server: SocketServer): String {
        val partId = params["part_id"] as? String
            ?: return Response.error("mms_part_pull", "MISSING_PARAM", "part_id required")

        val partUri = Uri.parse("content://mms/part/$partId")

        try {
            val inputStream = context.contentResolver.openInputStream(partUri)
                ?: return Response.error("mms_part_pull", "NOT_FOUND", "Part $partId not found")

            // Stream in chunks — never load the full attachment into RAM.
            // Header is sent first with size=-1 (unknown) since ContentResolver
            // streams don't reliably report length; the done frame carries the
            // final byte count and MD5.
            server.sendJsonFrame(Response.ok("mms_part_pull", mapOf(
                "part_id" to partId,
                "size" to -1   // actual size reported in done frame
            )))

            val md5 = java.security.MessageDigest.getInstance("MD5")
            val buf = ByteArray(512 * 1024)
            var totalSent = 0L

            inputStream.use { stream ->
                var bytesRead: Int
                while (stream.read(buf).also { bytesRead = it } != -1) {
                    val chunk = if (bytesRead == buf.size) buf else buf.copyOf(bytesRead)
                    md5.update(chunk)
                    server.sendBinaryFrame(chunk)
                    totalSent += bytesRead
                }
            }

            val md5Hex = md5.digest().joinToString("") { "%02x".format(it) }
            server.sendJsonFrame(Response.ok("mms_part_pull", mapOf(
                "part_id" to partId,
                "status" to "done",
                "size" to totalSent,
                "md5" to md5Hex
            )))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to pull MMS part $partId", e)
            return Response.error("mms_part_pull", "READ_ERROR", e.message ?: "Unknown error")
        }

        return ""  // handler managed framing
    }

    // ------------------------------------------------------------------
    // Automated SMS role takeover / restore (Item #6)
    // ------------------------------------------------------------------

    /** Saved original default SMS package, restored by release_sms_role. */
    private var savedDefaultSmsPackage: String? = null

    /**
     * Acquire the default SMS role automatically.
     * Saves the current default SMS app and attempts to become default.
     * On Android 10+ uses RoleManager; on older versions uses Telephony API.
     */
    private fun handleAcquireSmsRole(): String {
        // Already default?
        if (isDefaultSmsApp()) {
            return Response.ok("acquire_sms_role", mapOf(
                "acquired" to true,
                "was_default" to true
            ))
        }

        // Save the current default so we can restore later
        savedDefaultSmsPackage = Telephony.Sms.getDefaultSmsPackage(context)
        Log.i(TAG, "Saved current default SMS app: $savedDefaultSmsPackage")

        // Launch the role request activity
        return try {
            val intent = android.content.Intent(
                context,
                com.phonetransfer.companion.sms.ChangeDefaultSmsActivity::class.java
            ).apply {
                addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            Response.ok("acquire_sms_role", mapOf(
                "acquired" to false,
                "launched" to true,
                "previous_default" to (savedDefaultSmsPackage ?: "unknown"),
                "message" to "User must approve the SMS role change on device"
            ))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to launch SMS role acquisition", e)
            Response.error("acquire_sms_role", "LAUNCH_FAILED", e.message ?: "Unknown error")
        }
    }

    /**
     * Release the default SMS role back to the original app.
     * Call this after SMS injection is complete.
     */
    private fun handleReleaseSmsRole(): String {
        val previousDefault = savedDefaultSmsPackage
        if (previousDefault == null) {
            return Response.ok("release_sms_role", mapOf(
                "released" to false,
                "message" to "No saved default SMS app to restore (was this app already default?)"
            ))
        }

        // Check if we're still the default
        if (!isDefaultSmsApp()) {
            savedDefaultSmsPackage = null
            return Response.ok("release_sms_role", mapOf(
                "released" to true,
                "message" to "Already not the default SMS app"
            ))
        }

        // On Android 10+, the user must change it back via system UI
        // We launch an intent to switch back
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT) {
                val intent = android.content.Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT)
                    .putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, previousDefault)
                    .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                context.startActivity(intent)
            }
            savedDefaultSmsPackage = null
            Response.ok("release_sms_role", mapOf(
                "released" to true,
                "restored_to" to previousDefault,
                "message" to "Launched restore dialog for $previousDefault"
            ))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to restore SMS role to $previousDefault", e)
            Response.error("release_sms_role", "RESTORE_FAILED",
                "Could not restore default SMS app to $previousDefault: ${e.message}")
        }
    }

    private fun android.database.Cursor.safeString(col: String): String? {
        val idx = getColumnIndex(col); return if (idx >= 0) getString(idx) else null
    }
    private fun android.database.Cursor.safeLong(col: String): Long {
        val idx = getColumnIndex(col); return if (idx >= 0) getLong(idx) else 0L
    }
    private fun android.database.Cursor.safeInt(col: String): Int {
        val idx = getColumnIndex(col); return if (idx >= 0) getInt(idx) else 0
    }
}
