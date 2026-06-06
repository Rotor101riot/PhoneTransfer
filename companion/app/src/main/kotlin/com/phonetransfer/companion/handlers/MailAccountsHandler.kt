package com.phonetransfer.companion.handlers

import android.accounts.AccountManager
import android.content.Context
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response

/**
 * Extracts configured email account metadata from Android's [AccountManager].
 *
 * Only account type and email address are returned — no passwords or auth
 * tokens are ever accessed.  The goal is to let the user know which accounts
 * to re-configure on the destination device.
 */
class MailAccountsHandler(private val context: Context) {

    companion object {
        /**
         * AccountManager types that correspond to email accounts.
         * Covers the vast majority of Android devices in the wild.
         */
        private val EMAIL_ACCOUNT_TYPES = setOf(
            "com.google",                        // Gmail
            "com.google.android.gm.legacyimap",  // Gmail configured IMAP
            "com.microsoft.exchange",             // Exchange / Outlook
            "com.microsoft.office.outlook",       // Outlook app
            "com.android.exchange",               // Samsung Exchange
            "com.samsung.android.email.provider", // Samsung Mail
            "com.yahoo.mobile.client.android.im", // Yahoo Mail
            "org.mozilla.thunderbird",            // Thunderbird
        )

        /** Broader filter: anything with "mail", "imap", "pop3", or "exchange". */
        private val EMAIL_KEYWORDS = listOf("mail", "imap", "pop3", "exchange", "email", "smtp")
    }

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_mail_accounts"] = { cmd, server ->
            handleExtract(cmd, server)
        }
    }

    private suspend fun handleExtract(cmd: Map<String, Any?>, server: SocketServer): String {
        val accountManager = AccountManager.get(context)

        val accounts = try {
            accountManager.accounts
        } catch (e: SecurityException) {
            return Response.error(
                "extract_mail_accounts",
                "PERMISSION_DENIED",
                "GET_ACCOUNTS permission not granted"
            )
        }

        val results = mutableListOf<Map<String, Any?>>()

        for (account in accounts) {
            val typeLower = account.type.lowercase()
            val isEmail = account.type in EMAIL_ACCOUNT_TYPES ||
                    EMAIL_KEYWORDS.any { kw -> kw in typeLower }

            if (!isEmail) continue

            results.add(
                mapOf(
                    "email" to account.name,
                    "account_type" to account.type,
                    "display_name" to account.name,  // Android AccountManager doesn't expose a separate display name
                )
            )
        }

        server.sendProgress("mail_accounts", results.size, results.size)

        val payload = mapOf(
            "category" to "mail_accounts",
            "count" to results.size,
            "data" to results
        )
        return Response.ok("extract_mail_accounts", payload)
    }
}
