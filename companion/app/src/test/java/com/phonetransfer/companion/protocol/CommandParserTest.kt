package com.phonetransfer.companion.protocol

import org.junit.Assert.*
import org.junit.Test

/**
 * Unit tests for [CommandParser] — the JSON → Map deserialiser used by
 * [SocketServer] to route incoming commands.
 *
 * No Android dependencies.
 */
class CommandParserTest {

    // ------------------------------------------------------------------
    // parse()
    // ------------------------------------------------------------------

    @Test
    fun `parse valid command json returns map with cmd key`() {
        val map = CommandParser.parse("""{"cmd":"ping"}""")
        assertEquals("ping", map["cmd"])
    }

    @Test
    fun `parse returns all top-level keys`() {
        val map = CommandParser.parse("""{"cmd":"inject_sms","_v":2,"_seq":5,"_type":"iq"}""")
        assertEquals("inject_sms", map["cmd"])
        assertEquals(2.0, map["_v"])
        assertEquals(5.0, map["_seq"])
        assertEquals("iq", map["_type"])
    }

    @Test
    fun `parse empty json object returns empty map`() {
        val map = CommandParser.parse("{}")
        assertTrue(map.isEmpty())
    }

    @Test
    fun `parse nested data array is accessible`() {
        val map = CommandParser.parse("""{"cmd":"inject_contacts","items":[{"name":"Alice"},{"name":"Bob"}]}""")
        @Suppress("UNCHECKED_CAST")
        val items = map["items"] as? List<*>
        assertNotNull(items)
        assertEquals(2, items!!.size)
        @Suppress("UNCHECKED_CAST")
        val first = items[0] as? Map<String, Any?>
        assertEquals("Alice", first?.get("name"))
    }

    @Test
    fun `parse json null literal returns empty map`() {
        // Gson.fromJson("null", MapType) returns null — CommandParser must handle that.
        val map = CommandParser.parse("null")
        assertTrue("null literal should produce empty map", map.isEmpty())
    }

    @Test
    fun `parse boolean and numeric fields preserve types`() {
        val map = CommandParser.parse("""{"is_sent":true,"timestamp":1700000000000,"count":3}""")
        assertEquals(true, map["is_sent"])
        // Gson deserialises all JSON numbers as Double in Map<String, Any?>
        assertEquals(1.7E12, (map["timestamp"] as? Double) ?: 0.0, 1e8)
        assertEquals(3.0, map["count"])
    }

    @Test
    fun `parse categories list from hello handshake`() {
        val json = """{"cmd":"hello","_v":2,"compress":"zlib"}"""
        val map = CommandParser.parse(json)
        assertEquals("hello", map["cmd"])
        assertEquals(2.0, map["_v"])
        assertEquals("zlib", map["compress"])
    }

    // ------------------------------------------------------------------
    // Wire format round-trip: Python dict → JSON string → CommandParser map
    // ------------------------------------------------------------------

    @Test
    fun `inject_sms command round-trips through parser`() {
        // Simulates a frame arriving from the Python CompanionClient
        val wireJson = """
            {
              "cmd": "inject_sms",
              "_v": 2,
              "_type": "iq",
              "_seq": 12,
              "items": [
                {
                  "platform_id": "1001",
                  "sender": "+15551234567",
                  "recipient": "self",
                  "body": "Hello from iOS",
                  "timestamp": 1700000000000,
                  "is_sent": false,
                  "service": "sms",
                  "read": true,
                  "sms_type": 1,
                  "thread_id": 3,
                  "status": -1
                }
              ]
            }
        """.trimIndent()

        val map = CommandParser.parse(wireJson)
        assertEquals("inject_sms", map["cmd"])
        assertEquals(12.0, map["_seq"])

        @Suppress("UNCHECKED_CAST")
        val items = map["items"] as? List<*>
        assertNotNull(items)
        assertEquals(1, items!!.size)

        @Suppress("UNCHECKED_CAST")
        val msg = items[0] as? Map<String, Any?>
        assertNotNull(msg)
        assertEquals("+15551234567", msg!!["sender"])
        assertEquals("Hello from iOS", msg["body"])
        assertEquals(false, msg["is_sent"])
        assertEquals(true, msg["read"])
    }

    @Test
    fun `capabilities response from APK has expected structure`() {
        // Simulates CommandParser reading a capabilities response for validation
        val json = Response.ok("capabilities", mapOf(
            "categories" to SUPPORTED_CATEGORIES,
            "protocol_version" to PROTOCOL_VERSION
        ))
        val map = CommandParser.parse(json)
        assertEquals("ok", map["status"])
        @Suppress("UNCHECKED_CAST")
        val cats = map["categories"] as? List<*>
        assertNotNull(cats)
        assertTrue(cats!!.contains("contacts"))
        assertTrue(cats.contains("sms"))
    }
}
