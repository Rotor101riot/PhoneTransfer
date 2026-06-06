"""
test_tier1.py

Validation suite for all Tier 1 modules.
Run from the PhoneTransfer root directory:

    python tests/test_tier1.py

No device, no FFmpeg binary, and no pillow-heif are required to get a
passing run. Tests that need those tools are clearly skipped with a reason.
"""

import shutil
import sys
import tempfile
from pathlib import Path

# Force UTF-8 output on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Minimal test harness ──────────────────────────────────────────────────────
_results = {"passed": 0, "failed": 0, "skipped": 0}


def _run(label: str, fn):
    try:
        fn()
        print(f"  [PASS] {label}")
        _results["passed"] += 1
    except AssertionError as exc:
        print(f"  [FAIL] {label}: {exc}")
        _results["failed"] += 1
    except Exception as exc:
        print(f"  [FAIL] {label}: {type(exc).__name__}: {exc}")
        _results["failed"] += 1


def _skip(label: str, reason: str):
    print(f"  [SKIP] {label}: {reason}")
    _results["skipped"] += 1


def _section(name: str):
    print(f"\n{'─' * 60}")
    print(f" {name}")
    print(f"{'─' * 60}")


# ── Detect optional capabilities ──────────────────────────────────────────────
import shutil as _sh  # noqa: E402

FFMPEG_AVAILABLE  = bool(_sh.which("ffmpeg"))
FFPROBE_AVAILABLE = bool(_sh.which("ffprobe"))
try:
    import pillow_heif as _ph  # noqa: F401
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════════
# normalization_schema
# ══════════════════════════════════════════════════════════════════════════════
_section("normalization_schema")

def _schema_imports():
    pass
_run("All 13 schema classes importable", _schema_imports)

def _schema_defaults():
    from core.normalization_schema import Contact, Alarm, MessageAttachment
    c = Contact(first_name="Ada", last_name="Lovelace")
    assert c.phones == [], "phones should default to empty list"
    assert c.emails == [], "emails should default to empty list"
    assert c.organization is None

    alarm = Alarm(hour=6, minute=30)
    assert alarm.enabled is True
    assert alarm.repeat_days == []

    att = MessageAttachment(filename="img.jpg", mime_type="image/jpeg")
    assert att.data is None
    assert att.local_path is None
_run("Dataclass defaults are correct", _schema_defaults)

def _schema_field_assignment():
    from core.normalization_schema import Contact, MessageAttachment
    c = Contact(
        first_name="Test",
        phones=["+1555000"],
    )
    assert c.phones[0] == "+1555000"

    att = MessageAttachment(filename="audio.ogg", mime_type="audio/ogg")
    assert att.filename == "audio.ogg"
    assert att.mime_type == "audio/ogg"
_run("Field assignment and nested objects work correctly", _schema_field_assignment)

def _schema_no_shared_mutable_defaults():
    from core.normalization_schema import Contact
    a = Contact()
    b = Contact()
    a.phones.append("+1")
    assert b.phones == [], "Mutable default shared between instances (dataclass field() bug)"
_run("Mutable list defaults are not shared between instances", _schema_no_shared_mutable_defaults)


# ══════════════════════════════════════════════════════════════════════════════
# config_loader
# ══════════════════════════════════════════════════════════════════════════════
_section("config_loader")

def _config_importable():
    from core.config_loader import get_config, Config
    assert callable(get_config)
    assert isinstance(Config, type)
_run("config_loader: get_config and Config are importable", _config_importable)

def _config_returns_config_instance():
    from core.config_loader import get_config, Config
    try:
        cfg = get_config()
    except FileNotFoundError as exc:
        raise AssertionError(
            f"Binary bundle missing — install adb/ffmpeg/libimobiledevice first: {exc}"
        ) from exc
    assert isinstance(cfg, Config)
    assert hasattr(cfg, "adb_exe")
    assert hasattr(cfg, "ffmpeg_exe")
    assert hasattr(cfg, "project_root")
    assert hasattr(cfg, "temp_dir")
    assert cfg.temp_dir.is_dir(), "get_config() should create temp_dir"

def _config_singleton():
    from core.config_loader import get_config
    try:
        c1 = get_config()
        c2 = get_config()
    except FileNotFoundError as exc:
        raise AssertionError(f"Binary bundle missing: {exc}") from exc
    assert c1 is c2, "get_config() must return same instance (lru_cache)"

# Only run live config tests when the binary bundle is present
try:
    from core.config_loader import get_config as _gc_probe
    _gc_probe()
    _CONFIG_AVAILABLE = True
except Exception:
    _CONFIG_AVAILABLE = False

if _CONFIG_AVAILABLE:
    _run("get_config() returns Config with expected attributes", _config_returns_config_instance)
    _run("get_config() is a singleton (lru_cache)", _config_singleton)
else:
    _skip("get_config() returns Config with expected attributes",
          "Binary bundle (adb / ffmpeg / libimobiledevice) not installed")
    _skip("get_config() is a singleton (lru_cache)",
          "Binary bundle not installed")


# ══════════════════════════════════════════════════════════════════════════════
# transfer_logger
# ══════════════════════════════════════════════════════════════════════════════
_section("transfer_logger")

