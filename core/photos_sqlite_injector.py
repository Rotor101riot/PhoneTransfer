"""
photos_sqlite_injector.py

Injects asset records directly into the iOS Photos library database
(PhotoData/Photos.sqlite) so that transferred photos appear in the
Photos app immediately — without relying on medialibraryd re-scanning
the DCIM directory.

Strategy
--------
1. Pull Photos.sqlite (and -wal/-shm if present) from the device via AFC.
2. Open the local copy and insert the required CoreData rows for each file.
3. Checkpoint the WAL to consolidate all writes into the main DB file.
4. Push the updated Photos.sqlite back.
5. Clear the on-device WAL by overwriting it with an empty file.

Tables written
--------------
ZASSET                     — primary asset record
ZADDITIONALASSETATTRIBUTES — attributes; shares Z_PK with ZASSET
ZEXTENDEDATTRIBUTES        — EXIF / extended metadata
ZINTERNALRESOURCE          — the original-file resource reference
ZMOMENT                    — time-based grouping for the Photos UI
Z_PRIMARYKEY               — CoreData Z_MAX counters, kept in sync

Device paths (relative to AFC root /var/mobile/Media/)
-------------------------------------------------------
PhotoData/Photos.sqlite
PhotoData/Photos.sqlite-wal
PhotoData/Photos.sqlite-shm

Return value
------------
Number of asset records successfully written to the database.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.afc_connector import AFCConnector
from core.ios_service_broker import IOSServiceBroker
from core.ios_schema_guard import log_actual_schema, validate_photos_schema
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# AFC-relative paths (AFC root == /var/mobile/Media/)
_DB_PATH = "PhotoData/Photos.sqlite"
_WAL_PATH = "PhotoData/Photos.sqlite-wal"
_SHM_PATH = "PhotoData/Photos.sqlite-shm"

# CoreData entity IDs (from Z_PRIMARYKEY / Z_ENT columns)
_ENT_ADDITIONAL_ASSET_ATTRIBUTES = 1
_ENT_ASSET = 3
_ENT_EXTENDED_ATTRIBUTES = 28
_ENT_INTERNAL_RESOURCE = 51
_ENT_MOMENT = 58

# Apple CoreData epoch: 2001-01-01 00:00:00 UTC
_APPLE_EPOCH_OFFSET = 978_307_200  # seconds between Unix and Apple epochs

# ZSAVEDASSETTYPE: 3 = "imported" (same as iTunes/sync imports)
_SAVED_TYPE_IMPORTED = 3

# ZIMPORTEDBY: 6 = imported by a third-party app
_IMPORTED_BY_OTHER = 6

# ZORIGINATORSTATE for new moments
_MOMENT_ORIGINATOR_STATE = 9

# ---------------------------------------------------------------------------
# UTI / compact-UTI / subtype lookup tables
# ---------------------------------------------------------------------------

# Maps MIME type → iOS Uniform Type Identifier
_MIME_TO_UTI: dict[str, str] = {
    "image/jpeg": "public.jpeg",
    "image/jpg": "public.jpeg",
    "image/png": "public.png",
    "image/heic": "public.heic",
    "image/heif": "public.heic",
    "image/webp": "org.webmproject.webp",
    "image/gif": "com.compuserve.gif",
    "image/bmp": "com.microsoft.bmp",
    "image/tiff": "public.tiff",
    "video/quicktime": "com.apple.quicktime-movie",
    "video/mp4": "public.mpeg-4",
    "video/x-m4v": "public.m4v-video",
}

# Maps UTI → ZINTERNALRESOURCE.ZCOMPACTUTI (observed from live Photos.sqlite)
_UTI_TO_COMPACT: dict[str, str] = {
    "public.jpeg": "1",
    "public.png": "6",
    "public.heic": "3",
    "org.webmproject.webp": "_org.webmproject.webp",
    "com.compuserve.gif": "7",
    "com.apple.quicktime-movie": "1",
    "public.mpeg-4": "1",
    "public.m4v-video": "1",
}

# Maps UTI → ZINTERNALRESOURCE.ZDATASTORESUBTYPE
# 1 = still image, 4 = video (most common for MP4), 0 = MOV
_UTI_TO_SUBTYPE: dict[str, int] = {
    "public.jpeg": 1,
    "public.png": 1,
    "public.heic": 1,
    "org.webmproject.webp": 1,
    "com.compuserve.gif": 1,
    "com.apple.quicktime-movie": 0,
    "public.mpeg-4": 4,
    "public.m4v-video": 4,
}

# Maps UTI → ZINTERNALRESOURCE.ZDATASTOREKEYDATA (6-byte binary blob)
# Observed from a live Photos.sqlite — encodes data store class/type info.
_UTI_TO_KEYDATA: dict[str, bytes] = {
    "public.jpeg": bytes.fromhex("030000004001"),
    "public.png": bytes.fromhex("030000004003"),
    "public.heic": bytes.fromhex("030000004001"),
    "org.webmproject.webp": bytes.fromhex("030000004001"),
    "com.compuserve.gif": bytes.fromhex("030000004001"),
    "com.apple.quicktime-movie": bytes.fromhex("030000004001"),
    "public.mpeg-4": bytes.fromhex("030000004001"),
}

# ZASSET.ZKIND: 0 = photo, 1 = video
_UTI_TO_KIND: dict[str, int] = {
    "public.jpeg": 0,
    "public.png": 0,
    "public.heic": 0,
    "org.webmproject.webp": 0,
    "com.compuserve.gif": 0,
    "com.apple.quicktime-movie": 1,
    "public.mpeg-4": 1,
    "public.m4v-video": 1,
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inject_into_photos_db(
    broker: IOSServiceBroker,
    items: list[MediaFile],
    dcim_subfolder: str,
    pushed_filenames: list[str],
    staging_dir: Path,
    ios_version: str | None = None,
) -> int:
    """
    Pull Photos.sqlite, insert asset records for each pushed file, push back.

    Parameters
    ----------
    broker          : IOSServiceBroker connected to the target device.
    items           : MediaFile list (same order as pushed_filenames).
    dcim_subfolder  : Name of the DCIM subfolder used (e.g. '999PTRNS').
    pushed_filenames: Actual filenames as they were pushed to the device
                      (may differ from item.filename due to de-duplication).
    staging_dir     : Local temp directory for DB file copies.
    ios_version     : iOS version string (e.g. '17.2.1') for schema guard
                      context and log messages. Optional.

    Returns
    -------
    int: Number of asset rows successfully inserted.
    """
    if not items:
        return 0

    afc = AFCConnector(broker)

    # ── Pull Photos.sqlite from device ────────────────────────────────────
    local_db = staging_dir / "Photos_working.sqlite"
    local_wal = staging_dir / "Photos_working.sqlite-wal"
    local_shm = staging_dir / "Photos_working.sqlite-shm"

    logger.info("photos_db: pulling Photos.sqlite from device")
    if not afc.pull_file(_DB_PATH, local_db):
        logger.error("photos_db: could not pull Photos.sqlite — skipping DB injection")
        return 0

    # Pull WAL/SHM if present (best-effort; not fatal if absent)
    has_wal = afc.pull_file(_WAL_PATH, local_wal)
    has_shm = afc.pull_file(_SHM_PATH, local_shm)
    if has_wal:
        logger.debug("photos_db: WAL file pulled (%d bytes)", local_wal.stat().st_size)
    if has_shm:
        logger.debug("photos_db: SHM file pulled")

    # ── Modify local copy ─────────────────────────────────────────────────
    inserted = _modify_db(local_db, items, dcim_subfolder, pushed_filenames, ios_version)
    if inserted == 0:
        logger.warning("photos_db: no rows inserted — not pushing back")
        return 0

    # ── Push updated DB back to device ────────────────────────────────────
    logger.info("photos_db: pushing updated Photos.sqlite back to device")
    if not afc.push_file(local_db, _DB_PATH):
        logger.error("photos_db: failed to push Photos.sqlite back to device")
        return 0

    # Clear the WAL on the device so the daemon sees a clean DB
    empty = staging_dir / "empty.wal"
    empty.write_bytes(b"")
    if not afc.push_file(empty, _WAL_PATH):
        logger.warning("photos_db: failed to push empty WAL — DB may be corrupted on next read")
    # Remove the SHM — the daemon will recreate it
    # (push a zero-length file; AFC doesn't have a delete for existing files
    #  on standard AFC, so we overwrite with an empty file)
    if not afc.push_file(empty, _SHM_PATH):
        logger.warning("photos_db: failed to push empty SHM — DB may be corrupted on next read")

    logger.info(
        "photos_db: %d asset record(s) injected into Photos.sqlite", inserted
    )
    return inserted


# ---------------------------------------------------------------------------
# DB modification
# ---------------------------------------------------------------------------


def _modify_db(
    db_path: Path,
    items: list[MediaFile],
    dcim_subfolder: str,
    pushed_filenames: list[str],
    ios_version: str | None = None,
) -> int:
    """Open the local DB copy, validate schema, insert rows, checkpoint, return count."""
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")  # we manage FKs manually
        # Photos.sqlite contains CoreData change-tracking triggers that call
        # Apple-private SQLite functions (NSCoreDataDATrigger*).  These
        # functions don't exist in standard SQLite.  Register no-op stubs so
        # the triggers fire without raising "no such function" errors.
        # -1 = accept any number of arguments; returns None (NULL in SQL).
        _coredata_funcs = [
            "NSCoreDataDATriggerInsertUpdatedAffectedObjectValue",
            "NSCoreDataDATriggerUpdatedAffectedObjectValue",
            "NSCoreDataTriggerUpdateAffectedObjectValue",
        ]
        for fn in _coredata_funcs:
            conn.create_function(fn, -1, lambda *_: None)
    except Exception as exc:
        logger.error("photos_db: cannot open local Photos.sqlite: %s", exc)
        return 0

    # ── Schema guard: validate before any write ────────────────────────────
    schema_ok, issues = validate_photos_schema(conn, ios_version)
    if not schema_ok:
        # Log the full Z_PRIMARYKEY for maintainer reference, then bail.
        log_actual_schema(conn)
        try:
            conn.close()
        except Exception:
            pass
        return 0

    try:
        # Gather current Z_MAX values from Z_PRIMARYKEY
        pks = _read_primary_keys(conn)

        next_asset = pks.get(_ENT_ASSET, 0) + 1
        next_ext = pks.get(_ENT_EXTENDED_ATTRIBUTES, 0) + 1
        next_res = pks.get(_ENT_INTERNAL_RESOURCE, 0) + 1
        next_moment = pks.get(_ENT_MOMENT, 0) + 1

        now_apple = _now_apple_ts()

        # Create one ZMOMENT for the entire import batch
        photo_count = sum(
            1 for item in items if _kind_for_item(item) == 0
        )
        video_count = len(items) - photo_count

        moment_pk = next_moment
        _insert_moment(
            conn,
            moment_pk,
            items,
            photo_count,
            video_count,
            now_apple,
        )

        inserted = 0
        asset_pk = next_asset
        ext_pk = next_ext
        res_pk = next_res

        for item, pushed_name in zip(items, pushed_filenames):
            try:
                ok = _insert_asset(
                    conn,
                    asset_pk=asset_pk,
                    ext_pk=ext_pk,
                    res_pk=res_pk,
                    moment_pk=moment_pk,
                    item=item,
                    pushed_filename=pushed_name,
                    dcim_subfolder=dcim_subfolder,
                    now_apple=now_apple,
                )
                if ok:
                    inserted += 1
                    asset_pk += 1
                    ext_pk += 1
                    res_pk += 1
            except Exception as exc:
                logger.warning(
                    "photos_db: error inserting %s: %s", item.filename, exc
                )

        # Update Z_PRIMARYKEY counters
        _update_primary_keys(
            conn,
            {
                _ENT_ASSET: asset_pk - 1,
                _ENT_ADDITIONAL_ASSET_ATTRIBUTES: asset_pk - 1,
                _ENT_EXTENDED_ATTRIBUTES: ext_pk - 1,
                _ENT_INTERNAL_RESOURCE: res_pk - 1,
                _ENT_MOMENT: moment_pk,
            },
        )

        # Checkpoint WAL → main file so push is self-contained
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        return inserted
    except Exception as exc:
        logger.error("photos_db: unexpected DB error: %s", exc)
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CoreData row helpers
# ---------------------------------------------------------------------------


def _insert_moment(
    conn: sqlite3.Connection,
    moment_pk: int,
    items: list[MediaFile],
    photo_count: int,
    video_count: int,
    now_apple: float,
) -> None:
    """Insert one ZMOMENT row covering all items in the batch."""
    total = len(items)
    # Representative date: earliest creation date among items, or now
    dates = [_apple_ts_for_item(item) for item in items]
    start_date = min(dates) if dates else now_apple
    end_date = max(dates) if dates else now_apple
    representative = start_date
    moment_uuid = str(uuid.uuid4()).upper()

    conn.execute(
        """
        INSERT INTO ZMOMENT (
            Z_PK, Z_ENT, Z_OPT,
            ZCACHEDCOUNT, ZCACHEDPHOTOSCOUNT, ZCACHEDVIDEOSCOUNT,
            ZORIGINATORSTATE, ZPROCESSEDLOCATION,
            ZTIMEZONEOFFSET, ZTRASHEDSTATE,
            ZAGGREGATIONSCORE,
            ZAPPROXIMATELATITUDE, ZAPPROXIMATELONGITUDE,
            ZSTARTDATE, ZENDDATE, ZREPRESENTATIVEDATE, ZMODIFICATIONDATE,
            ZUUID
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            moment_pk, _ENT_MOMENT, 1,
            total, photo_count, video_count,
            _MOMENT_ORIGINATOR_STATE, 2,
            0, 0,
            -1.0,
            -180.0, -180.0,
            start_date, end_date, representative, now_apple,
            moment_uuid,
        ),
    )
    logger.debug("photos_db: inserted ZMOMENT pk=%d uuid=%s", moment_pk, moment_uuid)


