# PhoneTransfer Companion — ProGuard / R8 rules

# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------
# HandlerRegistry.kt registers every category handler by calling its class
# constructor and then registerExtract / registerInject / register methods.
# R8 sees no reflection, but does see the direct constructor calls — however
# the handler *classes* are only referenced via the extension function
# SocketServer.registerAllHandlers(), which is an inline-site call.
# Keeping all handler class members prevents R8 from inlining away the
# registerXxx methods that the registry expects to call.
-keep class com.phonetransfer.companion.handlers.** { *; }

# SocketServer holds the handler map as Map<String, (cmd, server) -> Response>.
# Keep the lambdas and the SocketServer itself so R8 doesn't collapse the map.
-keep class com.phonetransfer.companion.SocketServer { *; }
-keepclassmembers class com.phonetransfer.companion.SocketServer {
    public <fields>;
    public <methods>;
}

# ---------------------------------------------------------------------------
# SMS registration components
# ---------------------------------------------------------------------------
# HeadlessSmsSendService, SmsReceiver, MmsReceiver, ChangeDefaultSmsActivity
# are all registered in AndroidManifest.xml and must not be removed or renamed.
-keep class com.phonetransfer.companion.sms.** { *; }

# ---------------------------------------------------------------------------
# Protocol + data classes used by Gson serialisation
# ---------------------------------------------------------------------------
-keepclassmembers class com.phonetransfer.companion.protocol.** {
    <fields>;
}
-keep class com.phonetransfer.companion.protocol.** { *; }

# ---------------------------------------------------------------------------
# Coroutines
# ---------------------------------------------------------------------------
-keepnames class kotlinx.coroutines.internal.MainDispatcherFactory {}
-keepnames class kotlinx.coroutines.CoroutineExceptionHandler {}
-keepclassmembernames class kotlinx.** {
    volatile <fields>;
}

# ---------------------------------------------------------------------------
# Gson
# ---------------------------------------------------------------------------
-keepattributes Signature
-keepattributes *Annotation*
-dontwarn sun.misc.**
-keep class com.google.gson.** { *; }
-keep class * implements com.google.gson.TypeAdapterFactory
-keep class * implements com.google.gson.JsonSerializer
-keep class * implements com.google.gson.JsonDeserializer

# ---------------------------------------------------------------------------
# Android system reflection used by OEM skin detection
# ---------------------------------------------------------------------------
# HandlerRegistry.kt calls Class.forName("android.os.SystemProperties") to
# read ro.miui.*, ro.build.version.emui, etc.  Keep the method name so
# reflection survives R8's aggressive method inlining.
-keepclassmembers class android.os.SystemProperties {
    public static java.lang.String get(java.lang.String, java.lang.String);
}

# ---------------------------------------------------------------------------
# General Android
# ---------------------------------------------------------------------------
# Preserve source file names and line numbers in crash stack traces.
-keepattributes SourceFile,LineNumberTable
# Keep all Service, Activity, BroadcastReceiver subclasses so the manifest
# component references survive minification.
-keep public class * extends android.app.Service
-keep public class * extends android.app.Activity
-keep public class * extends android.content.BroadcastReceiver