def _logger_creates_file_and_writes():
    import logging
    import core.transfer_logger as tl
    tl.reset()
    tmp = tempfile.mkdtemp()
    try:
        logger = tl.get_logger(log_dir=tmp)
        assert isinstance(logger, logging.Logger)
        logger.info("Tier 1 test log entry")
        # Flush handlers
        for h in logger.handlers:
            h.flush()
        log_files = list(Path(tmp).glob("transfer_*.log"))
        assert len(log_files) == 1, "Expected exactly one log file"
        content = log_files[0].read_text(encoding="utf-8")
        assert "Tier 1 test log entry" in content
    finally:
        tl.reset()
        shutil.rmtree(tmp)
_run("Logger creates timestamped file and writes entries", _logger_creates_file_and_writes)

def _logger_singleton():
    import core.transfer_logger as tl
    tl.reset()
    tmp = tempfile.mkdtemp()
    try:
        l1 = tl.get_logger(log_dir=tmp)
        l2 = tl.get_logger(log_dir=tmp)
        assert l1 is l2, "get_logger() must return the same instance"
    finally:
        tl.reset()
        shutil.rmtree(tmp)
_run("get_logger() returns same instance on repeated calls (singleton)", _logger_singleton)

def _logger_reset_allows_new_instance():
    import time
    import core.transfer_logger as tl
    tl.reset()
    tmp = tempfile.mkdtemp()
    try:
        tl.get_logger(log_dir=tmp)
        file1 = tl.get_log_file()
        tl.reset()
        time.sleep(1.1)  # log filenames use second-resolution timestamps
        tl.get_logger(log_dir=tmp)
        file2 = tl.get_log_file()
        assert file1 != file2, "reset() should produce a fresh log file on next call"
    finally:
        tl.reset()
        shutil.rmtree(tmp)
_run("reset() causes next get_logger() to open a fresh log file", _logger_reset_allows_new_instance)

def _logger_get_log_file_path():
    import core.transfer_logger as tl
    tl.reset()
    tmp = tempfile.mkdtemp()
    try:
        tl.get_logger(log_dir=tmp)
        p = tl.get_log_file()
        assert p is not None
        assert p.suffix == ".log"
    finally:
        tl.reset()
        shutil.rmtree(tmp)
_run("get_log_file() returns Path to the session log", _logger_get_log_file_path)


# ══════════════════════════════════════════════════════════════════════════════
# progress_reporter
# ══════════════════════════════════════════════════════════════════════════════
_section("progress_reporter")

def _progress_basic_counting():
    from core.progress_reporter import ProgressReporter
    p = ProgressReporter()
    p.set_total("contacts", 100)
    for _ in range(60):
        p.increment("contacts")
    assert p.get_percentage("contacts") == 60.0
    p.increment("contacts", failed=True)
    state = p.get("contacts")
    assert state["completed"] == 60
    assert state["failed"] == 1
_run("Tracks completed/failed counts and percentage", _progress_basic_counting)

def _progress_bulk_increment():
    from core.progress_reporter import ProgressReporter
    p = ProgressReporter()
    p.set_total("photos", 500)
    p.increment("photos", count=250)
    assert p.get_percentage("photos") == 50.0
_run("increment(count=N) adds N in one call", _progress_bulk_increment)

def _progress_status_transitions():
    from core.progress_reporter import ProgressReporter, STATUS_RUNNING, STATUS_DONE, STATUS_ERROR, STATUS_SKIPPED
    p = ProgressReporter()
    p.set_total("sms", 10)
    assert p.get("sms")["status"] == STATUS_RUNNING
    p.mark_done("sms")
    assert p.get("sms")["status"] == STATUS_DONE
    p.mark_error("photos")
    assert p.get("photos")["status"] == STATUS_ERROR
    p.mark_skipped("health")
    assert p.get("health")["status"] == STATUS_SKIPPED
_run("Status transitions: pending→running→done/error/skipped", _progress_status_transitions)

def _progress_zero_total_returns_zero():
    from core.progress_reporter import ProgressReporter
    p = ProgressReporter()
    assert p.get_percentage("music") == 0.0
_run("get_percentage() returns 0.0 when total not set", _progress_zero_total_returns_zero)

def _progress_callback_fired():
    from core.progress_reporter import ProgressReporter
    events = []
    p = ProgressReporter(on_update=lambda state: events.append(len(state)))
    p.set_total("videos", 5)
    p.increment("videos")
    assert len(events) == 2  # one per mutation
_run("on_update callback fires on every state change", _progress_callback_fired)

def _progress_get_all_keys():
    from core.progress_reporter import ProgressReporter, CATEGORIES
    p = ProgressReporter()
    all_state = p.get_all()
    for cat in CATEGORIES:
        assert cat in all_state, f"Category '{cat}' missing from get_all()"
_run("get_all() returns entries for every known category", _progress_get_all_keys)

def _progress_thread_safety():
    import threading
    from core.progress_reporter import ProgressReporter
    p = ProgressReporter()
    p.set_total("photos", 1000)

    def worker():
        for _ in range(100):
            p.increment("photos")

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert p.get("photos")["completed"] == 1000
_run("Thread-safe: 10 threads × 100 increments = 1000 completed", _progress_thread_safety)


