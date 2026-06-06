# convert/ — Format Conversion Modules

All format conversion used by the PhoneTransfer pipeline lives here.
Each module is a thin, independently testable wrapper that either calls
FFmpeg (via `core/ffmpeg_wrapper.py`) or uses a pure-Python library.
None of these modules talk to devices — they operate on local files only.

---

## Modules

### `convert_heic.py` — HEIC/HEIF → JPEG
**When used:** iOS → Android photo transfer (Android < 13 cannot decode HEIC natively).

**Library:** `pillow-heif` + Pillow

**Key settings:**
| Parameter | Default | Notes |
|-----------|---------|-------|
| `quality` | `85` | JPEG quality 1–95. 85 balances file size and visual fidelity; use 92–95 for archival. |

**EXIF:** Preserved by Pillow when decoding from HEIF. Orientation tag is honoured on decode.

**Live Photos:** iOS Live Photos are a `.heic` + `.mov` pair. The `.heic` still image is converted to JPEG; the `.mov` motion component is handled separately by `convert_video.py`. Android has no Live Photo equivalent — only the JPEG is delivered.

**Batch entry point:** `convert_batch(input_dir, output_dir, quality=85, on_progress=None)`

---

### `convert_video.py` — MOV / video → MP4
**When used:** iOS → Android video transfer.

**Library:** FFmpeg (via `core/ffmpeg_wrapper.py`)

**Strategy:**
1. **Remux (lossless):** `remux_to_mp4()` — stream-copies H.264/H.265 into an MP4 container with `-movflags +faststart`. Zero quality loss, very fast.
2. **Transcode (fallback):** `transcode_to_mp4()` — full re-encode with `libx264` + AAC when the source codec is incompatible with MP4 (e.g. ProRes, HEVC-in-AVI).

**Public entry point:** `convert(input_path, output_path)` — tries remux, falls back to transcode automatically.

**CRF:** Default `23` (libx264). Lower = higher quality / larger file. Range 18–28 is typical.

---

### `convert_audio.py` — General audio transcoding
**When used:** By `convert_ringtones.py` and any audio-path callers.

**Library:** FFmpeg

**Supported inputs:** `.ogg .m4a .mp3 .aac .caf .wav .flac .opus .m4r`
**Supported outputs:** `.m4a .mp3 .aac .wav .ogg`

**Default bitrate:** `192k`. Pass `sample_rate` (Hz) to override the output sample rate.

---

### `convert_ringtones.py` — Ringtone format conversion
**When used:** Ringtone category transfers (iOS ↔ Android).

**Library:** FFmpeg

| Function | Output | Use case |
|----------|--------|----------|
| `to_m4r()` | `.m4r` AAC | Android/other → iOS ringtone. Enforces iOS 30-second limit by default (`trim_to_30s=True`). |
| `to_mp3()` | `.mp3` 192k | iOS `.m4r` → Android ringtone. MP3 is the safest universal format across all OEMs. |
| `to_m4a()` | `.m4a` AAC 192k | iOS `.m4r` → Android (M4A alternative for modern devices). |

**iOS constraint:** Files longer than 30 seconds are silently ignored by iOS. `to_m4r` trims by default.

---

### `convert_imessage_to_mms.py` — iMessage → SMS/MMS
**When used:** iOS → Android SMS transfer. Called on every message before Android injection.

**Library:** Pure Python (no external deps)

**Rules:**
- Message with attachments → MMS
- Plain-text body > 160 UTF-8 bytes → MMS (carrier SMS limit)
- Otherwise → SMS
- Sender/recipient numbers are normalized to E.164-like format (prepends `+` to bare digit strings ≥ 10 digits)

**Lossiness:** iMessage-specific features are dropped on Android:
- Tapbacks / reactions → dropped (no Android MMS equivalent)
- Sticker attachments → delivered as JPEG
- Digital Touch / Memoji → dropped
- "Sent with" effect annotations → dropped

**Thread grouping:** `group_by_thread(messages)` groups by `{sender, recipient}` pair, sorted by timestamp — use this before batch injection to preserve conversation order.

---

### `convert_mms_attachments.py` — MMS attachment normalisation
**When used:** Android → iOS MMS transfers; also used when injecting MMS into Android.

**Library:** FFmpeg (for media) + Python stdlib

Normalises MIME types, sanitises filenames, and routes each attachment to the correct
size-reduction path before injection into the target platform's message store.

---

### `convert_sms.py` — SMS format bridging
**When used:** Cross-platform SMS normalisation.

**Library:** Pure Python

Handles character-set normalisation (GSM-7 vs UCS-2 detection), splits oversized bodies into
multipart SMS segments, and maps platform-specific "read" / "sent" flags to the normalised
`Message` schema.

