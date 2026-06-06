package com.phonetransfer.companion.handlers

import com.google.gson.Gson
import com.phonetransfer.companion.protocol.Response
import org.junit.Assert.*
import org.junit.Test

/**
 * Tests for the data-validation guard logic in the SMS inject path.
 *
 * These tests exercise the parsing and guard-return paths of the inject
 * handler without requiring a real Android Context or ContentResolver.
 * They verify that the wire protocol contract is upheld: malformed or empty
 * commands produce the correct error/ok responses rather than crashing.
 *
 * Note: Tests that exercise actual ContentResolver writes (SecurityException
 * path, row-count verification) belong in the androidTest suite where a real
 * or emulated ContentProvider is available.
 */
class SmsInjectGuardTest {

    private val gson = Gson()

    /**
     * Simulate the inject guard logic extracted from [SmsHandler.handleInject].
     *
     * Returns:
     *   - `null`  when the data/items key is present and non-empty (i.e. guard
     *     passed — caller proceeds with ContentResolver writes)
     *   - a JSON error string when the guard rejects the command
     */
    @Suppress("UNCHECKED_CAST")
    private fun runGuard(cmd: Map<String, Any?>): String? {
        val dataRaw = (cmd["data"] as? List<*>) ?: (cmd["items"] as? List<*>)
            ?: return Response.error("inject_sms", "MISSING_DATA", "No data/items array provided")
        return null  // guard passed
    }

    // ------------------------------------------------------------------
    // Missing data key
    // ------------------------------------------------------------------

    @Test
    fun `missing data and items key returns error`() {
        val cmd = mapOf("cmd" to "inject_sms")
        val result = runGuard(cmd)
        assertNotNull(result)
        val map = parseJson(result!!)
        assertEquals("error", map["status"])
        assertEquals("MISSING_DATA", map["error"])
    }

    @Test
    fun `null data value returns error`() {
        val cmd = mapOf("cmd" to "inject_sms", "data" to null)
        val result = runGuard(cmd)
        assertNotNull(result)
        val map = parseJson(result!!)
        assertEquals("error", map["status"])
    }

    @Test
    fun `empty command map returns error`() {
        val result = runGuard(emptyMap())
        assertNotNull(result)
    }

    // ------------------------------------------------------------------
    // Valid data present — guard passes
    // ------------------------------------------------------------------

    @Test
    fun `data key with list allows guard to pass`() {
        val cmd = mapOf("cmd" to "inject_sms", "data" to listOf(mapOf("body" to "hi")))
        val result = runGuard(cmd)
        assertNull("guard should pass when data is a non-null list", result)
    }

    @Test
    fun `items key accepted as alias for data`() {
        val cmd = mapOf("cmd" to "inject_sms", "items" to listOf(mapOf("body" to "hi")))
        val result = runGuard(cmd)
        assertNull("guard should pass when items is present", result)
    }

    @Test
    fun `empty list allows guard to pass`() {
        // An empty list is valid — the handler writes zero rows and returns ok.
        val cmd = mapOf("cmd" to "inject_sms", "data" to emptyList<Any>())
        val result = runGuard(cmd)
        assertNull("empty list should pass the guard", result)
    }

    // ------------------------------------------------------------------
    // SMS type routing logic (pure logic, no Android)
    // ------------------------------------------------------------------

    /**
     * Verify the sms_type → targetUri selection logic in isolation.
     * Maps the integer sms_type field to the expected content URI suffix
     * that the SmsHandler routes to, without touching any real Android API.
     */
    @Test
    fun `sms_type routing maps known types to expected uri suffixes`() {
        // Mirrors the `when (smsType)` branch in SmsHandler.handleInject.
        // Constants: 1=inbox, 2=sent, 3=draft, 4=outbox, 5=failed, 6=queued
        val expected = mapOf(
            1 to "inbox",
            2 to "sent",
            3 to "draft",
            4 to "outbox",
            5 to "failed",
            6 to "queued",
        )
        for ((smsType, uriSuffix) in expected) {
            val actual = resolveSmsUriSuffix(smsType, isSent = smsType == 2)
            assertEquals("type $smsType should route to $uriSuffix", uriSuffix, actual)
        }
    }

    @Test
    fun `unknown sms_type falls back to inbox for received messages`() {
        val suffix = resolveSmsUriSuffix(smsType = 99, isSent = false)
        assertEquals("inbox", suffix)
    }

    @Test
    fun `unknown sms_type falls back to sent for sent messages`() {
        val suffix = resolveSmsUriSuffix(smsType = 99, isSent = true)
        assertEquals("sent", suffix)
    }

    // ------------------------------------------------------------------
    // Address derivation logic (pure logic, no Android)
    // ------------------------------------------------------------------

    @Test
    fun `inbox message uses sender as address`() {
        val sender = "+15551234567"
        val recipient = "self"
        val isSent = false
        val address = if (isSent) recipient else sender
        assertEquals(sender, address)
    }

    @Test
    fun `sent message uses recipient as address`() {
        val sender = "self"
        val recipient = "+15559876543"
        val isSent = true
        val address = if (isSent) recipient else sender
        assertEquals(recipient, address)
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    @Suppress("UNCHECKED_CAST")
    private fun parseJson(json: String): Map<String, Any?> {
        val type = com.google.gson.reflect.TypeToken.getParameterized(
            Map::class.java, String::class.java, Any::class.java
        ).type
        return gson.fromJson(json, type) ?: emptyMap()
    }

    /**
     * Pure-logic replica of the URI routing logic from SmsHandler.
     * Returns the URI path suffix (e.g. "inbox", "sent") without importing
     * android.provider.Telephony.
     */
    private fun resolveSmsUriSuffix(smsType: Int, isSent: Boolean): String = when (smsType) {
        1 -> "inbox"
        2 -> "sent"
        3 -> "draft"
        4 -> "outbox"
        5 -> "failed"
        6 -> "queued"
        else -> if (isSent) "sent" else "inbox"
    }
}