# ══════════════════════════════════════════════════════════════════════════════
# session_file
# ══════════════════════════════════════════════════════════════════════════════
_section("session_file")

def _session_create_and_load():
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        session = sf.create(tmp, "ios", "android", "SRC001", "DST002", ["contacts", "sms"])
        assert session["source_platform"] == "ios"
        assert session["dest_platform"] == "android"
        assert "contacts" in session["categories"]
        assert session["categories"]["contacts"]["status"] == "pending"

        loaded = sf.load(tmp)
        assert loaded is not None
        assert loaded["session_id"] == session["session_id"]
    finally:
        shutil.rmtree(tmp)
_run("create() and load() produce consistent session data", _session_create_and_load)

def _session_mark_category_complete():
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        sf.create(tmp, "android", "ios", "A", "B", ["photos"])
        sf.mark_category_complete(tmp, "photos", extracted=200, injected=198, failed=2)
        s = sf.load(tmp)
        cat = s["categories"]["photos"]
        assert cat["status"]          == "completed"
        assert cat["extracted_count"] == 200
        assert cat["injected_count"]  == 198
        assert cat["failed_count"]    == 2
    finally:
        shutil.rmtree(tmp)
_run("mark_category_complete() persists all counts", _session_mark_category_complete)

def _session_mark_category_failed():
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        sf.create(tmp, "ios", "ios", "A", "B", ["health"])
        sf.mark_category_failed(tmp, "health", error="HealthKit not authorised")
        s = sf.load(tmp)
        assert s["categories"]["health"]["status"] == "failed"
        assert "HealthKit" in s["categories"]["health"]["error"]
    finally:
        shutil.rmtree(tmp)
_run("mark_category_failed() stores error message", _session_mark_category_failed)

def _session_resumable_flag():
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        sf.create(tmp, "ios", "android", "A", "B", ["sms"])
        assert sf.is_resumable(tmp) is True
        sf.mark_complete(tmp)
        assert sf.is_resumable(tmp) is False
    finally:
        shutil.rmtree(tmp)
_run("is_resumable() reflects session completion state", _session_resumable_flag)

def _session_aborted_not_resumable():
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        sf.create(tmp, "android", "android", "A", "B", ["contacts"])
        sf.mark_aborted(tmp)
        assert sf.is_resumable(tmp) is False
        s = sf.load(tmp)
        assert s["aborted"] is True
    finally:
        shutil.rmtree(tmp)
_run("mark_aborted() marks session as non-resumable", _session_aborted_not_resumable)

def _session_missing_file():
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        assert sf.load(tmp)    is None
        assert sf.exists(tmp)  is False
        assert sf.is_resumable(tmp) is False
    finally:
        shutil.rmtree(tmp)
_run("Missing session file handled gracefully", _session_missing_file)

def _session_pending_categories():
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        sf.create(tmp, "ios", "android", "A", "B", ["contacts", "photos", "sms"])
        sf.mark_category_complete(tmp, "contacts", 10, 10, 0)
        pending = sf.pending_categories(tmp)
        assert "contacts" not in pending
        assert "photos"   in pending
        assert "sms"      in pending
    finally:
        shutil.rmtree(tmp)
_run("pending_categories() excludes completed categories", _session_pending_categories)

def _session_updated_at_changes():
    import time
    import core.session_file as sf
    tmp = tempfile.mkdtemp()
    try:
        sf.create(tmp, "ios", "android", "A", "B", ["contacts"])
        s1 = sf.load(tmp)
        time.sleep(0.05)
        sf.mark_category_complete(tmp, "contacts", 1, 1, 0)
        s2 = sf.load(tmp)
        assert s2["updated_at"] > s1["updated_at"]
    finally:
        shutil.rmtree(tmp)
_run("updated_at timestamp advances on every write", _session_updated_at_changes)


# ══════════════════════════════════════════════════════════════════════════════
# ffmpeg_wrapper
# ══════════════════════════════════════════════════════════════════════════════
_section("ffmpeg_wrapper")

def _ffmpeg_not_found_error():
    import core.ffmpeg_wrapper as fw
    original = fw._BUNDLED
    fw._BUNDLED = Path("/nonexistent/ffmpeg.exe")
    saved_which = shutil.which

    def no_ffmpeg(name, **kw):
        return None if name == "ffmpeg" else saved_which(name, **kw)

    shutil.which = no_ffmpeg
    try:
        try:
            fw.find_ffmpeg()
            assert False, "Should have raised FFmpegNotFoundError"
        except fw.FFmpegNotFoundError:
            pass
    finally:
        fw._BUNDLED   = original
        shutil.which  = saved_which
_run("FFmpegNotFoundError raised when binary absent", _ffmpeg_not_found_error)

def _ffmpeg_error_on_bad_invocation():
    if not FFMPEG_AVAILABLE:
        _skip("FFmpegError on bad invocation", "FFmpeg not on PATH")
        return
    import core.ffmpeg_wrapper as fw
    try:
        fw.run(["-i", "definitely_nonexistent_file_xyz123.mp4", "out.mp4"])
        assert False, "Should have raised FFmpegError"
    except fw.FFmpegError:
        pass
_run("FFmpegError raised on bad FFmpeg invocation", _ffmpeg_error_on_bad_invocation)