def _insert_asset(
    conn: sqlite3.Connection,
    asset_pk: int,
    ext_pk: int,
    res_pk: int,
    moment_pk: int,
    item: MediaFile,
    pushed_filename: str,
    dcim_subfolder: str,
    now_apple: float,
) -> bool:
    """Insert ZASSET + ZADDITIONALASSETATTRIBUTES + ZEXTENDEDATTRIBUTES + ZINTERNALRESOURCE."""
    uti = _uti_for_item(item)
    kind = _UTI_TO_KIND.get(uti, 0)
    width, height = _dimensions_for_item(item)
    date_created = _apple_ts_for_item(item)
    file_size = item.local_path.stat().st_size if item.local_path.exists() else 0
    asset_uuid = str(uuid.uuid4()).upper()
    directory = f"DCIM/{dcim_subfolder}"

    # ── ZASSET ────────────────────────────────────────────────────────────
    conn.execute(
        """
        INSERT INTO ZASSET (
            Z_PK, Z_ENT, Z_OPT,
            ZKIND, ZHEIGHT, ZWIDTH, ZORIENTATION,
            ZDATECREATED, ZADDEDDATE, ZMODIFICATIONDATE,
            ZDIRECTORY, ZFILENAME,
            ZUUID, ZUNIFORMTYPEIDENTIFIER,
            ZTRASHEDSTATE, ZVISIBILITYSTATE, ZCOMPLETE,
            ZSAVEDASSETTYPE, ZPLAYBACKSTYLE,
            ZDEFERREDPROCESSINGNEEDED, ZVIDEODEFERREDPROCESSINGNEEDED,
            ZACTIVELIBRARYSCOPEPARTICIPATIONSTATE,
            ZADDITIONALATTRIBUTES, ZEXTENDEDATTRIBUTES, ZMOMENT,
            ZADJUSTMENTSSTATE, ZALBUMASSOCIATIVITY, ZAVALANCHEKIND,
            ZAVALANCHEPICKTYPE, ZBUNDLESCOPE,
            ZFAVORITE, ZHIDDEN,
            ZCLOUDDELETESTATE, ZCLOUDLOCALSTATE,
            ZSYNDICATIONSTATE, ZDUPLICATEASSETVISIBILITYSTATE
        ) VALUES (
            ?,?,?,
            ?,?,?,?,
            ?,?,?,
            ?,?,
            ?,?,
            ?,?,?,
            ?,?,
            ?,?,
            ?,
            ?,?,?,
            ?,?,?,
            ?,?,
            ?,?,
            ?,?,
            ?,?
        )
        """,
        (
            asset_pk, _ENT_ASSET, 1,
            kind, height, width, 1,
            date_created, now_apple, now_apple,
            directory, pushed_filename,
            asset_uuid, uti,
            0, 0, 1,
            _SAVED_TYPE_IMPORTED, 2,
            0, 0,
            0,
            asset_pk, ext_pk, moment_pk,
            0, 0, 0,
            0, 0,
            0, 0,
            0, 0,
            0, 0,
        ),
    )

    # ── ZADDITIONALASSETATTRIBUTES ────────────────────────────────────────
    conn.execute(
        """
        INSERT INTO ZADDITIONALASSETATTRIBUTES (
            Z_PK, Z_ENT, Z_OPT,
            ZASSET,
            ZALLOWEDFORANALYSIS,
            ZIMPORTEDBY, ZDATECREATEDSOURCE,
            ZORIGINALFILESIZE, ZORIGINALHEIGHT, ZORIGINALWIDTH,
            ZORIGINALORIENTATION, ZORIGINALRESOURCECHOICE,
            ZORIGINALFILENAME,
            ZIMPORTEDBYBUNDLEIDENTIFIER, ZIMPORTEDBYDISPLAYNAME,
            ZGPSHORIZONTALACCURACY,
            ZUPLOADATTEMPTS, ZVIEWCOUNT, ZPLAYCOUNT, ZSHARECOUNT,
            ZPENDINGVIEWCOUNT, ZPENDINGPLAYCOUNT, ZPENDINGSHARECOUNT,
            ZREVERSELOCATIONDATAISVALID, ZSHIFTEDLOCATIONISVALID,
            ZSLEETISREVERSIBLE, ZSYNDICATIONHISTORY,
            ZPTPTRASHEDSTATE,
            ZCLOUDAVALANCHEPICKTYPE, ZCLOUDKINDSUBTYPE,
            ZCLOUDRECOVERYSTATE, ZCLOUDSTATERECOVERYATTEMPTSCOUNT,
            ZDESTINATIONASSETCOPYSTATE,
            ZDUPLICATEDETECTORPERCEPTUALPROCESSINGSTATE,
            ZFACEANALYSISVERSION, ZHASPEOPLESCENEMIDORGREATERCONFIDENCE,
            ZLOCATIONHASH,
            ZVARIATIONSUGGESTIONSTATES,
            ZVIDEOCPDISPLAYTIMESCALE, ZVIDEOCPDISPLAYVALUE,
            ZVIDEOCPDURATIONTIMESCALE
        ) VALUES (
            ?,?,?,
            ?,
            ?,
            ?,?,
            ?,?,?,
            ?,?,
            ?,
            ?,?,
            ?,
            ?,?,?,?,
            ?,?,?,
            ?,?,
            ?,?,
            ?,
            ?,?,
            ?,?,
            ?,
            ?,
            ?,?,
            ?,
            ?,
            ?,?,
            ?
        )
        """,
        (
            asset_pk, _ENT_ADDITIONAL_ASSET_ATTRIBUTES, 1,
            asset_pk,
            1,
            _IMPORTED_BY_OTHER, 3,
            file_size, height, width,
            1, 0,
            item.filename,
            "com.phonetransfer.app", "PhoneTransfer",
            -1.0,
            0, 0, 0, 0,
            0, 0, 0,
            0, 0,
            0, 0,
            0,
            0, 0,
            0, 0,
            0,
            0,
            0, 0,
            None,
            0,
            0, 0,
            0,
        ),
    )

    # ── ZEXTENDEDATTRIBUTES ───────────────────────────────────────────────
    conn.execute(
        """
        INSERT INTO ZEXTENDEDATTRIBUTES (
            Z_PK, Z_ENT, Z_OPT,
            ZASSET,
            ZFLASHFIRED,
            ZDATECREATED, ZORIENTATION, ZTIMEZONEOFFSET,
            ZLATITUDE, ZLONGITUDE,
            ZSLEETCAST, ZGENERATIVEAITYPE
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ext_pk, _ENT_EXTENDED_ATTRIBUTES, 1,
            asset_pk,
            0,
            date_created, 1, 0,
            item.latitude if item.latitude is not None else -180.0,
            item.longitude if item.longitude is not None else -180.0,
            0, 0,
        ),
    )

    # ── ZINTERNALRESOURCE ─────────────────────────────────────────────────
    compact_uti = _UTI_TO_COMPACT.get(uti, "1")
    subtype = _UTI_TO_SUBTYPE.get(uti, 1)
    keydata = _UTI_TO_KEYDATA.get(uti, bytes.fromhex("030000004001"))

    conn.execute(
        """
        INSERT INTO ZINTERNALRESOURCE (
            Z_PK, Z_ENT, Z_OPT,
            ZASSET,
            ZRESOURCETYPE,
            ZDATALENGTH, ZLOCALAVAILABILITY, ZLOCALAVAILABILITYTARGET,
            ZDATASTORECLASSID, ZDATASTORESUBTYPE,
            ZCOMPACTUTI, ZDATASTOREKEYDATA,
            ZCLOUDDELETESTATE, ZCLOUDLOCALSTATE,
            ZCLOUDPREFETCHCOUNT, ZCLOUDSOURCETYPE,
            ZPTPTRASHEDSTATE, ZTRASHEDSTATE,
            ZREMOTEAVAILABILITY, ZREMOTEAVAILABILITYTARGET,
            ZVERSION, ZUNORIENTEDHEIGHT, ZUNORIENTEDWIDTH,
            ZUTICONFORMANCEHINT, ZSIDECARINDEX
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            res_pk, _ENT_INTERNAL_RESOURCE, 1,
            asset_pk,
            0,
            file_size, 1, 0,
            0, subtype,
            compact_uti, keydata,
            0, 0,
            0, 0,
            0, 0,
            0, 0,
            3, 0, 0,
            0, None,
        ),
    )

    logger.debug(
        "photos_db: inserted asset pk=%d uuid=%s file=%s/%s",
        asset_pk, asset_uuid, directory, pushed_filename,
    )
    return True


