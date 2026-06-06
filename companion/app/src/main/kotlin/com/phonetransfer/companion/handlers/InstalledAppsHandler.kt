package com.phonetransfer.companion.handlers

import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.phonetransfer.companion.protocol.Response
import com.phonetransfer.companion.SocketServer
import java.io.File
import java.io.FileInputStream

class InstalledAppsHandler(private val context: Context) {

    private val gson = Gson()

    fun registerExtract(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["extract_installed_apps"] = { cmd, server ->
            handleExtract(cmd, server)
        }
    }

    fun registerInject(
        registry: MutableMap<String, suspend (Map<String, Any?>, SocketServer) -> String>
    ) {
        registry["inject_installed_apps"] = { cmd, server ->
            handleInject(cmd, server)
        }
    }

    @Suppress("DEPRECATION")
    private suspend fun handleExtract(cmd: Map<String, Any?>, server: SocketServer): String {
        val includeApk = cmd["include_apk"] as? Boolean ?: false
        val packageManager = context.packageManager

        val allApps = packageManager.getInstalledApplications(PackageManager.GET_META_DATA)
        val userApps = allApps.filter { (it.flags and ApplicationInfo.FLAG_SYSTEM) == 0 }

        val total = userApps.size
        val appList = mutableListOf<Map<String, Any?>>()

        userApps.forEachIndexed { index, appInfo ->
            val appMap = mutableMapOf<String, Any?>(
                "package_name" to appInfo.packageName,
                "app_name" to packageManager.getApplicationLabel(appInfo).toString(),
                "source_dir" to appInfo.sourceDir,
                "apk_size" to File(appInfo.sourceDir).length(),
                "is_system" to false
            )

            try {
                val pkgInfo = packageManager.getPackageInfo(appInfo.packageName, 0)
                appMap["version_name"] = pkgInfo.versionName
                appMap["version_code"] = if (android.os.Build.VERSION.SDK_INT >= 28) {
                    pkgInfo.longVersionCode
                } else {
                    pkgInfo.versionCode.toLong()
                }
                appMap["install_time"] = pkgInfo.firstInstallTime
                appMap["update_time"] = pkgInfo.lastUpdateTime
            } catch (e: PackageManager.NameNotFoundException) {
                // App was uninstalled between listing and querying
                appMap["version_name"] = null
                appMap["version_code"] = null
                appMap["install_time"] = null
                appMap["update_time"] = null
            }

            appList.add(appMap)

            val processed = index + 1
            if (processed % 10 == 0) {
                server.sendProgress("installed_apps", processed, total)
            }
        }

        server.sendProgress("installed_apps", total, total)

        val payload = mapOf(
            "category" to "installed_apps",
            "count" to appList.size,
            "data" to appList
        )
        val response = Response.ok("extract_installed_apps", payload)

        if (!includeApk) {
            return response
        }

        // When include_apk is true, send the JSON response first, then stream each APK
        server.sendJsonFrame(response)

        userApps.forEachIndexed { index, appInfo ->
            val apkFile = File(appInfo.sourceDir)
            if (!apkFile.exists()) return@forEachIndexed

            val apkSize = apkFile.length()
            val filename = "${appInfo.packageName}.apk"

            // Send header frame for this APK
            val headerJson = gson.toJson(
                mapOf(
                    "cmd" to "app_apk_chunk",
                    "package_name" to appInfo.packageName,
                    "filename" to filename,
                    "size" to apkSize
                )
            )
            server.sendJsonFrame(headerJson)

            // Stream APK in 512KB chunks
            val buffer = ByteArray(512 * 1024)
            FileInputStream(apkFile).use { fis ->
                var bytesRead: Int
                while (fis.read(buffer).also { bytesRead = it } != -1) {
                    if (bytesRead == buffer.size) {
                        server.sendBinaryFrame(buffer)
                    } else {
                        server.sendBinaryFrame(buffer.copyOf(bytesRead))
                    }
                }
            }

            // Send done frame for this APK
            val doneJson = gson.toJson(
                mapOf(
                    "cmd" to "app_apk_done",
                    "package_name" to appInfo.packageName
                )
            )
            server.sendJsonFrame(doneJson)

            val processed = index + 1
            server.sendProgress("installed_apps_apk", processed, userApps.size)
        }

        // Handler already sent all frames; return empty to skip automatic write
        return ""
    }

    private suspend fun handleInject(cmd: Map<String, Any?>, server: SocketServer): String {
        @Suppress("UNCHECKED_CAST")
        val dataRaw = cmd["data"] as? List<*>
            ?: return Response.error("inject_installed_apps", "MISSING_DATA", "No data array provided")

        val dataType = object : TypeToken<List<Map<String, Any?>>>() {}.type
        val dataJson = gson.toJson(dataRaw)
        val apps: List<Map<String, Any?>> = gson.fromJson(dataJson, dataType)

        val payload = mapOf(
            "category" to "installed_apps",
            "received" to apps.size,
            "note" to "App installation requires user action via Play Store"
        )
        return Response.ok("inject_installed_apps", payload)
    }
}