if FFMPEG_AVAILABLE:
    def _ffmpeg_version_string():
        import core.ffmpeg_wrapper as fw
        v = fw.version()
        assert "ffmpeg" in v.lower()
    _run("version() returns non-empty version string", _ffmpeg_version_string)
else:
    _skip("version() returns non-empty string", "FFmpeg not on PATH")


# ══════════════════════════════════════════════════════════════════════════════
# convert_audio
# ══════════════════════════════════════════════════════════════════════════════
_section("convert_audio")

def _audio_missing_input():
    from convert.convert_audio import convert
    try:
        convert("ghost_file.ogg", "out.mp3")
        assert False
    except FileNotFoundError:
        pass
_run("FileNotFoundError on missing input file", _audio_missing_input)

def _audio_bad_input_format():
    from convert.convert_audio import convert
    tmp = Path(tempfile.mkdtemp())
    try:
        bad = tmp / "test.xyz"
        bad.write_bytes(b"fake")
        try:
            convert(str(bad), str(tmp / "out.mp3"))
            assert False
        except ValueError as exc:
            assert "Unsupported input" in str(exc)
    finally:
        shutil.rmtree(str(tmp))
_run("ValueError on unsupported input extension", _audio_bad_input_format)

def _audio_bad_output_format():
    from convert.convert_audio import convert
    tmp = Path(tempfile.mkdtemp())
    try:
        good_input = tmp / "test.ogg"
        good_input.write_bytes(b"fake ogg")
        try:
            convert(str(good_input), str(tmp / "out.xyz"))
            assert False
        except ValueError as exc:
            assert "Unsupported output" in str(exc)
    finally:
        shutil.rmtree(str(tmp))
_run("ValueError on unsupported output extension", _audio_bad_output_format)


# ══════════════════════════════════════════════════════════════════════════════
# convert_video
# ══════════════════════════════════════════════════════════════════════════════
_section("convert_video")

def _video_missing_input_remux():
    from convert.convert_video import remux_to_mp4
    try:
        remux_to_mp4("ghost.mov", "out.mp4")
        assert False
    except FileNotFoundError:
        pass
_run("remux_to_mp4() FileNotFoundError on missing input", _video_missing_input_remux)

def _video_missing_input_transcode():
    from convert.convert_video import transcode_to_mp4
    try:
        transcode_to_mp4("ghost.mov", "out.mp4")
        assert False
    except FileNotFoundError:
        pass
_run("transcode_to_mp4() FileNotFoundError on missing input", _video_missing_input_transcode)

def _video_output_forced_mp4():
    from convert.convert_video import remux_to_mp4
    import inspect
    src = inspect.getsource(remux_to_mp4)
    assert '.with_suffix(".mp4")' in src
_run("remux_to_mp4() forces .mp4 extension on output", _video_output_forced_mp4)


# ══════════════════════════════════════════════════════════════════════════════
# convert_ringtones
# ══════════════════════════════════════════════════════════════════════════════
_section("convert_ringtones")

def _ringtone_to_m4r_missing():
    from convert.convert_ringtones import to_m4r
    try:
        to_m4r("ghost.mp3", "out.m4r")
        assert False
    except FileNotFoundError:
        pass
_run("to_m4r() FileNotFoundError on missing input", _ringtone_to_m4r_missing)

def _ringtone_to_mp3_missing():
    from convert.convert_ringtones import to_mp3
    try:
        to_mp3("ghost.m4r", "out.mp3")
        assert False
    except FileNotFoundError:
        pass
_run("to_mp3() FileNotFoundError on missing input", _ringtone_to_mp3_missing)

def _ringtone_to_m4a_missing():
    from convert.convert_ringtones import to_m4a
    try:
        to_m4a("ghost.m4r", "out.m4a")
        assert False
    except FileNotFoundError:
        pass
_run("to_m4a() FileNotFoundError on missing input", _ringtone_to_m4a_missing)

def _ringtone_m4r_extension_enforced():
    import inspect
    from convert.convert_ringtones import to_m4r
    src = inspect.getsource(to_m4r)
    assert '.with_suffix(".m4r")' in src, ".m4r extension not enforced in source"
_run("to_m4r() forces .m4r extension on output path", _ringtone_m4r_extension_enforced)

def _ringtone_trim_flag_in_args():
    import inspect
    from convert.convert_ringtones import to_m4r, IOS_MAX_SECONDS as _MAX
    src = inspect.getsource(to_m4r)
    assert str(_MAX) in src, f"IOS_MAX_SECONDS ({_MAX}) not referenced in to_m4r source"
_run("to_m4r() references IOS_MAX_SECONDS (30s) in its FFmpeg args", _ringtone_trim_flag_in_args)


# ══════════════════════════════════════════════════════════════════════════════
# convert_heic
# ══════════════════════════════════════════════════════════════════════════════
_section("convert_heic")

def _heic_missing_input():
    from convert.convert_heic import convert
    try:
        convert("ghost.heic", "out.jpg")
        assert False
    except (FileNotFoundError, ImportError):
        pass  # FileNotFoundError = correct; ImportError = pillow-heif missing (also acceptable)
_run("convert() raises FileNotFoundError or ImportError appropriately", _heic_missing_input)