# ---------------------------------------------------------------------------
# Z_PRIMARYKEY helpers
# ---------------------------------------------------------------------------


def _read_primary_keys(conn: sqlite3.Connection) -> dict[int, int]:
    """Return {Z_ENT: Z_MAX} from Z_PRIMARYKEY."""
    cur = conn.execute("SELECT Z_ENT, Z_MAX FROM Z_PRIMARYKEY")
    return {row[0]: row[1] for row in cur.fetchall()}


def _update_primary_keys(
    conn: sqlite3.Connection, updates: dict[int, int]
) -> None:
    """Set Z_MAX for each entity in *updates*."""
    for ent, max_val in updates.items():
        conn.execute(
            "UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_ENT=?",
            (max_val, ent),
        )
        logger.debug("photos_db: Z_PRIMARYKEY entity %d → Z_MAX=%d", ent, max_val)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _now_apple_ts() -> float:
    """Current time as an Apple CoreData timestamp (seconds since 2001-01-01 UTC)."""
    return datetime.now(tz=timezone.utc).timestamp() - _APPLE_EPOCH_OFFSET


def _apple_ts_for_item(item: MediaFile) -> float:
    """Return the Apple CoreData timestamp for a MediaFile's creation date."""
    if item.created is not None:
        ts = item.created.timestamp()
        return ts - _APPLE_EPOCH_OFFSET
    # Fall back to file mtime
    if item.local_path.exists():
        return item.local_path.stat().st_mtime - _APPLE_EPOCH_OFFSET
    return _now_apple_ts()


