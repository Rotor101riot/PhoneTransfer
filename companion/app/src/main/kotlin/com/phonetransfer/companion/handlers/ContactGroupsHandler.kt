package com.phonetransfer.companion.handlers

import android.content.ContentValues
import android.content.Context
import android.provider.ContactsContract
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.phonetransfer.companion.protocol.Response
import com.phonetransfer.companion.SocketServer

class ContactGroupsHandler(private val context: Context) {

    private val gson = Gson()

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_contact_groups"] = { cmd, server ->
            handleExtract(cmd, server)
        }
    }

    fun registerInject(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["inject_contact_groups"] = { cmd, server ->
            handleInject(cmd, server)
        }
    }

    private suspend fun handleExtract(cmd: Map<String, Any?>, server: SocketServer): String {
        val groups = mutableListOf<Map<String, Any?>>()

        val cursor = context.contentResolver.query(
            ContactsContract.Groups.CONTENT_URI,
            arrayOf(
                ContactsContract.Groups._ID,
                ContactsContract.Groups.TITLE,
                ContactsContract.Groups.ACCOUNT_NAME,
                ContactsContract.Groups.ACCOUNT_TYPE,
                ContactsContract.Groups.GROUP_VISIBLE,
                ContactsContract.Groups.NOTES
            ),
            "${ContactsContract.Groups.DELETED} = 0",
            null,
            null
        ) ?: return Response.error("extract_contact_groups", "QUERY_FAILED", "Failed to query contact groups")

        var processed = 0
        val total = cursor.count

        cursor.use {
            while (it.moveToNext()) {
                val groupId = it.safeLong(ContactsContract.Groups._ID)
                val title = it.safeString(ContactsContract.Groups.TITLE)
                val accountName = it.safeString(ContactsContract.Groups.ACCOUNT_NAME)
                val accountType = it.safeString(ContactsContract.Groups.ACCOUNT_TYPE)
                val visible = it.safeInt(ContactsContract.Groups.GROUP_VISIBLE)
                val notes = it.safeString(ContactsContract.Groups.NOTES)

                val memberCount = queryMemberCount(groupId)

                groups.add(
                    mapOf(
                        "group_id" to groupId,
                        "title" to (title ?: ""),
                        "account_name" to accountName,
                        "account_type" to accountType,
                        "visible" to (visible != 0),
                        "notes" to notes,
                        "member_count" to memberCount
                    )
                )

                processed++
                if (processed % 20 == 0) {
                    server.sendProgress("contact_groups", processed, total)
                }
            }
        }

        server.sendProgress("contact_groups", total, total)

        val payload = mapOf(
            "category" to "contact_groups",
            "count" to groups.size,
            "data" to groups
        )
        return Response.ok("extract_contact_groups", payload)
    }

    private fun queryMemberCount(groupId: Long): Int {
        val memberCursor = context.contentResolver.query(
            ContactsContract.Data.CONTENT_URI,
            arrayOf(ContactsContract.Data._ID),
            "${ContactsContract.Data.MIMETYPE} = ? AND ${ContactsContract.CommonDataKinds.GroupMembership.GROUP_ROW_ID} = ?",
            arrayOf(
                ContactsContract.CommonDataKinds.GroupMembership.CONTENT_ITEM_TYPE,
                groupId.toString()
            ),
            null
        ) ?: return 0

        return memberCursor.use { it.count }
    }

    private suspend fun handleInject(cmd: Map<String, Any?>, server: SocketServer): String {
        @Suppress("UNCHECKED_CAST")
        val dataRaw = cmd["data"] as? List<*>
            ?: return Response.error("inject_contact_groups", "MISSING_DATA", "No data array provided")

        val dataType = object : TypeToken<List<Map<String, Any?>>>() {}.type
        val dataJson = gson.toJson(dataRaw)
        val groups: List<Map<String, Any?>> = gson.fromJson(dataJson, dataType)

        val total = groups.size
        var injected = 0
        var failed = 0

        groups.forEachIndexed { index, group ->
            try {
                val title = group["title"] as? String ?: ""
                val visible = group["visible"] as? Boolean ?: true
                val notes = group["notes"] as? String
                val accountName = group["account_name"] as? String ?: "phone"
                val accountType = group["account_type"] as? String ?: "phone"

                val values = ContentValues().apply {
                    put(ContactsContract.Groups.TITLE, title)
                    put(ContactsContract.Groups.GROUP_VISIBLE, if (visible) 1 else 0)
                    if (!notes.isNullOrEmpty()) {
                        put(ContactsContract.Groups.NOTES, notes)
                    }
                    put(ContactsContract.Groups.ACCOUNT_NAME, accountName)
                    put(ContactsContract.Groups.ACCOUNT_TYPE, accountType)
                }

                val result = context.contentResolver.insert(
                    ContactsContract.Groups.CONTENT_URI,
                    values
                )
                if (result != null) injected++ else failed++
            } catch (e: SecurityException) {
                failed++
            } catch (e: Exception) {
                failed++
            }

            val processed = index + 1
            if (processed % 20 == 0) {
                server.sendProgress("contact_groups", processed, total)
            }
        }

        server.sendProgress("contact_groups", total, total)

        val payload = mapOf(
            "category" to "contact_groups",
            "injected" to injected,
            "failed" to failed
        )
        return Response.ok("inject_contact_groups", payload)
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