if HEIF_AVAILABLE:
    def _heic_batch_empty_dir():
        from convert.convert_heic import convert_batch
        tmp = Path(tempfile.mkdtemp())
        try:
            results = convert_batch(str(tmp), str(tmp / "out"))
            assert results == [], f"Expected [], got {results}"
        finally:
            shutil.rmtree(str(tmp))
    _run("convert_batch() returns [] for a dir with no HEIC files", _heic_batch_empty_dir)

    def _heic_batch_creates_output_dir():
        from convert.convert_heic import convert_batch
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "converted"
        try:
            convert_batch(str(tmp), str(out))
            assert out.exists(), "Output dir should be created even when empty"
        finally:
            shutil.rmtree(str(tmp))
    _run("convert_batch() creates output directory even when no files present", _heic_batch_creates_output_dir)
else:
    _skip("convert_batch() empty dir test", "pillow-heif not installed — run: pip install pillow-heif")
    _skip("convert_batch() creates output dir test", "pillow-heif not installed — run: pip install pillow-heif")


# ══════════════════════════════════════════════════════════════════════════════
# pii_filter
# ══════════════════════════════════════════════════════════════════════════════
_section("pii_filter")

def _pii_redact_phone_basic():
    from core.pii_filter import redact_phone
    result = redact_phone("Call me at 555-867-5309 please")
    assert "[PHONE]" in result, f"Expected [PHONE] in: {result!r}"
    assert "555-867-5309" not in result
_run("redact_phone() replaces a NANP number", _pii_redact_phone_basic)

def _pii_redact_phone_e164():
    from core.pii_filter import redact_phone
    result = redact_phone("+14155551234 is the number")
    assert "[PHONE]" in result, f"Expected [PHONE] in: {result!r}"
    assert "+14155551234" not in result
_run("redact_phone() replaces an E.164 number", _pii_redact_phone_e164)

def _pii_redact_phone_no_false_positive():
    from core.pii_filter import redact_phone
    result = redact_phone("version 1.2.3 code 42")
    assert "[PHONE]" not in result, f"Unexpected [PHONE] in: {result!r}"
_run("redact_phone() leaves short numeric IDs alone", _pii_redact_phone_no_false_positive)

def _pii_redact_email_basic():
    from core.pii_filter import redact_email
    result = redact_email("contact user@example.com for info")
    assert "[EMAIL]" in result, f"Expected [EMAIL] in: {result!r}"
    assert "user@example.com" not in result
_run("redact_email() replaces a plain email address", _pii_redact_email_basic)

def _pii_redact_email_no_match():
    from core.pii_filter import redact_email
    result = redact_email("no emails here, just text")
    assert "[EMAIL]" not in result
_run("redact_email() leaves plain text untouched", _pii_redact_email_no_match)

def _pii_redact_combined():
    from core.pii_filter import redact
    s = "Phone: +12025551234, email: foo@bar.org"
    result = redact(s)
    assert "[PHONE]" in result
    assert "[EMAIL]" in result
    assert "+12025551234" not in result
    assert "foo@bar.org" not in result
_run("redact() applies both phone and email redaction", _pii_redact_combined)

def _pii_filter_mutates_record():
    import logging
    from core.pii_filter import PiiRedactFilter
    f = PiiRedactFilter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="user@test.com called 555-123-4567",
        args=(), exc_info=None,
    )
    f.filter(record)
    assert "[EMAIL]" in str(record.msg), f"Email not redacted: {record.msg!r}"
    assert "[PHONE]" in str(record.msg), f"Phone not redacted: {record.msg!r}"
_run("PiiRedactFilter.filter() redacts msg in-place", _pii_filter_mutates_record)

def _pii_filter_returns_true():
    import logging
    from core.pii_filter import PiiRedactFilter
    f = PiiRedactFilter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    assert f.filter(record) is True
_run("PiiRedactFilter.filter() always returns True (never drops records)", _pii_filter_returns_true)


# ══════════════════════════════════════════════════════════════════════════════
# content_dedup
# ══════════════════════════════════════════════════════════════════════════════
_section("content_dedup")

def _dedup_empty_store_no_filter():
    import tempfile
    from pathlib import Path
    from core.content_dedup import DedupStore
    from core.normalization_schema import Contact
    tmp = Path(tempfile.mkdtemp())
    store = DedupStore("SRC", "DST", data_dir=tmp)
    c = Contact(first_name="Alice", last_name="Smith", phones=["+1234567890"])
    result = store.filter_duplicates("contacts", [c])
    assert result == [c], "Empty store should pass all items through"
    shutil.rmtree(str(tmp))
_run("filter_duplicates() passes all items when store is empty", _dedup_empty_store_no_filter)

def _dedup_mark_then_filter():
    import tempfile
    from pathlib import Path
    from core.content_dedup import DedupStore
    from core.normalization_schema import Contact
    tmp = Path(tempfile.mkdtemp())
    store = DedupStore("SRC", "DST", data_dir=tmp)
    c = Contact(first_name="Bob", last_name="Jones", phones=["+9876543210"])
    store.filter_duplicates("contacts", [c])
    store.mark_transferred("contacts", [c])
    store.save()
    # Reload from disk to verify persistence
    store2 = DedupStore("SRC", "DST", data_dir=tmp)
    result = store2.filter_duplicates("contacts", [c])
    assert result == [], f"Transferred item should be filtered as duplicate; got {result}"
    shutil.rmtree(str(tmp))
