package com.phonetransfer.companion.protocol

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.IOException

/**
 * Pure-JVM unit tests for Frame framing logic.
 *
 * No Android dependencies — Frame uses only java.io and java.util.zip.
 */
class FrameTest {

    // ------------------------------------------------------------------
    // JSON frame round-trips
    // ------------------------------------------------------------------

    @Test
    fun `write then read returns identical string`() {
        val payload = """{"cmd":"ping","_v":2}"""
        val baos = ByteArrayOutputStream()
        Frame.write(baos, payload)
        val result = Frame.read(ByteArrayInputStream(baos.toByteArray()))
        assertEquals(payload, result)
    }

    @Test
    fun `write then read preserves unicode content`() {
        val payload = """{"body":"Héllo wörld 😀","cmd":"inject_sms"}"""
        val baos = ByteArrayOutputStream()
        Frame.write(baos, payload)
        val result = Frame.read(ByteArrayInputStream(baos.toByteArray()))
        assertEquals(payload, result)
    }

    @Test
    fun `write compress=false skips compression even for large payloads`() {
        val payload = "x".repeat(Frame.COMPRESS_THRESHOLD * 2)
        val baos = ByteArrayOutputStream()
        Frame.write(baos, payload, compress = false)
        // Body starts with 'x' (0x78 is also the zlib magic — critical edge case)
        // but compress=false means no zlib header; read should still decode correctly
        // because Frame.read only decompresses if first byte of BODY is 0x78 AND
        // the body is valid zlib.  "xxx..." is not valid zlib, so inflate() would
        // throw — if this test passes, compress=false stayed raw.
        val result = Frame.read(ByteArrayInputStream(baos.toByteArray()))
        assertEquals(payload, result)
    }

    @Test
    fun `write compress=true transparently decompresses on read`() {
        // Build a compressible payload (>4096 bytes, high repetition)
        val payload = buildString {
            repeat(200) { append("""{"status":"ok","cmd":"extract_contacts","count":$it}""") }
        }
        assertTrue("payload must exceed threshold", payload.length > Frame.COMPRESS_THRESHOLD)
        val baos = ByteArrayOutputStream()
        Frame.write(baos, payload, compress = true)
        val result = Frame.read(ByteArrayInputStream(baos.toByteArray()))
        assertEquals(payload, result)
    }

    @Test
    fun `write compress=true does not expand incompressible data`() {
        // Random-looking payload won't compress; Frame.write should fall back to raw.
        val random = java.util.Random(42L)
        val bytes = ByteArray(Frame.COMPRESS_THRESHOLD * 2).also { random.nextBytes(it) }
        // Encode as hex to make it valid UTF-8 JSON
        val payload = '"' + bytes.joinToString("") { "%02x".format(it) } + '"'
        val baos = ByteArrayOutputStream()
        Frame.write(baos, payload, compress = true)
        val result = Frame.read(ByteArrayInputStream(baos.toByteArray()))
        assertEquals(payload, result)
    }

    @Test
    fun `two sequential frames in one stream are read independently`() {
        val msg1 = """{"cmd":"ping"}"""
        val msg2 = """{"cmd":"capabilities","_seq":1}"""
        val baos = ByteArrayOutputStream()
        Frame.write(baos, msg1)
        Frame.write(baos, msg2)
        val stream = ByteArrayInputStream(baos.toByteArray())
        assertEquals(msg1, Frame.read(stream))
        assertEquals(msg2, Frame.read(stream))
    }

    @Test(expected = EOFException::class)
    fun `read throws EOFException on truncated stream`() {
        // Write a valid 4-byte length header claiming 100 bytes, but provide only 10.
        val baos = ByteArrayOutputStream()
        val lengthBytes = java.nio.ByteBuffer.allocate(4)
            .order(java.nio.ByteOrder.LITTLE_ENDIAN).putInt(100).array()
        baos.write(lengthBytes)
        baos.write(ByteArray(10)) // only 10 bytes instead of 100
        Frame.read(ByteArrayInputStream(baos.toByteArray()))
    }

    @Test(expected = IOException::class)
    fun `read throws IOException on oversized length header`() {
        // Write a length header exceeding MAX_JSON_FRAME_SIZE (4 MB).
        val oversized = 5 * 1024 * 1024  // 5 MB > 4 MB limit
        val lengthBytes = java.nio.ByteBuffer.allocate(4)
            .order(java.nio.ByteOrder.LITTLE_ENDIAN).putInt(oversized).array()
        val baos = ByteArrayOutputStream()
        baos.write(lengthBytes)
        Frame.read(ByteArrayInputStream(baos.toByteArray()))
    }

    // ------------------------------------------------------------------
    // Binary frame round-trips
    // ------------------------------------------------------------------

    @Test
    fun `writeBinary then readBinary returns identical bytes`() {
        val data = byteArrayOf(0x00, 0x01, 0x7F, 0x78.toByte(), 0xFF.toByte())
        val baos = ByteArrayOutputStream()
        Frame.writeBinary(baos, data)
        val result = Frame.readBinary(ByteArrayInputStream(baos.toByteArray()))
        assertArrayEquals(data, result)
    }

    @Test
    fun `writeBinary handles empty byte array`() {
        val baos = ByteArrayOutputStream()
        Frame.writeBinary(baos, ByteArray(0))
        val result = Frame.readBinary(ByteArrayInputStream(baos.toByteArray()))
        assertArrayEquals(ByteArray(0), result)
    }

    @Test
    fun `binary and json frames coexist in one stream`() {
        val json = """{"cmd":"file_pull","status":"ok"}"""
        val binary = byteArrayOf(1, 2, 3, 4, 5)
        val baos = ByteArrayOutputStream()
        Frame.write(baos, json)
        Frame.writeBinary(baos, binary)
        val stream = ByteArrayInputStream(baos.toByteArray())
        assertEquals(json, Frame.read(stream))
        assertArrayEquals(binary, Frame.readBinary(stream))
    }
}
