package com.phonetransfer.companion.protocol

import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import org.junit.Assert.*
import org.junit.Test

/**
 * Unit tests for [Response] — the JSON response builder used by all handlers.
 *
 * No Android dependencies.
 */
class ResponseTest {

    private val gson = Gson()
    private val mapType = object : TypeToken<Map<String, Any?>>() {}.type

    private fun parse(json: String): Map<String, Any?> = gson.fromJson(json, mapType)

    // ------------------------------------------------------------------
    // Response.ok
    // ------------------------------------------------------------------

    @Test
    fun `ok response has status=ok and correct cmd`() {
        val json = Response.ok("ping")
        val map = parse(json)
        assertEquals("ok", map["status"])
        assertEquals("ping", map["cmd"])
    }

    @Test
    fun `ok response with payload merges keys at top level`() {
        val json = Response.ok("extract_contacts", mapOf("count" to 42, "category" to "contacts"))
        val map = parse(json)
        assertEquals("ok", map["status"])
        assertEquals(42.0, map["count"])  // Gson deserialises numbers as Double by default
        assertEquals("contacts", map["category"])
    }

    @Test
    fun `ok response with seq=0 omits v2 fields`() {
        val json = Response.ok("ping", seq = 0)
        val map = parse(json)
        assertFalse("_seq should be absent", map.containsKey("_seq"))
        assertFalse("_v should be absent", map.containsKey("_v"))
        assertFalse("_type should be absent", map.containsKey("_type"))
    }

    @Test
    fun `ok response with seq greater than 0 includes v2 envelope fields`() {
        val json = Response.ok("extract_sms", seq = 7)
        val map = parse(json)
        assertEquals(7.0, map["_seq"])
        assertEquals(PROTOCOL_VERSION.toDouble(), map["_v"])
        assertEquals(MessageType.IQ, map["_type"])
    }

    // ------------------------------------------------------------------
    // Response.error
    // ------------------------------------------------------------------

    @Test
    fun `error response has status=error, code, and message`() {
        val json = Response.error("inject_sms", "PERMISSION_DENIED", "No SMS role")
        val map = parse(json)
        assertEquals("error", map["status"])
        assertEquals("inject_sms", map["cmd"])
        assertEquals("PERMISSION_DENIED", map["error"])
        assertEquals("No SMS role", map["message"])
    }

    @Test
    fun `error response with seq includes v2 envelope fields`() {
        val json = Response.error("inject_contacts", "DB_LOCKED", "DB in use", seq = 3)
        val map = parse(json)
        assertEquals(3.0, map["_seq"])
        assertEquals(PROTOCOL_VERSION.toDouble(), map["_v"])
        assertEquals(MessageType.IQ, map["_type"])
    }

    @Test
    fun `error response with seq=0 omits v2 fields`() {
        val json = Response.error("inject_calls", "WRITE_FAILED", "Cursor returned null")
        val map = parse(json)
        assertFalse(map.containsKey("_seq"))
    }

    // ------------------------------------------------------------------
    // Response.progress
    // ------------------------------------------------------------------

    @Test
    fun `progress response has correct category, done, total`() {
        val json = Response.progress("sms", done = 50, total = 200)
        val map = parse(json)
        assertEquals("progress", map["status"])
        assertEquals("sms", map["category"])
        assertEquals(50.0, map["done"])
        assertEquals(200.0, map["total"])
    }

    @Test
    fun `progress response with done=total is valid`() {
        val json = Response.progress("contacts", 100, 100)
        val map = parse(json)
        assertEquals(100.0, map["done"])
        assertEquals(100.0, map["total"])
    }

    // ------------------------------------------------------------------
    // Response.event
    // ------------------------------------------------------------------

    @Test
    fun `event response has correct type, namespace, and data`() {
        val json = Response.event("battery", mapOf("level" to 42, "charging" to true))
        val map = parse(json)
        assertEquals(MessageType.EVENT, map["_type"])
        assertEquals(PROTOCOL_VERSION.toDouble(), map["_v"])
        assertEquals("battery", map["ns"])
        @Suppress("UNCHECKED_CAST")
        val data = map["data"] as? Map<String, Any?>
        assertNotNull(data)
        assertEquals(42.0, data!!["level"])
        assertEquals(true, data["charging"])
    }

    @Test
    fun `event response with empty data map is valid`() {
        val json = Response.event("screen", emptyMap())
        val map = parse(json)
        assertEquals("screen", map["ns"])
        assertNotNull(map["data"])
    }
}