_run("filter_duplicates() removes items marked as transferred (persisted)", _dedup_mark_then_filter)

def _dedup_schema_v0_migration():
    import tempfile
    import json
    import hashlib
    from pathlib import Path
    from core.content_dedup import DedupStore
    from core.normalization_schema import Contact
    tmp = Path(tempfile.mkdtemp())
    dedup_dir = tmp / "dedup"
    dedup_dir.mkdir()
    c = Contact(first_name="Carol", last_name="White", phones=["+1112223333"])
    # Compute fingerprint the same way the store does for contacts
    keys = ["first_name", "last_name", "phones", "emails", "organization"]
    data = {}
    for k in keys:
        val = getattr(c, k, None)
        data[k] = val if val is not None else ""
    fp = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    # Write a v0 flat file (no _schema_version key)
    v0_payload = {"contacts": {fp: "2025-01-01T00:00:00"}}
    (dedup_dir / "SRC_to_DST.json").write_text(json.dumps(v0_payload), encoding="utf-8")
    store = DedupStore("SRC", "DST", data_dir=tmp)
    result = store.filter_duplicates("contacts", [c])
    assert result == [], f"v0 fingerprint should survive migration; got {result}"
    shutil.rmtree(str(tmp))
_run("DedupStore loads v0 (flat) file and treats existing fingerprints as known", _dedup_schema_v0_migration)

def _dedup_schema_future_version_resets():
    import tempfile
    import json
    from pathlib import Path
    from core.content_dedup import DedupStore, _SCHEMA_VERSION
    tmp = Path(tempfile.mkdtemp())
    dedup_dir = tmp / "dedup"
    dedup_dir.mkdir()
    future_payload = {"_schema_version": _SCHEMA_VERSION + 99, "categories": {"contacts": {"deadbeef": "2025-01-01"}}}
    (dedup_dir / "SRC_to_DST.json").write_text(json.dumps(future_payload), encoding="utf-8")
    store = DedupStore("SRC", "DST", data_dir=tmp)
    assert store.stats == {}, f"Future-version store should reset to empty; got {store.stats}"
    shutil.rmtree(str(tmp))
_run("DedupStore resets on future schema version rather than reading corrupt data", _dedup_schema_future_version_resets)

def _dedup_save_writes_versioned_envelope():
    import tempfile
    import json
    from pathlib import Path
    from core.content_dedup import DedupStore, _SCHEMA_VERSION
    from core.normalization_schema import Contact
    tmp = Path(tempfile.mkdtemp())
    store = DedupStore("SRC", "DST", data_dir=tmp)
    c = Contact(first_name="Dave", phones=["+5556667777"])
    store.filter_duplicates("contacts", [c])
    store.mark_transferred("contacts", [c])
    store.save()
    raw = json.loads((tmp / "dedup" / "SRC_to_DST.json").read_text(encoding="utf-8"))
    assert raw.get("_schema_version") == _SCHEMA_VERSION, "Saved file missing correct _schema_version"
    assert "categories" in raw
    shutil.rmtree(str(tmp))
_run("DedupStore.save() writes versioned envelope with _schema_version", _dedup_save_writes_versioned_envelope)


# ══════════════════════════════════════════════════════════════════════════════
# quirk_detector
# ══════════════════════════════════════════════════════════════════════════════
_section("quirk_detector")

def _make_dev(platform, model, os_version, brand="", is_jailbroken=False, is_rooted=False):
    from core.normalization_schema import DeviceInfo
    return DeviceInfo(
        udid="test-udid", platform=platform, model=model,
        name="Test Device", os_version=os_version,
        is_jailbroken=is_jailbroken, is_rooted=is_rooted,
        serial="test-serial", brand=brand,
    )

def _quirk_no_match_different_platform():
    from core.quirk_detector import detect_quirks
    src = _make_dev("android", "SM-G991B", "13", brand="Samsung")
    dst = _make_dev("android", "SM-A525F", "12", brand="Samsung")
    pairs = detect_quirks(src, dst)
    ios_quirks = [(q, r) for q, r in pairs if "ios" in q.id.lower()]
    assert ios_quirks == [], f"Android devices should not match iOS quirks; got {ios_quirks}"
_run("detect_quirks() does not match iOS quirks against Android devices", _quirk_no_match_different_platform)

def _quirk_returns_list():
    from core.quirk_detector import detect_quirks
    src = _make_dev("ios", "iPhone15,2", "17.5", brand="Apple")
    dst = _make_dev("android", "Pixel 7", "14", brand="Google")
    result = detect_quirks(src, dst)
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    for item in result:
        assert isinstance(item, tuple) and len(item) == 2, f"Expected (Quirk, role) tuple: {item}"
_run("detect_quirks() always returns a list of (Quirk, role) tuples", _quirk_returns_list)