_EXT_TO_UTI: dict[str, str] = {
    ".jpg": "public.jpeg",
    ".jpeg": "public.jpeg",
    ".png": "public.png",
    ".heic": "public.heic",
    ".heif": "public.heic",
    ".webp": "org.webmproject.webp",
    ".gif": "com.compuserve.gif",
    ".bmp": "com.microsoft.bmp",
    ".tiff": "public.tiff",
    ".tif": "public.tiff",
    ".mov": "com.apple.quicktime-movie",
    ".mp4": "public.mpeg-4",
    ".m4v": "public.m4v-video",
}


def _uti_for_item(item: MediaFile) -> str:
    """Return the UTI for a MediaFile.

    Extension is checked first — it is always authoritative for files that
    have arrived from Android with a known extension.  MIME type is used only
    as a fallback for formats whose extension is ambiguous or missing.
    """
    ext = Path(item.filename).suffix.lower()
    if ext in _EXT_TO_UTI:
        return _EXT_TO_UTI[ext]
    mime = (item.mime_type or "").lower().split(";")[0].strip()
    if mime in _MIME_TO_UTI:
        return _MIME_TO_UTI[mime]
    return "public.jpeg"


def _kind_for_item(item: MediaFile) -> int:
    """0 = photo, 1 = video."""
    return _UTI_TO_KIND.get(_uti_for_item(item), 0)


def _dimensions_for_item(item: MediaFile) -> tuple[int, int]:
    """Return (width, height) by reading image metadata via Pillow."""
    if not item.local_path.exists():
        return 0, 0
    try:
        from PIL import Image  # type: ignore[import]
        with Image.open(item.local_path) as img:
            return img.width, img.height
    except Exception as exc:
        logger.debug(
            "photos_db: could not read dimensions for %s: %s",
            item.local_path.name, exc,
        )
        return 0, 0
