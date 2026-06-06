"""
extract_apps_android.py

Extracts third-party APKs from an Android source device via ADB.

Strategy
--------
1. ``adb shell pm list packages -3 -f`` — lists every user-installed package
   with its base APK path.
2. For each package, ``adb shell pm path <pkg>`` — enumerates *all* APK paths
   including split APKs (base.apk, config.arm64-v8a.apk, etc.).
3. ``adb shell dumpsys package <pkg>`` — reads versionCode + versionName so
   the injector can do version-aware installs.
4. APKs are pulled into  staging_path/apps/<pkg>/  as a flat set of .apk files.

No root is required.  System / pre-installed apps are excluded via the -3 flag.

Returns a list of AppInfo dicts:
    {
        "package":      str,          # e.g. "com.spotify.music"
        "version_code": int,          # numeric version, 0 if unknown
        "version_name": str,          # display version, "" if unknown
        "apk_files":    list[Path],   # local paths of pulled APK(s)
        "apk_size_mb":  float,        # total size of all APKs in MB
    }

The caller (inject_apps_android) filters this list against what is already
installed on the destination device.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath

from core.adb_manager import ADBManager
from core.config_loader import get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Human-readable app name lookup (curated for common packages)
# ---------------------------------------------------------------------------

_APP_NAME_MAP: dict[str, str] = {
    # Messaging & social
    "com.whatsapp":                          "WhatsApp",
    "com.whatsapp.w4b":                      "WhatsApp Business",
    "org.telegram.messenger":                "Telegram",
    "org.telegram.messenger.web":            "Telegram",
    "com.discord":                           "Discord",
    "com.facebook.katana":                   "Facebook",
    "com.facebook.orca":                     "Messenger",
    "com.instagram.android":                 "Instagram",
    "com.twitter.android":                   "X (Twitter)",
    "com.snapchat.android":                  "Snapchat",
    "com.zhiliaoapp.musically":              "TikTok",
    "com.ss.android.ugc.trill":              "TikTok",
    "com.reddit.frontpage":                  "Reddit",
    "com.linkedin.android":                  "LinkedIn",
    "com.pinterest":                         "Pinterest",
    "com.tumblr":                            "Tumblr",
    "com.viber.voip":                        "Viber",
    "jp.naver.line.android":                 "LINE",
    "com.kakao.talk":                        "KakaoTalk",
    "com.tencent.mm":                        "WeChat",
    "org.thoughtcrime.securesms":            "Signal",
    "com.skype.raider":                      "Skype",
    # Streaming & media
    "com.google.android.youtube":            "YouTube",
    "com.netflix.mediaclient":               "Netflix",
    "com.amazon.avod.thirdpartyclient":      "Prime Video",
    "com.hbo.hbonow":                        "Max",
    "com.disneyplus":                        "Disney+",
    "com.hulu.plus":                         "Hulu",
    "com.peacocktv.peacockandroid":          "Peacock",
    "com.plexapp.android":                   "Plex",
    "com.twitch.android.app":               "Twitch",
    "com.spotify.music":                     "Spotify",
    "com.pandora.android":                   "Pandora",
    "com.soundcloud.android":                "SoundCloud",
    "com.deezer.android.app":                "Deezer",
    "com.tidal.android":                     "Tidal",
    "com.shazam.android":                    "Shazam",
    "com.audible.application":               "Audible",
    "com.roku.remote":                       "Roku",
    # Google apps
    "com.google.android.gm":                 "Gmail",
    "com.google.android.apps.maps":          "Google Maps",
    "com.google.android.apps.photos":        "Google Photos",
    "com.google.android.keep":               "Google Keep",
    "com.google.android.calendar":           "Google Calendar",
    "com.google.android.contacts":           "Google Contacts",
    "com.google.android.apps.docs":          "Google Docs",
    "com.google.android.apps.sheets":        "Google Sheets",
    "com.google.android.apps.slides":        "Google Slides",
    "com.google.android.apps.drive":         "Google Drive",
    "com.google.android.apps.translate":     "Google Translate",
    "com.google.android.apps.chromecast.app": "Google Home",
    "com.google.android.apps.authenticator2": "Google Authenticator",
    "com.google.android.apps.bard":          "Gemini",
    # Microsoft
    "com.microsoft.teams":                   "Microsoft Teams",
    "com.microsoft.office.word":             "Microsoft Word",
    "com.microsoft.office.excel":            "Microsoft Excel",
    "com.microsoft.office.powerpoint":       "PowerPoint",
    "com.microsoft.office.outlook":          "Outlook",
    "com.microsoft.launcher":                "Microsoft Launcher",
    "com.microsoft.bing":                    "Microsoft Bing",
    # Productivity
    "com.dropbox.android":                   "Dropbox",
    "com.box.android":                       "Box",
    "com.slack":                             "Slack",
    "com.notion.id":                         "Notion",
    "com.todoist.android.Todoist":           "Todoist",
    "com.evernote":                          "Evernote",
    "com.adobe.reader":                      "Adobe Acrobat",
    # Creative
    "com.adobe.lrmobile":                    "Lightroom",
    "com.adobe.psmobile":                    "Photoshop",
    "com.canva.editor":                      "Canva",
    "com.picsart.studio":                    "PicsArt",
    # Maps & travel
    "com.waze":                              "Waze",
    "com.ubercab":                           "Uber",
    "com.lyft.android":                      "Lyft",
    "com.airbnb.android":                    "Airbnb",
    "com.booking.android":                   "Booking.com",
    "com.expedia.android":                   "Expedia",
    # Food & delivery
    "com.doordash.diner":                    "DoorDash",
    "com.grubhub.android":                   "Grubhub",
    "com.ubereats":                          "Uber Eats",
    "com.yelp.android":                      "Yelp",
    # Shopping
    "com.amazon.mShop.android.shopping":     "Amazon",
    "com.amazon.kindle":                     "Kindle",
    # Finance
    "com.paypal.android.p2pmobile":          "PayPal",
    "com.venmo":                             "Venmo",
    "com.squareup.cash":                     "Cash App",
    "com.robinhood.android":                 "Robinhood",
    "com.coinbase.android":                  "Coinbase",
    # Security
    "com.nordvpn.android":                   "NordVPN",
    "com.expressvpn.vpn":                    "ExpressVPN",
    "com.lastpass.lpandroid":                "LastPass",
    "com.onepassword.android":               "1Password",
    "com.authy.authy":                       "Authy",
    # News & reading
    "com.nytimes.android":                   "New York Times",
    "com.cnn.mobile.android.phone":          "CNN",
    "com.bbc.news":                          "BBC News",
    # Dev / Other
    "com.github.android":                    "GitHub",
    "com.openai.chatgpt":                    "ChatGPT",
    "com.duolingo":                          "Duolingo",
    "com.happymod.apk":                      "HappyMod",
}

# Emoji rules: list of (keywords_in_pkg_name, emoji).  First match wins.
_EMOJI_RULES: list[tuple[tuple[str, ...], str]] = [
    # AI / Chatbots
    (("chatgpt", "openai", "bard", "gemini", "claude", "copilot", "ai"),          "🤖"),
    # Messaging / calling
    (("whatsapp", "telegram", "messenger", "orca", "signal", "discord",
      "viber", "line", "kakao", "wechat", "mm", "skype"),                          "💬"),
    # Social media
    (("instagram", "snapchat", "pinterest", "tumblr"),                             "📸"),
    (("twitter", "reddit", "linkedin", "facebook", "katana", "social",
      "tiktok", "musically"),                                                       "🌐"),
    # Streaming / video
    (("youtube", "netflix", "hulu", "disney", "hbo", "peacock", "plex",
      "twitch", "video", "player", "vlc", "roku", "prime", "avod"),               "🎬"),
    # Music / audio
    (("spotify", "music", "pandora", "soundcloud", "deezer", "tidal",
      "shazam", "audible", "podcast", "radio", "audio"),                           "🎵"),
    # Maps / navigation / transport
    (("maps", "navigation", "gps", "waze", "uber", "lyft",
      "airbnb", "booking", "expedia", "travel", "trip"),                           "🗺️"),
    # Food & delivery
    (("doordash", "grubhub", "ubereats", "yelp", "food",
      "delivery", "restaurant"),                                                    "🍕"),
    # Finance / payments
    (("paypal", "venmo", "cash", "robinhood", "coinbase",
      "banking", "finance", "wallet", ".pay", "revolut"),                          "💳"),
    # Shopping
    (("amazon", "shopping", "shop", "store", "ebay", "etsy"),                     "🛍️"),
    # Games
    (("game", "clash", "candy", "roblox", "minecraft",
      "fortnite", "pubg", "league", "gaming"),                                     "🎮"),
    # Health & fitness
    (("fitness", "health", "workout", "gym", "strava",
      "calorie", "sleep", "medita", "steps"),                                      "💪"),
    # Camera & photo editing
    (("camera", "gallery", "lightroom", "photoshop",
      "picsart", "canva", "photo", "snap"),                                        "📷"),
    # Productivity / office
    (("docs", "sheets", "slides", "office", "word", "excel",
      "powerpoint", "outlook", "teams", "slack", "notion",
      "todoist", "keep", "notes", "tasks", "drive", "evernote"),                  "📋"),
    # Security / VPN / password
    (("vpn", "nordvpn", "expressvpn", "lastpass", "1password",
      "dashlane", "authy", "authenticator", "password"),                           "🔒"),
    # News & reading
    (("news", "kindle", "nytimes", "cnn", "bbc",
      "reuters", "medium", "reader", "book"),                                      "📰"),
    # Phone / calls / SMS
    (("dialer", "phone", "call", "sms", "messaging"),                             "📞"),
    # Education
    (("duolingo", "khan", "academy", "learn", "edu",
      "tutor", "quiz"),                                                             "📚"),
    # Developer tools
    (("github", "dev", "code", "terminal", "adb", "ssh"),                         "👩‍💻"),
]

_GENERIC_APP_EMOJI = "📱"
_SKIP_PKG_PARTS = frozenset({
    "android", "app", "mobile", "www", "lite", "free",
    "pro", "plus", "hd", "com", "org", "net", "io", "co",
})


def _label_for_package(pkg: str) -> str:
    """
    Return a human-readable display name for *pkg*.

    Looks up a curated table first; falls back to deriving a name from the
    package name components (e.g. ``com.spotify.music`` → ``Spotify Music``).
    """
    if pkg in _APP_NAME_MAP:
        return _APP_NAME_MAP[pkg]
    parts = pkg.split(".")
    meaningful = [
        p.title()
        for p in parts
        if p.lower() not in _SKIP_PKG_PARTS and len(p) > 2
    ]
    if meaningful:
        return " ".join(meaningful[:2])
    return parts[-1].title() if parts else pkg


def _emoji_for_package(pkg: str) -> str:
    """Return a category emoji for *pkg* based on keyword matching."""
    lower = pkg.lower()
    for keywords, emoji in _EMOJI_RULES:
        if any(kw in lower for kw in keywords):
            return emoji
    return _GENERIC_APP_EMOJI

# Packages that are impossible to sideload (Google Play Services, GMS core,
# device-specific vendor stacks) or are intentionally excluded.
_SKIP_PREFIXES: tuple[str, ...] = (
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.android.vending",       # Play Store itself
    "com.miui.",
    "com.samsung.",
    "com.sec.",
    "com.huawei.android.",
    "com.phonetransfer.",           # our own companion app — never back it up
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_path: Path,
    privileged: bool = False,
    config=None,
) -> list[dict]:
    """
    Pull third-party APKs from *serial* into *staging_path/apps/*.

    Follows the standard pipeline extractor signature:
        extract(device_id, staging_dir, privileged) -> list

    Selected packages are read from ``config.apps_selected_packages``
    (set by the AppPickerDialog before transfer starts).  Pass ``None``
    (the default) to extract every third-party package.

    Parameters
    ----------
    serial:
        ADB serial of the source device.
    staging_path:
        Session staging directory.  An ``apps/`` sub-directory is created.
    privileged:
        Unused (no root required for APK extraction).  Accepted for API
        compatibility with the pipeline calling convention.
    config:
        Optional pre-built Config.  Defaults to get_config().

    Returns
    -------
    list[dict]
        One AppInfo dict per successfully pulled package.
    """
    cfg = config or get_config()
    adb = ADBManager(cfg)
    # Read selected packages from the config runtime field
    selected_packages = cfg.apps_selected_packages
    apps_dir = staging_path / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)

    packages = _list_packages(adb, serial)
    if not packages:
        logger.info("[apps/android] No third-party packages found on %s", serial)
        return []

    logger.info(
        "[apps/android] Found %d third-party package(s) on %s",
        len(packages), serial,
    )

    if selected_packages is not None:
        selected_set = set(selected_packages)
        packages = {k: v for k, v in packages.items() if k in selected_set}
        logger.info(
            "[apps/android] Filtered to %d selected package(s)", len(packages)
        )

    results: list[dict] = []
    total = len(packages)
    for idx, (pkg, _base_path) in enumerate(packages.items(), 1):
        logger.info("[apps/android] Pulling %s (%d/%d)…", pkg, idx, total)
        info = _pull_package(adb, serial, pkg, apps_dir)
        if info:
            results.append(info)

    logger.info(
        "[apps/android] Successfully staged %d/%d app(s)",
        len(results), total,
    )
    return results


# ---------------------------------------------------------------------------
# Public helper — used by app_picker_dialog to populate the list without
# actually pulling anything.
# ---------------------------------------------------------------------------

def list_packages(serial: str, config=None) -> dict[str, dict]:
    """
    Return a dict mapping package_name -> {version_code, version_name, apk_size_mb}
    for every user-installed app on *serial*.  No files are copied.
    """
    cfg = config or get_config()
    adb = ADBManager(cfg)
    packages = _list_packages(adb, serial)
    result: dict[str, dict] = {}
    for pkg in packages:
        vc, vn = _get_version(adb, serial, pkg)
        size_mb = _estimate_size(adb, serial, pkg)
        result[pkg] = {
            "version_code": vc,
            "version_name": vn,
            "apk_size_mb": size_mb,
            "label": _label_for_package(pkg),
            "emoji": _emoji_for_package(pkg),
        }
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _list_packages(adb: ADBManager, serial: str) -> dict[str, str]:
    """
    Returns {package_name: base_apk_path} for every third-party package.
    Lines from 'pm list packages -3 -f' look like:
        package:/data/app/~~random==/com.example-suffix==/base.apk=com.example
    """
    # --user 0 targets the primary/owner profile.  Without it, ADB shell
    # defaults to the foreground user which may be a Work Profile (user 10+)
    # that ADB shell lacks permission to query, causing SecurityException rc=255.
    stdout, _, rc = adb.shell(serial, "pm list packages -3 -f --user 0", timeout=30)
    if rc != 0 or not stdout.strip():
        return {}

    packages: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        # strip "package:" prefix
        rest = line[len("package:"):]
        # split on last '=' to separate path from package name
        if "=" not in rest:
            continue
        eq_idx = rest.rfind("=")
        apk_path = rest[:eq_idx].strip()
        pkg_name = rest[eq_idx + 1:].strip()
        if not pkg_name or not apk_path:
            continue
        if any(pkg_name.startswith(skip) for skip in _SKIP_PREFIXES):
            continue
        packages[pkg_name] = apk_path

    return packages


def _get_all_apk_paths(adb: ADBManager, serial: str, pkg: str) -> list[str]:
    """
    Use 'pm path <pkg>' to enumerate all APK paths for a package
    (handles split APKs).  Returns list of device-side paths.
    """
    stdout, _, rc = adb.shell(serial, f"pm path --user 0 {pkg}", timeout=15)
    if rc != 0:
        return []
    paths: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            path = line[len("package:"):].strip()
            if path:
                paths.append(path)
    return paths


def _get_version(adb: ADBManager, serial: str, pkg: str) -> tuple[int, str]:
    """Return (version_code, version_name) from dumpsys package output."""
    stdout, _, rc = adb.shell(
        serial, f"dumpsys package {pkg}", timeout=15
    )
    if rc != 0:
        return 0, ""
    vc = 0
    vn = ""
    for line in stdout.splitlines():
        line = line.strip()
        m = re.search(r"versionCode=(\d+)", line)
        if m:
            vc = int(m.group(1))
        m = re.search(r"versionName=(\S+)", line)
        if m:
            vn = m.group(1)
        if vc and vn:
            break
    return vc, vn


def _estimate_size(adb: ADBManager, serial: str, pkg: str) -> float:
    """Return total APK size in MB for *pkg* (sum of all split APKs)."""
    apk_paths = _get_all_apk_paths(adb, serial, pkg)
    if not apk_paths:
        return 0.0
    total_bytes = 0
    for path in apk_paths:
        posix = PurePosixPath(path)
        stdout, _, rc = adb.shell(
            serial, f"stat -c %s {posix}", timeout=10
        )
        if rc == 0:
            try:
                total_bytes += int(stdout.strip())
            except ValueError:
                pass
    return round(total_bytes / 1_048_576, 1)


def _pull_package(
    adb: ADBManager,
    serial: str,
    pkg: str,
    apps_dir: Path,
) -> dict | None:
    """
    Pull all APKs for *pkg* into apps_dir/<pkg>/.
    Returns an AppInfo dict on success, None on failure.
    """
    apk_device_paths = _get_all_apk_paths(adb, serial, pkg)
    if not apk_device_paths:
        logger.warning("[apps/android] No APK paths found for %s — skipping", pkg)
        return None

    pkg_dir = apps_dir / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)

    pulled: list[Path] = []
    for device_path in apk_device_paths:
        filename = PurePosixPath(device_path).name
        local_path = pkg_dir / filename
        if adb.pull(serial, device_path, local_path, timeout=120):
            pulled.append(local_path)
        else:
            logger.warning(
                "[apps/android] Failed to pull %s for %s", device_path, pkg
            )

    if not pulled:
        logger.warning("[apps/android] No APKs pulled for %s", pkg)
        return None

    vc, vn = _get_version(adb, serial, pkg)
    total_mb = sum(p.stat().st_size for p in pulled) / 1_048_576

    return {
        "package":      pkg,
        "version_code": vc,
        "version_name": vn,
        "apk_files":    pulled,
        "apk_size_mb":  round(total_mb, 1),
    }
