package com.phonetransfer.companion.protocol

import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.zip.Deflater
import java.util.zip.DeflaterOutputStream
import java.util.zip.Inflater
import java.util.zip.InflaterOutputStream

/**
 * PhoneTransfer frame protocol — shared by both JSON control messages and
 * raw binary file-transfer chunks.
 *
 * Wire format (same for both variants):
 *   [4 bytes, little-endian uint32]  — body length in bytes
 *   [N bytes]                        — body (UTF-8 JSON for control frames;
 *                                       raw bytes for binary chunks)
 *
 * **Transparent compression** (negotiated via `hello`):
 *   JSON frames larger than [COMPRESS_THRESHOLD] bytes may be zlib-compressed
 *   before framing.  The receiver detects compression by checking the first
 *   byte: `0x78` = zlib, anything else = raw JSON.  This is safe because
 *   valid JSON must start with `{`, `[`, `"`, etc. — none of which is `0x78`.
 *
 * The receiver knows from command-flow context whether to interpret the body
 * as a JSON string (via [read]) or raw bytes (via [readBinary]):
 *   • After `{cmd: file_pull}` → expect JSON header, then N binary chunks,
 *     then JSON done frame.
 *   • After sending `{cmd: file_push}` → expect JSON "ready" frame, then
 *     send N binary chunks, then expect JSON result frame.
 */
object Frame {

    private const val MAX_JSON_FRAME_SIZE = 4 * 1024 * 1024   // 4 MB — generous for any JSON command
    private const val MAX_BINARY_FRAME_SIZE = 64 * 1024 * 1024 // 64 MB — file transfer chunks

    /** Minimum payload size before zlib compression is attempted. */
    const val COMPRESS_THRESHOLD = 4096

    /** zlib default compression magic byte (CMF with method=8, info=7). */
    private const val ZLIB_MAGIC: Byte = 0x78.toByte()

    // -----------------------------------------------------------------------
    // JSON frame  (control messages)
    // -----------------------------------------------------------------------

    /**
     * Write a JSON string as a length-prefixed frame.
     *
     * @param compress If true and the payload exceeds [COMPRESS_THRESHOLD],
     *   the body is zlib-compressed before framing.
     */
    fun write(out: OutputStream, json: String, compress: Boolean = false) {
        val body = json.toByteArray(Charsets.UTF_8)
        val payload = if (compress && body.size > COMPRESS_THRESHOLD) {
            deflate(body) ?: body  // fall back to raw if compression expands
        } else {
            body
        }
        writeLength(out, payload.size)
        out.write(payload)
        out.flush()
    }

    /**
     * Read one JSON frame, transparently decompressing if zlib-wrapped.
     * Blocks until a full frame arrives.
     */
    fun read(input: InputStream): String {
        val length = readLength(input, MAX_JSON_FRAME_SIZE)
        val body = readExactly(input, length)
        val decompressed = if (body.isNotEmpty() && body[0] == ZLIB_MAGIC) {
            inflate(body)
        } else {
            body
        }
        return String(decompressed, Charsets.UTF_8)
    }

    // -----------------------------------------------------------------------
    // Binary frame  (file-transfer chunks)
    // -----------------------------------------------------------------------

    /**
     * Write raw bytes as a length-prefixed binary frame.
     * Used when streaming file chunks to the PC.
     */
    fun writeBinary(out: OutputStream, data: ByteArray) {
        writeLength(out, data.size)
        out.write(data)
        out.flush()
    }

    /**
     * Read one binary frame and return the raw byte array.
     * Blocks until a full frame arrives.
     * Used when receiving file chunks from the PC.
     */
    fun readBinary(input: InputStream): ByteArray {
        val length = readLength(input, MAX_BINARY_FRAME_SIZE)
        return readExactly(input, length)
    }

    // -----------------------------------------------------------------------
    // Shared helpers
    // -----------------------------------------------------------------------

    private fun writeLength(out: OutputStream, length: Int) {
        val bytes = ByteBuffer.allocate(4)
            .order(ByteOrder.LITTLE_ENDIAN)
            .putInt(length)
            .array()
        out.write(bytes)
    }

    private fun readLength(input: InputStream, maxSize: Int): Int {
        val buf = readExactly(input, 4)
        val length = ByteBuffer.wrap(buf).order(ByteOrder.LITTLE_ENDIAN).int
        if (length < 0 || length > maxSize) {
            throw IOException("Frame too large: $length bytes (max $maxSize)")
        }
        return length
    }

    /**
     * Read exactly [count] bytes from [input], blocking until all bytes arrive.
     * Throws [EOFException] if the stream closes before [count] bytes are read.
     */
    private fun readExactly(input: InputStream, count: Int): ByteArray {
        val buf = ByteArray(count)
        var offset = 0
        while (offset < count) {
            val read = input.read(buf, offset, count - offset)
            if (read == -1) throw EOFException("Stream closed after $offset of $count bytes")
            offset += read
        }
        return buf
    }

    // -----------------------------------------------------------------------
    // Compression helpers
    // -----------------------------------------------------------------------

    /**
     * Zlib-compress [data]. Returns the compressed bytes, or null if the
     * compressed result is not smaller than the input (not worth it).
     */
    private fun deflate(data: ByteArray): ByteArray? {
        val baos = ByteArrayOutputStream(data.size / 2)
        DeflaterOutputStream(baos, Deflater(Deflater.DEFAULT_COMPRESSION, false)).use { dos ->
            dos.write(data)
        }
        val compressed = baos.toByteArray()
        return if (compressed.size < data.size) compressed else null
    }

    /**
     * Zlib-decompress [data].
     */
    private fun inflate(data: ByteArray): ByteArray {
        val baos = ByteArrayOutputStream(data.size * 3)
        InflaterOutputStream(baos, Inflater(false)).use { ios ->
            ios.write(data)
        }
        return baos.toByteArray()
    }
}