---

### `convert_contacts.py` — Contact format bridging
**When used:** Cross-platform contact normalisation before injection.

**Library:** Pure Python

Handles phone number normalisation, vCard 3.0 ↔ Android ContentProvider field mapping,
and deduplication of multi-value fields (e.g. duplicate phone numbers from ABMultiValue).

---

### `convert_calendar.py` — Calendar / iCal bridging
**When used:** Cross-platform calendar normalisation.

**Library:** Pure Python (iCalendar RFC 5545 parsing)

Maps iOS `ZCALCALENDARITEM` Core Data fields to Android `CalendarContract.Events` columns.
Known lossiness: iOS custom recurrence rules with complex `BYSETPOS` patterns may simplify
to the nearest standard RRULE that Android's ContentProvider accepts.

---

### `convert_calllog.py` — Call log format bridging
**When used:** Cross-platform call log normalisation.

**Library:** Pure Python

Maps iOS call types (FaceTime Audio, FaceTime Video, regular) to Android call type constants
(INCOMING=1, OUTGOING=2, MISSED=3). FaceTime-specific types are mapped to regular call types;
the FaceTime distinction is lost on Android.

---

### `convert_notes.py` — Notes format bridging
**When used:** Cross-platform notes normalisation.

**Library:** Pure Python

Decodes Apple Notes RTFD/attributed-string blobs (when accessible) and falls back to
`ZSNIPPET` plain text. Rich formatting (tables, sketches, inline images) is stripped on
Android destinations — only plain text and inline-image references survive.

---

### `convert_bookmarks.py` — Bookmark format bridging
**When used:** Cross-platform bookmark normalisation.

**Library:** Pure Python

Converts Safari bookmark plist format to a flat `{title, url, folder}` schema and back.
Chrome/Firefox bookmark JSON formats on Android are supported for import.

---

### `convert_alarms.py` — Alarm format bridging
**When used:** Alarm category transfers (iOS ↔ Android).

**Library:** Pure Python

Maps iOS `MTAlarmDataVersion` clock model to Android `AlarmClock` intent schema.
Known lossiness: iOS bedtime / Sleep schedule alarms have no Android equivalent and are dropped.

---

### `convert_blocked.py` — Blocked-number list bridging
**When used:** Blocked number category transfers.

**Library:** Pure Python

Normalises blocked number lists between iOS blocklist format and Android
`BlockedNumberContract` ContentProvider rows.

---

### `convert_health.py` — Health data bridging
**When used:** iOS → Android health category (read-only metrics export).

**Library:** Pure Python

Reads `healthdb.sqlite` step counts, heart rate samples, and sleep records.
Android has no standard writable health ContentProvider; output is a JSON
summary file placed in `/sdcard/PhoneTransfer/health_export.json`.

---

### `convert_signal.py` — Signal message bridging
**When used:** Signal category transfers (experimental).

**Library:** Pure Python

Decrypts and normalises Signal message records from the ADB-pulled Signal database.
Cross-device Signal restore is fragile — sealed-sender keys are device-specific.
This module handles the normalisation step only; key management is handled by the
Signal extractor/injector pair.

---

### `convert_whatsapp.py` — WhatsApp message bridging
**When used:** WhatsApp category transfers (experimental).

**Library:** Pure Python

Normalises WhatsApp `msgstore.db` records to the common `Message` schema.
WhatsApp uses E2E encryption; the database is only accessible when the backup key
is available (Android backup key file or iOS unencrypted backup).

---

## FFmpeg dependency

Most format conversions use FFmpeg.  The bundled binary lives in
`bin/ffmpeg/ffmpeg.exe` (Windows) and is located by `core/config_loader.py`.

`core/ffmpeg_wrapper.py` wraps subprocess calls, captures stderr for logging,
and raises `FFmpegError` on non-zero exit codes so callers can handle
conversion failures without crashing the pipeline.

---

## Quality settings summary

| Conversion         | Default quality              | How to change                                 |
|--------------------|------------------------------|-----------------------------------------------|
| HEIC → JPEG        | quality=85                   | Pass `quality=` to `convert_heic.convert()`  |
| MOV → MP4 (remux)  | Lossless                     | N/A — stream copy                             |
| MOV → MP4 (transcode) | CRF 23 (libx264)          | Pass `crf=` to `transcode_to_mp4()`           |
| Audio transcode    | 192k bitrate                 | Pass `bitrate=` to `convert_audio.convert()` |
| Ringtone → M4R     | AAC 128k, trimmed to 30s     | Pass `trim_to_30s=False` to keep full length  |
| Ringtone → MP3     | libmp3lame 192k              | Not configurable (hardcoded for compatibility)|
