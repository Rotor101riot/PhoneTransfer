package com.phonetransfer.companion.handlers

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import com.phonetransfer.companion.SocketServer
import com.phonetransfer.companion.protocol.Response
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private const val TAG = "ClipboardHandler"

class ClipboardHandler(private val context: Context) {

    fun registerExtract(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["extract_clipboard"] = { _, _ ->
            extractClipboard()
        }
    }

    fun registerInject(registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>) {
        registry["inject_clipboard"] = { params, _ ->
            injectClipboard(params)
        }
    }

    // -----------------------------------------------------------------------
    // Extract
    // -----------------------------------------------------------------------

    private suspend fun extractClipboard(): String {
        val items = withContext(Dispatchers.Main) {
            val clipboardManager =
                context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager

            if (!clipboardManager.hasPrimaryClip()) {
                return@withContext emptyList<Map<String, Any?>>()
            }

            val clip = clipboardManager.primaryClip ?: return@withContext emptyList<Map<String, Any?>>()
            val result = mutableListOf<Map<String, Any?>>()

            for (i in 0 until clip.itemCount) {
                val item = clip.getItemAt(i)
                val text = item.coerceToText(context).toString()
                val mimeType = if (clip.description != null && clip.description.mimeTypeCount > 0) {
                    clip.description.getMimeType(0)
                } else {
                    null
                }
                result.add(
                    mapOf(
                        "text" to text,
                        "mime_type" to mimeType
                    )
                )
            }

            result
        }

        return Response.ok(
            "extract_clipboard",
            mapOf(
                "category" to "clipboard",
                "count" to items.size,
                "data" to items
            )
        )
    }

    // -----------------------------------------------------------------------
    // Inject
    // -----------------------------------------------------------------------

    private suspend fun injectClipboard(params: Map<String, Any?>): String {
        @Suppress("UNCHECKED_CAST")
        val data = params["data"] as? List<Map<String, Any?>>
            ?: return Response.error("inject_clipboard", "missing_param", "param 'data' is required")

        if (data.isEmpty()) {
            return Response.error("inject_clipboard", "invalid_param", "data list must not be empty")
        }

        val text = data[0]["text"] as? String
            ?: return Response.error("inject_clipboard", "invalid_param", "first data entry must contain 'text' key")

        withContext(Dispatchers.Main) {
            val clipboardManager =
                context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            val clipData = ClipData.newPlainText("PhoneTransfer", text)
            clipboardManager.setPrimaryClip(clipData)
        }

        return Response.ok(
            "inject_clipboard",
            mapOf(
                "category" to "clipboard",
                "injected" to 1
            )
        )
    }
}