def _quirk_os_version_min_filters_old():
    from core.quirk_detector import _matches
    entry = {"match": {"platform": "ios", "os_version_min": "18.0"}}
    dev_old = _make_dev("ios", "iPhone14,2", "17.5", brand="Apple")
    dev_new = _make_dev("ios", "iPhone16,1", "18.1", brand="Apple")
    assert not _matches(entry, dev_old), "iOS 17.5 should not match os_version_min=18.0"
    assert _matches(entry, dev_new),     "iOS 18.1 should match os_version_min=18.0"
_run("_matches() respects os_version_min boundary correctly", _quirk_os_version_min_filters_old)

def _quirk_os_version_max_filters_new():
    from core.quirk_detector import _matches
    entry = {"match": {"platform": "ios", "os_version_max": "16.99"}}
    dev_old = _make_dev("ios", "iPhone13,2", "16.5", brand="Apple")
    dev_new = _make_dev("ios", "iPhone15,2", "17.0", brand="Apple")
    assert _matches(entry, dev_old),     "iOS 16.5 should match os_version_max=16.99"
    assert not _matches(entry, dev_new), "iOS 17.0 should not match os_version_max=16.99"
_run("_matches() respects os_version_max boundary correctly", _quirk_os_version_max_filters_new)

def _quirk_brand_contains():
    from core.quirk_detector import _matches
    entry = {"match": {"platform": "android", "brand_contains": ["xiaomi", "miui"]}}
    xiaomi_dev = _make_dev("android", "2203121C", "13", brand="Xiaomi")
    samsung_dev = _make_dev("android", "SM-G991B", "13", brand="Samsung")
    assert _matches(entry, xiaomi_dev),      "Xiaomi brand should match brand_contains=['xiaomi','miui']"
    assert not _matches(entry, samsung_dev), "Samsung should not match xiaomi/miui brand_contains"
_run("_matches() brand_contains is case-insensitive", _quirk_brand_contains)

def _quirk_never_raises():
    from core.quirk_detector import detect_quirks
    src = _make_dev("ios", "", "", brand="Apple")
    dst = _make_dev("android", "", "", brand="")
    try:
        result = detect_quirks(src, dst)
        assert isinstance(result, list)
    except Exception as exc:
        raise AssertionError(f"detect_quirks() raised unexpectedly: {exc}")
_run("detect_quirks() never raises — returns empty list on bad input", _quirk_never_raises)


# ══════════════════════════════════════════════════════════════════════════════
# convert_imessage_to_mms (SMS/MMS fidelity — #49)
# ══════════════════════════════════════════════════════════════════════════════
_section("convert_imessage_to_mms — SMS/MMS fidelity")

def _mms_short_text_becomes_sms():
    from convert.convert_imessage_to_mms import imessage_to_mms
    from core.normalization_schema import Message
    msg = Message(
        platform_id="1", sender="15555550100", recipient="15555550200",
        body="Hello!", timestamp=0, is_sent=True, attachments=[],
        service="imessage", read=True,
    )
    out = imessage_to_mms(msg)
    assert out.service == "sms", f"Short iMessage with no attachment should be sms, got {out.service!r}"
_run("imessage_to_mms(): plain short text → service='sms'", _mms_short_text_becomes_sms)

def _mms_long_text_becomes_mms():
    from convert.convert_imessage_to_mms import imessage_to_mms
    from core.normalization_schema import Message
    long_body = "A" * 161
    msg = Message(
        platform_id="2", sender="15555550100", recipient="15555550200",
        body=long_body, timestamp=0, is_sent=True, attachments=[],
        service="imessage", read=True,
    )
    out = imessage_to_mms(msg)
    assert out.service == "mms", f"Long body should be mms, got {out.service!r}"
_run("imessage_to_mms(): body > 160 bytes → service='mms'", _mms_long_text_becomes_mms)

def _mms_attachment_becomes_mms():
    from convert.convert_imessage_to_mms import imessage_to_mms
    from core.normalization_schema import Message, MessageAttachment
    att = MessageAttachment(filename="photo.jpg", mime_type="image/jpeg", local_path=None)
    msg = Message(
        platform_id="3", sender="15555550100", recipient="15555550200",
        body="See photo", timestamp=0, is_sent=True, attachments=[att],
        service="imessage", read=True,
    )
    out = imessage_to_mms(msg)
    assert out.service == "mms", f"Message with attachment should be mms, got {out.service!r}"
    assert len(out.attachments) == 1
_run("imessage_to_mms(): message with attachment → service='mms'", _mms_attachment_becomes_mms)

def _mms_non_imessage_passthrough():
    from convert.convert_imessage_to_mms import convert_batch
    from core.normalization_schema import Message
    sms_msg = Message(
        platform_id="4", sender="15555550100", recipient="15555550200",
        body="Regular SMS", timestamp=0, is_sent=True, attachments=[],
        service="sms", read=True,
    )
    result = convert_batch([sms_msg])
    assert len(result) == 1
    assert result[0].service == "sms", "Non-iMessage must pass through unchanged"
    assert result[0] is sms_msg, "Non-iMessage object identity must be preserved"
_run("convert_batch(): non-iMessage messages pass through unchanged", _mms_non_imessage_passthrough)

