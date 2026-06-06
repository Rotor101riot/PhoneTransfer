package com.phonetransfer.companion.handlers

import android.content.ContentProviderOperation
import android.content.Context
import android.util.Log
import android.provider.ContactsContract
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.phonetransfer.companion.protocol.Response
import com.phonetransfer.companion.SocketServer

private const val TAG = "ContactsHandler"

class ContactsHandler(private val context: Context) {

    private val gson = Gson()

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_contacts"] = { cmd, server ->
            handleExtract(cmd, server)
        }
    }

    fun registerInject(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["inject_contacts"] = { cmd, server ->
            handleInject(cmd, server)
        }
    }

    private suspend fun handleExtract(cmd: Map<String, Any?>, server: SocketServer): String {
        val contacts = mutableListOf<Map<String, Any?>>()

        val contactsCursor = context.contentResolver.query(
            ContactsContract.Contacts.CONTENT_URI,
            arrayOf(
                ContactsContract.Contacts._ID,
                ContactsContract.Contacts.DISPLAY_NAME_PRIMARY
            ),
            null,
            null,
            null
        ) ?: return Response.error("extract_contacts", "QUERY_FAILED", "Failed to query contacts")

        var processed = 0
        val total = contactsCursor.count

        contactsCursor.use { cursor ->
            while (cursor.moveToNext()) {
                val contactId = cursor.safeLong(ContactsContract.Contacts._ID)
                val displayName = cursor.safeString(ContactsContract.Contacts.DISPLAY_NAME_PRIMARY) ?: ""

                val (firstName, lastName) = queryStructuredName(contactId).let { (first, last) ->
                    // Fall back to display name split if structured name fields are empty
                    if (first == null && last == null) splitDisplayName(displayName) else Pair(first, last)
                }
                val phones = queryPhones(contactId)
                val emails = queryEmails(contactId)
                val (organization, note) = queryOrgAndNote(contactId)

                contacts.add(
                    mapOf(
                        "first_name" to firstName,
                        "last_name" to lastName,
                        "phones" to phones,
                        "emails" to emails,
                        "organization" to organization,
                        "note" to note
                    )
                )

                processed++
                if (processed % 50 == 0) {
                    server.sendProgress("contacts", processed, total)
                }
            }
        }

        server.sendProgress("contacts", total, total)

        val payload = mapOf(
            "category" to "contacts",
            "count" to contacts.size,
            "data" to contacts
        )
        return Response.ok("extract_contacts", payload)
    }

    private suspend fun handleInject(cmd: Map<String, Any?>, server: SocketServer): String {
        @Suppress("UNCHECKED_CAST")
        val dataRaw = cmd["data"] as? List<*>
            ?: return Response.error("inject_contacts", "MISSING_DATA", "No data array provided")

        val dataType = object : TypeToken<List<Map<String, Any?>>>() {}.type
        val dataJson = gson.toJson(dataRaw)
        val contacts: List<Map<String, Any?>> = gson.fromJson(dataJson, dataType)

        val total = contacts.size
        var injected = 0
        var failed = 0

        contacts.forEachIndexed { index, contact ->
            try {
                val ops = ArrayList<ContentProviderOperation>()
                val rawContactIndex = ops.size

                ops.add(
                    ContentProviderOperation.newInsert(ContactsContract.RawContacts.CONTENT_URI)
                        .withValue(ContactsContract.RawContacts.ACCOUNT_TYPE, null)
                        .withValue(ContactsContract.RawContacts.ACCOUNT_NAME, null)
                        .build()
                )

                val firstName = contact["first_name"] as? String ?: ""
                val lastName = contact["last_name"] as? String ?: ""
                if (firstName.isNotEmpty() || lastName.isNotEmpty()) {
                    ops.add(
                        ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                            .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawContactIndex)
                            .withValue(
                                ContactsContract.Data.MIMETYPE,
                                ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE
                            )
                            .withValue(ContactsContract.CommonDataKinds.StructuredName.GIVEN_NAME, firstName)
                            .withValue(ContactsContract.CommonDataKinds.StructuredName.FAMILY_NAME, lastName)
                            .build()
                    )
                }

                @Suppress("UNCHECKED_CAST")
                val phones = contact["phones"] as? List<String> ?: emptyList()
                for (phone in phones) {
                    ops.add(
                        ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                            .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawContactIndex)
                            .withValue(
                                ContactsContract.Data.MIMETYPE,
                                ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE
                            )
                            .withValue(ContactsContract.CommonDataKinds.Phone.NUMBER, phone)
                            .withValue(
                                ContactsContract.CommonDataKinds.Phone.TYPE,
                                ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE
                            )
                            .build()
                    )
                }

                @Suppress("UNCHECKED_CAST")
                val emails = contact["emails"] as? List<String> ?: emptyList()
                for (email in emails) {
                    ops.add(
                        ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                            .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawContactIndex)
                            .withValue(
                                ContactsContract.Data.MIMETYPE,
                                ContactsContract.CommonDataKinds.Email.CONTENT_ITEM_TYPE
                            )
                            .withValue(ContactsContract.CommonDataKinds.Email.ADDRESS, email)
                            .withValue(
                                ContactsContract.CommonDataKinds.Email.TYPE,
                                ContactsContract.CommonDataKinds.Email.TYPE_HOME
                            )
                            .build()
                    )
                }

                val organization = contact["organization"] as? String
                if (!organization.isNullOrEmpty()) {
                    ops.add(
                        ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                            .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawContactIndex)
                            .withValue(
                                ContactsContract.Data.MIMETYPE,
                                ContactsContract.CommonDataKinds.Organization.CONTENT_ITEM_TYPE
                            )
                            .withValue(ContactsContract.CommonDataKinds.Organization.COMPANY, organization)
                            .build()
                    )
                }

                val note = contact["note"] as? String
                if (!note.isNullOrEmpty()) {
                    ops.add(
                        ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                            .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawContactIndex)
                            .withValue(
                                ContactsContract.Data.MIMETYPE,
                                ContactsContract.CommonDataKinds.Note.CONTENT_ITEM_TYPE
                            )
                            .withValue(ContactsContract.CommonDataKinds.Note.NOTE, note)
                            .build()
                    )
                }

                context.contentResolver.applyBatch(ContactsContract.AUTHORITY, ops)
                injected++
            } catch (e: Exception) {
                Log.w(TAG, "Failed to inject contact #${index + 1} (${contact["first_name"]} ${contact["last_name"]}): ${e.message}")
                failed++
            }

            val processed = index + 1
            if (processed % 50 == 0) {
                server.sendProgress("contacts", processed, total)
            }
        }

        server.sendProgress("contacts", total, total)

        val payload = mapOf(
            "category" to "contacts",
            "injected" to injected,
            "failed" to failed
        )
        return Response.ok("inject_contacts", payload)
    }

    private fun queryStructuredName(contactId: Long): Pair<String?, String?> {
        val cursor = context.contentResolver.query(
            ContactsContract.Data.CONTENT_URI,
            arrayOf(
                ContactsContract.CommonDataKinds.StructuredName.GIVEN_NAME,
                ContactsContract.CommonDataKinds.StructuredName.FAMILY_NAME
            ),
            "${ContactsContract.Data.CONTACT_ID} = ? AND ${ContactsContract.Data.MIMETYPE} = ?",
            arrayOf(
                contactId.toString(),
                ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE
            ),
            null
        ) ?: return Pair(null, null)

        cursor.use {
            if (it.moveToFirst()) {
                val givenIdx = it.getColumnIndex(ContactsContract.CommonDataKinds.StructuredName.GIVEN_NAME)
                val familyIdx = it.getColumnIndex(ContactsContract.CommonDataKinds.StructuredName.FAMILY_NAME)
                val given = if (givenIdx >= 0) it.getString(givenIdx) else null
                val family = if (familyIdx >= 0) it.getString(familyIdx) else null
                return Pair(given?.ifEmpty { null }, family?.ifEmpty { null })
            }
        }
        return Pair(null, null)
    }

    private fun splitDisplayName(displayName: String): Pair<String?, String?> {
        val parts = displayName.trim().split(" ", limit = 2)
        return when (parts.size) {
            0 -> Pair(null, null)
            1 -> Pair(parts[0].ifEmpty { null }, null)
            else -> Pair(parts[0].ifEmpty { null }, parts[1].ifEmpty { null })
        }
    }

    private fun queryPhones(contactId: Long): List<String> {
        val phones = mutableListOf<String>()
        val cursor = context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Phone.NUMBER),
            "${ContactsContract.CommonDataKinds.Phone.CONTACT_ID} = ?",
            arrayOf(contactId.toString()),
            null
        ) ?: return phones

        cursor.use {
            while (it.moveToNext()) {
                val number = it.safeString(ContactsContract.CommonDataKinds.Phone.NUMBER)
                if (!number.isNullOrEmpty()) phones.add(number)
            }
        }
        return phones
    }

    private fun queryEmails(contactId: Long): List<String> {
        val emails = mutableListOf<String>()
        val cursor = context.contentResolver.query(
            ContactsContract.CommonDataKinds.Email.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Email.ADDRESS),
            "${ContactsContract.CommonDataKinds.Email.CONTACT_ID} = ?",
            arrayOf(contactId.toString()),
            null
        ) ?: return emails

        cursor.use {
            while (it.moveToNext()) {
                val address = it.safeString(ContactsContract.CommonDataKinds.Email.ADDRESS)
                if (!address.isNullOrEmpty()) emails.add(address)
            }
        }
        return emails
    }

    private fun queryOrgAndNote(contactId: Long): Pair<String?, String?> {
        var organization: String? = null
        var note: String? = null

        val cursor = context.contentResolver.query(
            ContactsContract.Data.CONTENT_URI,
            arrayOf(
                ContactsContract.Data.MIMETYPE,
                ContactsContract.CommonDataKinds.Organization.COMPANY,
                ContactsContract.CommonDataKinds.Note.NOTE
            ),
            "${ContactsContract.Data.CONTACT_ID} = ? AND (${ContactsContract.Data.MIMETYPE} = ? OR ${ContactsContract.Data.MIMETYPE} = ?)",
            arrayOf(
                contactId.toString(),
                ContactsContract.CommonDataKinds.Organization.CONTENT_ITEM_TYPE,
                ContactsContract.CommonDataKinds.Note.CONTENT_ITEM_TYPE
            ),
            null
        ) ?: return Pair(null, null)

        cursor.use {
            while (it.moveToNext()) {
                val mimetype = it.safeString(ContactsContract.Data.MIMETYPE)
                when (mimetype) {
                    ContactsContract.CommonDataKinds.Organization.CONTENT_ITEM_TYPE -> {
                        val colIndex = it.getColumnIndex(ContactsContract.CommonDataKinds.Organization.COMPANY)
                        if (colIndex >= 0) organization = it.getString(colIndex)
                    }
                    ContactsContract.CommonDataKinds.Note.CONTENT_ITEM_TYPE -> {
                        val colIndex = it.getColumnIndex(ContactsContract.CommonDataKinds.Note.NOTE)
                        if (colIndex >= 0) note = it.getString(colIndex)
                    }
                }
            }
        }
        return Pair(organization, note)
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