def _mms_sender_e164_normalization():
    from convert.convert_imessage_to_mms import imessage_to_mms
    from core.normalization_schema import Message
    msg = Message(
        platform_id="5", sender="5555550100", recipient="5555550200",
        body="Hi", timestamp=0, is_sent=True, attachments=[],
        service="imessage", read=True,
    )
    out = imessage_to_mms(msg)
    assert out.sender.startswith("+"), f"10-digit sender should get + prefix, got {out.sender!r}"
    assert out.recipient.startswith("+"), f"10-digit recipient should get + prefix, got {out.recipient!r}"
_run("imessage_to_mms(): bare 10-digit numbers get E.164 + prefix", _mms_sender_e164_normalization)

def _mms_already_e164_unchanged():
    from convert.convert_imessage_to_mms import imessage_to_mms
    from core.normalization_schema import Message
    msg = Message(
        platform_id="6", sender="+14155550100", recipient="+14155550200",
        body="Hey", timestamp=0, is_sent=True, attachments=[],
        service="imessage", read=True,
    )
    out = imessage_to_mms(msg)
    assert out.sender == "+14155550100"
    assert out.recipient == "+14155550200"
_run("imessage_to_mms(): already-E.164 numbers are not double-prefixed", _mms_already_e164_unchanged)

def _mms_does_not_mutate_original():
    from convert.convert_imessage_to_mms import imessage_to_mms
    from core.normalization_schema import Message
    msg = Message(
        platform_id="7", sender="5555550100", recipient="5555550200",
        body="Test", timestamp=0, is_sent=True, attachments=[],
        service="imessage", read=True,
    )
    imessage_to_mms(msg)
    assert msg.service == "imessage", "Original Message must not be mutated"
    assert msg.sender == "5555550100"
_run("imessage_to_mms(): original Message object is not mutated", _mms_does_not_mutate_original)

def _mms_group_by_thread_order():
    from convert.convert_imessage_to_mms import group_by_thread
    from core.normalization_schema import Message
    msgs = [
        Message(platform_id="a", sender="A", recipient="B", body="1", timestamp=200, is_sent=True, attachments=[], service="sms", read=True),
        Message(platform_id="b", sender="A", recipient="B", body="2", timestamp=100, is_sent=True, attachments=[], service="sms", read=True),
    ]
    threads = group_by_thread(msgs)
    assert len(threads) == 1
    thread = list(threads.values())[0]
    assert thread[0].timestamp == 100, "Messages within thread should be sorted ascending by timestamp"
    assert thread[1].timestamp == 200
_run("group_by_thread(): messages within a thread are sorted by timestamp", _mms_group_by_thread_order)


# ══════════════════════════════════════════════════════════════════════════════
# convert_heic — HEIC quality and output format (#26)
# ══════════════════════════════════════════════════════════════════════════════
_section("convert_heic — HEIC quality and output format")

def _heic_default_quality_is_85():
    import inspect
    from convert.convert_heic import convert
    sig = inspect.signature(convert)
    q = sig.parameters["quality"].default
    assert q == 85, f"Default JPEG quality should be 85 (fidelity/size balance), got {q}"
_run("convert() default quality parameter is 85", _heic_default_quality_is_85)

def _heic_batch_default_quality_is_85():
    import inspect
    from convert.convert_heic import convert_batch
    sig = inspect.signature(convert_batch)
    q = sig.parameters["quality"].default
    assert q == 85, f"convert_batch() default quality should be 85, got {q}"
_run("convert_batch() default quality parameter is 85", _heic_batch_default_quality_is_85)

def _heic_extensions_set_nonempty():
    from convert.convert_heic import HEIC_EXTENSIONS
    assert ".heic" in HEIC_EXTENSIONS, "HEIC_EXTENSIONS must include .heic"
    assert ".heif" in HEIC_EXTENSIONS, "HEIC_EXTENSIONS must include .heif"
_run("HEIC_EXTENSIONS includes both .heic and .heif", _heic_extensions_set_nonempty)

def _heic_batch_output_uses_jpg_extension():
    import inspect
    from convert.convert_heic import convert_batch
    src = inspect.getsource(convert_batch)
    assert '.jpg"' in src or "'.jpg'" in src or '".jpg"' in src, \
        "convert_batch() must produce .jpg output files"
_run("convert_batch() writes output files with .jpg extension", _heic_batch_output_uses_jpg_extension)

def _heic_convert_uses_optimize():
    import inspect
    from convert.convert_heic import convert
    src = inspect.getsource(convert)
    assert "optimize=True" in src, "convert() should pass optimize=True to Pillow for smaller file sizes"
_run("convert() passes optimize=True to Pillow JPEG encoder", _heic_convert_uses_optimize)

def _heic_quality_range_valid():
    from convert.convert_heic import convert
    import inspect
    sig = inspect.signature(convert)
    q = sig.parameters["quality"].default
    assert 1 <= q <= 95, f"Quality {q} is outside the valid 1-95 Pillow JPEG range"
_run("convert() default quality is in the valid Pillow JPEG range (1-95)", _heic_quality_range_valid)


# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═' * 60}")
print(
    f"  RESULTS:  {_results['passed']} passed  |  "
    f"{_results['failed']} failed  |  "
    f"{_results['skipped']} skipped"
)
print(f"{'═' * 60}\n")

if _results["failed"] > 0:
    sys.exit(1)
