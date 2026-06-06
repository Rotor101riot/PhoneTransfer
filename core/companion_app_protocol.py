"""
companion_app_protocol.py

Python-side TCP client for the Android companion APK.

The companion APK runs a socket server on device port 7337. ADB port forwarding
maps ``localhost:7337`` on the host machine to ``device:7337``. Every message
uses the following framing protocol:

    [ 4 bytes: uint32 LE payload length ][ N bytes: UTF-8 JSON body ]

The maximum permitted frame is 64 MB. Both sides must observe this limit.

Protocol v2 (negotiated via ``hello`` handshake) adds:

- ``_v``: protocol version integer
- ``_type``: message type — ``iq`` (request/response), ``msg`` (push),
  ``event`` (device state change)
- ``_seq``: monotonic sequence ID for request/response correlation
- Event subscriptions: ``subscribe`` / ``unsubscribe`` commands
- Heartbeat: ``heartbeat`` command for connection liveness checks

v1 sessions (no ``hello``) continue to work unchanged.

Typical session::

    from core.adb_manager import ADBManager
    from core.companion_app_protocol import CompanionClient, setup_adb_forward

    adb = ADBManager()
    setup_adb_forward(adb, serial="emulator-5554")

    with CompanionClient() as client:
        if client.ping():
            result = client.extract("contacts")

Usage notes
-----------
- :func:`setup_adb_forward` must be called before :meth:`CompanionClient.connect`.
- :func:`teardown_adb_forward` should be called when the session is finished to
  release the ADB forward rule.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import struct
import zlib
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.adb_manager import ADBManager

logger = logging.getLogger(__name__)

_COMPANION_PKG: str = "com.phonetransfer.companion"

_MAX_FRAME_BYTES: int = 64 * 1024 * 1024  # 64 MB
_LENGTH_FORMAT: str = "<I"                  # 4-byte little-endian uint32
_LENGTH_SIZE: int = struct.calcsize(_LENGTH_FORMAT)

# Transfer chunk size for file_pull / file_push.  2 MB matches Dr.Fone's
# dominant I/O constant (0x200000) observed in RE — big enough for 4× fewer
# round-trips than 512 KB, small enough to avoid single-buffer allocation
# pressure on mid-range Android devices.  Well within the 64 MB frame limit.
_CHUNK_SIZE: int = 2 * 1024 * 1024  # 2 MB

# Minimum JSON payload size before zlib compression is attempted (bytes).
_COMPRESS_THRESHOLD: int = 4096

# zlib default compression CMF byte (method=8, info=7)
_ZLIB_MAGIC: int = 0x78

# Protocol version constants
PROTOCOL_VERSION: int = 2
# Oldest companion APK protocol version the desktop can work with fully.
# APKs reporting a lower version are warned about but not disconnected —
# v1 sessions degrade gracefully (no compression, no events, no heartbeat).
COMPANION_MIN_VERSION: int = 2

# Message type constants (match Kotlin MessageType)
MSG_TYPE_IQ: str = "iq"
MSG_TYPE_MSG: str = "msg"
MSG_TYPE_EVENT: str = "event"

# Event namespace constants (match Kotlin EventNamespace)
NS_BATTERY: str = "battery"
NS_STORAGE: str = "storage"
NS_SCREEN: str = "screen"
NS_NOTIFY: str = "notify"
NS_APP: str = "app"
NS_NETWORK: str = "network"
ALL_EVENT_NAMESPACES: list[str] = [NS_BATTERY, NS_STORAGE, NS_SCREEN, NS_NOTIFY, NS_APP, NS_NETWORK]

# Connection recovery constants
_MAX_RECONNECT_ATTEMPTS: int = 4
_RECONNECT_BASE_DELAY: float = 1.0  # seconds, doubled each retry
_HEARTBEAT_INTERVAL: float = 15.0   # seconds between heartbeat pings

# Per-category end-to-end command deadlines (seconds).
# These cap how long a single extract/inject command may run before the
# desktop surface a SocketTimeoutError and treats the category as failed.
# Without these, one stuck companion handler (e.g. WhatsApp DB scan on a
# device with 50 k messages) can block the entire pipeline queue indefinitely
# because progress frames keep the per-read socket timeout from firing.
EXTRACT_TIMEOUTS: dict[str, float] = {
    "contacts":       120.0,
    "sms":            180.0,
    "calls":           60.0,
    "calendar":        60.0,
    "notes":           60.0,
    "alarms":          30.0,
    "reminders":       60.0,
    "bookmarks":       30.0,
    "blocked":         30.0,
    "contact_groups":  30.0,
    "browser_history": 60.0,
    "clipboard":       15.0,
    "installed_apps":  90.0,
    "photos":         600.0,   # large media directories can be slow to enumerate
    "videos":         600.0,
    "ringtones":       60.0,
    "voice_memos":     60.0,
    "wallpaper":       30.0,
    "whatsapp":       300.0,
    "telegram":       300.0,
}

INJECT_TIMEOUTS: dict[str, float] = {
    "contacts":        90.0,
    "sms":            180.0,
    "calls":           60.0,
    "calendar":        60.0,
    "notes":           60.0,
    "alarms":          30.0,
    "reminders":       60.0,
    "bookmarks":       30.0,
    "blocked":         30.0,
    "contact_groups":  30.0,
    "browser_history": 60.0,
    "clipboard":       15.0,
    "installed_apps":  60.0,
    "photos":         900.0,
    "videos":         900.0,
    "ringtones":       90.0,
    "voice_memos":     90.0,
    "wallpaper":       30.0,
    "whatsapp":       300.0,
    "telegram":       300.0,
}

# Fallback deadline for categories not listed above (30 minutes)
_DEFAULT_DEADLINE: float = 1800.0


class CompanionClient:
    """
    TCP client that speaks the PhoneTransfer companion-APK protocol.

    The framing layer is handled transparently by :meth:`send` / :meth:`recv`;
    callers work entirely in terms of Python dicts.

    Protocol v2 features (enabled automatically when the APK supports it):
    - ``_seq`` sequence IDs for request/response correlation
    - Event subscriptions via :meth:`subscribe` / :meth:`unsubscribe`
    - Heartbeat keepalive via :meth:`start_heartbeat`
    - Auto-reconnect with exponential backoff

    Parameters
    ----------
    host:
        Hostname to connect to.  Always ``"127.0.0.1"`` when using ADB
        port forwarding.
    port:
        Host-side port (must match the ADB forward rule; default 7337).
    timeout:
        Socket timeout in seconds applied to each individual I/O operation.
    auto_reconnect:
        If True, automatically reconnect on connection loss (up to 4 retries
        with exponential backoff).
    event_callback:
        Optional callable invoked when the APK pushes an event frame.
        Signature: ``callback(namespace: str, data: dict) -> None``.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7337,
        timeout: float = 30.0,
        auto_reconnect: bool = False,
        event_callback: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._auto_reconnect = auto_reconnect
        self._event_callback = event_callback

        # v2 protocol state
        self._protocol_version: int = 1
        self._compress_json: bool = False
        self._seq_counter: int = 0
        self._seq_lock = threading.Lock()

        # Heartbeat
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()

        # Connection state
        self._connected = threading.Event()

    @property
    def protocol_version(self) -> int:
        """Negotiated protocol version (1 or 2)."""
        return self._protocol_version

    def _next_seq(self) -> int:
        """Return the next monotonic sequence ID (v2 only, 0 for v1)."""
        if self._protocol_version < 2:
            return 0
        with self._seq_lock:
            self._seq_counter += 1
            return self._seq_counter

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Open a TCP connection to the companion APK and perform a v2
        handshake if supported.

        Raises
        ------
        ConnectionError
            If the connection cannot be established.
        """
        self._raw_connect()
        self._handshake()

    def _raw_connect(self) -> None:
        """Open the TCP socket without handshake."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._timeout)
            sock.connect((self._host, self._port))
            self._sock = sock
            self._connected.set()
            logger.debug("CompanionClient connected to %s:%d", self._host, self._port)
        except OSError as exc:
            sock.close()
            raise ConnectionError(
                f"Cannot connect to companion APK at {self._host}:{self._port}: {exc}"
            ) from exc

    def _handshake(self) -> None:
        """
        Perform the v2 protocol handshake.  If the APK doesn't understand
        ``hello``, we fall back to v1 silently.
        """
        try:
            hello = {"cmd": "hello", "_v": PROTOCOL_VERSION, "compress": "zlib"}
            self._raw_send(hello)
            response = self._raw_recv()
            if response.get("status") == "ok" and response.get("cmd") == "hello":
                self._protocol_version = int(response.get("_v", 1))
                self._compress_json = bool(response.get("compress", False))
                self._seq_counter = 0
                logger.info(
                    "Protocol handshake: v%d (server v%s, compress=%s, "
                    "max_clients=%s)",
                    self._protocol_version,
                    response.get("server_version", "?"),
                    self._compress_json,
                    response.get("max_concurrent_clients", "?"),
                )
                if self._protocol_version < COMPANION_MIN_VERSION:
                    logger.warning(
                        "Companion APK protocol v%d is below minimum v%d — "
                        "update the companion APK; compression, event "
                        "subscriptions, and heartbeat will be unavailable.",
                        self._protocol_version, COMPANION_MIN_VERSION,
                    )
            else:
                # APK didn't understand hello — v1 fallback
                self._protocol_version = 1
                self._compress_json = False
                logger.info("Protocol handshake: APK returned non-hello response, using v1")
        except (ConnectionError, ValueError, OSError) as exc:
            # If hello itself fails, stay at v1
            self._protocol_version = 1
            self._compress_json = False
            logger.info("Protocol handshake failed (%s), using v1", exc)

    def reconnect(self) -> bool:
        """
        Attempt to reconnect with exponential backoff.

        Returns
        -------
        ``True`` if reconnection succeeded, ``False`` if all retries exhausted.
        """
        self._connected.clear()
        self.stop_heartbeat()

        delay = _RECONNECT_BASE_DELAY
        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            logger.info(
                "Reconnect attempt %d/%d (delay %.1fs)",
                attempt, _MAX_RECONNECT_ATTEMPTS, delay,
            )
            # Close stale socket
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

            try:
                self._raw_connect()
                self._handshake()
                logger.info("Reconnected successfully on attempt %d", attempt)
                return True
            except ConnectionError:
                if attempt < _MAX_RECONNECT_ATTEMPTS:
                    time.sleep(delay)
                    delay *= 2  # exponential backoff
        logger.error("All %d reconnect attempts failed", _MAX_RECONNECT_ATTEMPTS)
        return False

    def disconnect(self) -> None:
        """Close the socket gracefully and stop heartbeat."""
        self.stop_heartbeat()
        self._connected.clear()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            finally:
                self._sock = None
                self._protocol_version = 1
                self._compress_json = False
                self._seq_counter = 0
                logger.debug("CompanionClient disconnected")

    def __enter__(self) -> "CompanionClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Low-level framing
    # ------------------------------------------------------------------

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise ConnectionError("CompanionClient is not connected. Call connect() first.")
        return self._sock

    def _recv_exactly(self, n: int) -> bytes:
        """Read exactly *n* bytes from the socket, blocking until all arrive."""
        sock = self._require_socket()
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(
                    f"Connection closed by remote while reading {n} bytes "
                    f"(received {len(buf)} so far)"
                )
            buf.extend(chunk)
        return bytes(buf)

    def _raw_send(self, payload: dict) -> None:
        """Send a frame without adding v2 fields. Used by handshake."""
        sock = self._require_socket()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > _MAX_FRAME_BYTES:
            raise ValueError(
                f"Payload too large: {len(body)} bytes > {_MAX_FRAME_BYTES} byte limit"
            )
        header = struct.pack(_LENGTH_FORMAT, len(body))
        try:
            sock.sendall(header + body)
        except OSError as exc:
            raise ConnectionError(f"Failed to send payload: {exc}") from exc

    def _raw_recv(self) -> dict:
        """Read one frame without filtering. Used by handshake."""
        return self._recv_frame()

    def send(self, payload: dict) -> None:
        """
        Serialize *payload* to UTF-8 JSON and write a framed message.

        In v2 mode, automatically injects ``_v``, ``_type``, and ``_seq``
        fields if not already present.

        Format: ``[uint32 LE length][UTF-8 JSON body]``

        Parameters
        ----------
        payload:
            Arbitrary JSON-serialisable dict.

        Raises
        ------
        ValueError
            If the serialised payload exceeds the 64 MB frame limit.
        ConnectionError
            If the socket is not connected or the write fails.
        """
        # Inject v2 fields if negotiated
        if self._protocol_version >= 2:
            if "_v" not in payload:
                payload = {**payload, "_v": self._protocol_version}
            if "_type" not in payload:
                payload = {**payload, "_type": MSG_TYPE_IQ}
            if "_seq" not in payload:
                payload = {**payload, "_seq": self._next_seq()}

        sock = self._require_socket()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # Compress large JSON payloads if negotiated with the APK
        if self._compress_json and len(body) > _COMPRESS_THRESHOLD:
            compressed = zlib.compress(body)
            if len(compressed) < len(body):
                body = compressed
        if len(body) > _MAX_FRAME_BYTES:
            raise ValueError(
                f"Payload too large: {len(body)} bytes > {_MAX_FRAME_BYTES} byte limit"
            )
        header = struct.pack(_LENGTH_FORMAT, len(body))
        try:
            sock.sendall(header + body)
            logger.debug("send: cmd=%r seq=%s, %d bytes",
                         payload.get("cmd"), payload.get("_seq"), len(body))
        except OSError as exc:
            if self._auto_reconnect and self.reconnect():
                # Retry once after reconnect
                self.send(payload)
                return
            raise ConnectionError(f"Failed to send payload: {exc}") from exc

    def recv(self) -> dict:
        """
        Read the next non-progress, non-event response frame.

        Companion handlers send unsolicited ``{"type": "progress", ...}`` frames
        *before* the final command response so the device UI can update in real
        time.  v2 APKs also send ``{"_type": "event", ...}`` frames for
        subscribed device state changes.

        This method silently drains progress frames and dispatches event frames
        to the registered callback, returning only the IQ response.

        Returns
        -------
        Parsed JSON dict from the APK (the actual command response).

        Raises
        ------
        ValueError
            If a declared frame size exceeds 64 MB or a body is not valid JSON.
        ConnectionError
            If the connection drops during reading.
        """
        while True:
            try:
                frame = self._recv_frame()
            except ConnectionError:
                if self._auto_reconnect and self.reconnect():
                    continue
                raise

            # Skip v1 progress frames
            if frame.get("type") == "progress" or frame.get("status") == "progress":
                logger.debug(
                    "recv: skipping progress frame — category=%r %s/%s",
                    frame.get("category"),
                    frame.get("done"),
                    frame.get("total"),
                )
                continue

            # Dispatch v2 event frames to callback
            if frame.get("_type") == MSG_TYPE_EVENT:
                ns = frame.get("ns", "")
                data = frame.get("data", {})
                logger.debug("recv: event ns=%r", ns)
                if self._event_callback is not None:
                    try:
                        self._event_callback(ns, data)
                    except Exception:
                        logger.exception("Event callback error for ns=%r", ns)
                continue

            return frame

    def _recv_frame(self) -> dict:
        """
        Read exactly one framed message and return the decoded dict.

        This is the low-level primitive used by :meth:`recv`.  Callers that
        need to handle progress frames themselves can call this directly, but
        normally :meth:`recv` is the correct entry point.
        """
        header = self._recv_exactly(_LENGTH_SIZE)
        (length,) = struct.unpack(_LENGTH_FORMAT, header)
        if length > _MAX_FRAME_BYTES:
            raise ValueError(
                f"Incoming frame too large: {length} bytes > {_MAX_FRAME_BYTES} byte limit"
            )
        body_bytes = self._recv_exactly(length)
        # Transparent zlib decompression: if the first byte is the zlib magic
        # (0x78), decompress before JSON parsing.  Safe because valid JSON
        # never starts with 0x78.
        if body_bytes and body_bytes[0] == _ZLIB_MAGIC:
            try:
                body_bytes = zlib.decompress(body_bytes)
            except zlib.error:
                pass  # not actually zlib — try as raw JSON
        try:
            result: dict = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"Failed to decode companion APK response: {exc}"
            ) from exc
        logger.debug(
            "_recv_frame: %d bytes, type=%r _type=%r status=%r",
            length, result.get("type"), result.get("_type"), result.get("status"),
        )
        return result

    # ------------------------------------------------------------------
    # Binary frame I/O  (for file transfer and APK streaming)
    # ------------------------------------------------------------------

    def recv_binary_frame(self) -> bytes:
        """
        Read one raw binary frame from the socket.

        Binary frames use the same 4-byte LE length header as JSON frames,
        but the body is *not* decoded as UTF-8 JSON — it is returned as raw
        bytes.  The caller must know from command-flow context when to expect
        a binary frame (e.g. after receiving an ``app_apk_chunk`` JSON header
        or a ``file_pull`` header response).

        Returns
        -------
        Raw bytes of the frame body.
        """
        header = self._recv_exactly(_LENGTH_SIZE)
        (length,) = struct.unpack(_LENGTH_FORMAT, header)
        if length > _MAX_FRAME_BYTES:
            raise ValueError(
                f"Incoming binary frame too large: {length} bytes > "
                f"{_MAX_FRAME_BYTES} byte limit"
            )
        return self._recv_exactly(length)

    def send_binary_frame(self, data: bytes) -> None:
        """
        Send raw bytes as a length-prefixed binary frame.

        Parameters
        ----------
        data:
            Raw bytes to send.
        """
        sock = self._require_socket()
        if len(data) > _MAX_FRAME_BYTES:
            raise ValueError(
                f"Binary payload too large: {len(data)} bytes > "
                f"{_MAX_FRAME_BYTES} byte limit"
            )
        header = struct.pack(_LENGTH_FORMAT, len(data))
        try:
            sock.sendall(header + data)
        except OSError as exc:
            raise ConnectionError(f"Failed to send binary frame: {exc}") from exc

    def recv_file_pull(
        self,
        dest_path: Path,
    ) -> dict:
        """
        Receive a ``file_pull`` response: JSON header, N binary chunks,
        then JSON done frame.  Writes chunks to *dest_path* and verifies
        the MD5 checksum reported in the done frame.

        Parameters
        ----------
        dest_path:
            Local file path to write the pulled data to.

        Returns
        -------
        The final "done" JSON dict from the APK (contains ``"md5"``).
        """
        # The header frame was already received by the caller via send_recv,
        # which consumed the initial JSON response.  Binary chunks follow
        # until a JSON done frame arrives.
        md5 = hashlib.md5()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as fh:
            while True:
                # Peek at length header to decide if this is JSON or binary.
                # We read the full frame as raw bytes first.
                raw = self.recv_binary_frame()
                # Try to decode as JSON — if it succeeds and has a "status"
                # key, it is the done frame.
                try:
                    text = raw.decode("utf-8")
                    frame = json.loads(text)
                    if isinstance(frame, dict) and "status" in frame:
                        # Done frame — verify MD5
                        remote_md5 = frame.get("md5", "")
                        local_md5 = md5.hexdigest()
                        if remote_md5 and remote_md5 != local_md5:
                            logger.warning(
                                "file_pull MD5 mismatch: remote=%s local=%s "
                                "for %s",
                                remote_md5, local_md5, dest_path,
                            )
                        else:
                            logger.debug(
                                "file_pull MD5 verified: %s for %s",
                                local_md5, dest_path,
                            )
                        return frame
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                # Binary chunk — write to file
                fh.write(raw)
                md5.update(raw)

    def extract_installed_apps_with_apk(
        self,
        staging_dir: Path,
        **kwargs,
    ) -> tuple[list[dict], list[Path]]:
        """
        Send ``extract_installed_apps`` with ``include_apk=true`` and receive
        both the app list JSON response and the streamed APK binary files.

        The companion APK sends:
        1. JSON response with the app list (``data`` key)
        2. For each APK:
           a. JSON ``app_apk_chunk`` header (package_name, filename, size)
           b. N binary frames (512 KB each)
           c. JSON ``app_apk_done`` (package_name)
        3. Progress frames interspersed (type=progress)

        Parameters
        ----------
        staging_dir:
            Directory to write APK files into.
        **kwargs:
            Additional keys merged into the extract command.

        Returns
        -------
        (app_list, apk_paths)
            app_list: the ``data`` list from the initial JSON response.
            apk_paths: list of local Paths to downloaded APK files.
        """
        staging_dir.mkdir(parents=True, exist_ok=True)

        payload = {"cmd": "extract_installed_apps", "include_apk": True, **kwargs}
        self.send(payload)

        # 1. Read the initial JSON response (may be preceded by progress frames)
        initial_response = self.recv()
        app_list: list[dict] = initial_response.get("data", [])
        total_apps = len(app_list)

        logger.info(
            "extract_installed_apps: received app list with %d entries, "
            "now receiving APK streams…",
            total_apps,
        )

        apk_paths: list[Path] = []

        # 2. Receive APK streams until we have processed all apps
        apps_received = 0
        while apps_received < total_apps:
            # Read next frame — could be progress, app_apk_chunk header, or
            # unexpected end.
            frame = self._recv_frame()

            if frame.get("type") == "progress":
                logger.debug(
                    "APK stream progress: %s/%s",
                    frame.get("done"), frame.get("total"),
                )
                continue

            cmd = frame.get("cmd")
            if cmd == "app_apk_chunk":
                # APK header — start receiving binary chunks
                pkg = frame.get("package_name", "unknown")
                filename = frame.get("filename", f"{pkg}.apk")
                expected_size = int(frame.get("size", 0))
                apk_path = staging_dir / filename

                logger.debug(
                    "Receiving APK: %s (%d bytes)", filename, expected_size,
                )

                received = 0
                with open(apk_path, "wb") as fh:
                    while received < expected_size:
                        chunk = self.recv_binary_frame()
                        fh.write(chunk)
                        received += len(chunk)

                logger.debug(
                    "APK received: %s (%d bytes written)", filename, received,
                )
                apk_paths.append(apk_path)

                # Read the app_apk_done frame
                done_frame = self._recv_frame()
                while done_frame.get("type") == "progress":
                    done_frame = self._recv_frame()

                if done_frame.get("cmd") != "app_apk_done":
                    logger.warning(
                        "Expected app_apk_done for %s, got: %s",
                        pkg, done_frame.get("cmd"),
                    )

                apps_received += 1
            else:
                # Unexpected frame — log and break
                logger.warning(
                    "Unexpected frame during APK streaming: %r", frame,
                )
                break

        logger.info(
            "extract_installed_apps: received %d APK file(s)", len(apk_paths),
        )
        return app_list, apk_paths

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def send_recv(self, payload: dict) -> dict:
        """
        Send *payload* and return the next response from the APK.

        Parameters
        ----------
        payload:
            Command dict to send.

        Returns
        -------
        Response dict from the APK.
        """
        self.send(payload)
        return self.recv()

    def send_recv_timed(self, payload: dict, deadline: float) -> dict:
        """
        Send *payload* and read frames until an IQ response arrives, honouring
        an end-to-end *deadline* (seconds) measured from call entry.

        Unlike :meth:`send_recv`, which relies solely on the per-read socket
        timeout, this method resets ``sock.settimeout`` before every frame read
        so the remaining window shrinks with each progress frame received.  A
        companion handler that streams progress pings can no longer stall the
        pipeline indefinitely.

        Parameters
        ----------
        payload:
            Command dict to send.
        deadline:
            Maximum elapsed seconds from call entry to final IQ response.

        Raises
        ------
        socket.timeout
            If total elapsed time exceeds *deadline*.
        ConnectionError
            If the socket is not connected or drops during reading.
        """
        self.send(payload)
        t_start = time.monotonic()
        sock = self._require_socket()
        saved_timeout = sock.gettimeout()
        try:
            while True:
                remaining = deadline - (time.monotonic() - t_start)
                if remaining <= 0.0:
                    raise socket.timeout(
                        f"end-to-end deadline of {deadline:.1f}s exceeded"
                    )
                sock.settimeout(min(self._timeout, remaining))
                try:
                    frame = self._recv_frame()
                except ConnectionError:
                    if self._auto_reconnect and self.reconnect():
                        continue
                    raise

                if frame.get("type") == "progress" or frame.get("status") == "progress":
                    logger.debug(
                        "send_recv_timed: progress %s/%s elapsed=%.1fs deadline=%.1fs",
                        frame.get("done"), frame.get("total"),
                        time.monotonic() - t_start, deadline,
                    )
                    continue

                if frame.get("_type") == MSG_TYPE_EVENT:
                    ns = frame.get("ns", "")
                    data = frame.get("data", {})
                    if self._event_callback is not None:
                        try:
                            self._event_callback(ns, data)
                        except Exception:
                            logger.exception("Event callback error for ns=%r", ns)
                    continue

                return frame
        finally:
            try:
                sock.settimeout(saved_timeout)
            except OSError:
                pass

    def ping(self) -> bool:
        """
        Send a ping command and check for a successful response.

        Returns
        -------
        ``True`` if the APK responds ``{"status": "ok"}``, ``False`` otherwise.
        """
        try:
            response = self.send_recv({"cmd": "ping"})
            return response.get("status") == "ok"
        except (ConnectionError, ValueError, OSError) as exc:
            logger.warning("ping failed: %s", exc)
            return False

    def capabilities(self) -> dict:
        """
        Query the APK for its supported capabilities.

        Returns
        -------
        The raw response dict (APK-defined structure).
        """
        return self.send_recv({"cmd": "capabilities"})

    # ------------------------------------------------------------------
    # v2: Event subscriptions
    # ------------------------------------------------------------------

    def subscribe(self, namespaces: list[str]) -> dict:
        """
        Subscribe to real-time device event namespaces (v2 only).

        After subscribing, event frames are dispatched to the ``event_callback``
        passed to the constructor.

        Parameters
        ----------
        namespaces:
            List of namespace strings, e.g. ``["battery", "storage"]``.

        Returns
        -------
        APK response dict with ``"subscribed"`` listing active subscriptions.
        """
        return self.send_recv({"cmd": "subscribe", "ns": namespaces})

    def unsubscribe(self, namespaces: list[str]) -> dict:
        """
        Unsubscribe from device event namespaces (v2 only).

        Parameters
        ----------
        namespaces:
            Namespace strings to unsubscribe from.

        Returns
        -------
        APK response with remaining ``"subscribed"`` list.
        """
        return self.send_recv({"cmd": "unsubscribe", "ns": namespaces})

    # ------------------------------------------------------------------
    # v2: Heartbeat keepalive
    # ------------------------------------------------------------------

    def heartbeat(self) -> bool:
        """
        Send a lightweight heartbeat and check for a response.

        Returns
        -------
        ``True`` if the APK responds, ``False`` on failure.
        """
        try:
            response = self.send_recv({"cmd": "heartbeat"})
            return response.get("status") == "ok"
        except (ConnectionError, ValueError, OSError) as exc:
            logger.warning("heartbeat failed: %s", exc)
            return False

    def start_heartbeat(self, interval: float = _HEARTBEAT_INTERVAL) -> None:
        """
        Start a background thread that sends periodic heartbeats.

        If a heartbeat fails and ``auto_reconnect`` is enabled, the thread
        will attempt to reconnect before resuming.

        Parameters
        ----------
        interval:
            Seconds between heartbeat pings (default 15).
        """
        self.stop_heartbeat()
        self._heartbeat_stop.clear()

        def _heartbeat_loop() -> None:
            while not self._heartbeat_stop.wait(timeout=interval):
                if not self.heartbeat():
                    logger.warning("Heartbeat failed — connection may be lost")
                    if self._auto_reconnect:
                        if not self.reconnect():
                            logger.error("Heartbeat reconnect failed, stopping heartbeat")
                            break
                    else:
                        break

        self._heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name="companion-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        logger.debug("Heartbeat started (interval=%.1fs)", interval)

    def stop_heartbeat(self) -> None:
        """Stop the background heartbeat thread if running."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5.0)
            self._heartbeat_thread = None
            logger.debug("Heartbeat stopped")

    def extract(self, category: str, **kwargs) -> dict:
        """
        Ask the APK to extract a data category.

        Parameters
        ----------
        category:
            Logical data category name.  The command sent to the APK will be
            ``"extract_{category}"``, with the special case that ``"calls"``
            maps to ``"extract_call_log"``.
        **kwargs:
            Additional key-value pairs merged into the command dict.

        Returns
        -------
        APK response dict (typically contains ``"items"`` and ``"status"``).
        """
        if category == "calls":
            cmd_name = "extract_call_log"
        else:
            cmd_name = f"extract_{category}"
        payload = {"cmd": cmd_name, **kwargs}
        deadline = EXTRACT_TIMEOUTS.get(category, _DEFAULT_DEADLINE)
        return self.send_recv_timed(payload, deadline)

    def inject(self, category: str, items: list, **kwargs) -> dict:
        """
        Send data to the APK for injection into the device.

        Parameters
        ----------
        category:
            Logical data category.  The command will be ``"inject_{category}"``
            except ``"calls"`` → ``"inject_call_log"``.
        items:
            List of serialisable data items to inject.
        **kwargs:
            Additional key-value pairs merged into the command dict.

        Returns
        -------
        APK response dict.
        """
        if category == "calls":
            cmd_name = "inject_call_log"
        else:
            cmd_name = f"inject_{category}"
        payload = {"cmd": cmd_name, "items": items, **kwargs}
        deadline = INJECT_TIMEOUTS.get(category, _DEFAULT_DEADLINE)
        return self.send_recv_timed(payload, deadline)

    def media_list(self, media_type: str) -> dict:
        """
        Request a listing of media files of the given type.

        Parameters
        ----------
        media_type:
            e.g. ``"photos"``, ``"videos"``, ``"ringtones"``, ``"voice_memos"``,
            ``"playlists"``.

        Returns
        -------
        APK response dict.
        """
        return self.send_recv({"cmd": "media_list", "media_type": media_type})

    # ------------------------------------------------------------------
    # Device info & storage reporting  (Phase 2, Item #7)
    # ------------------------------------------------------------------

    def device_info(self) -> dict:
        """
        Query comprehensive device information including per-type storage
        breakdown.

        Returns a dict with keys like ``manufacturer``, ``model``,
        ``battery_level``, ``ram_total``, ``storage_internal_total``,
        ``storage_by_type`` (images/videos/audio/documents/downloads), etc.
        """
        return self.send_recv({"cmd": "device_info"})

    # ------------------------------------------------------------------
    # MMS attachment pull  (Phase 2, Item #4)
    # ------------------------------------------------------------------

    def mms_part_pull(self, part_id: str, dest_path: Path) -> dict:
        """
        Pull a single MMS attachment part's binary data from the device.

        The APK streams the part data as:
        1. JSON header (part_id, size)
        2. N binary chunks (512 KB each)
        3. JSON done frame (part_id, md5, size)

        Parameters
        ----------
        part_id:
            The ``_id`` of the MMS part from the extract_sms response's
            ``attachments[].part_id`` field.
        dest_path:
            Local file path to write the attachment to.

        Returns
        -------
        The done-frame dict (contains ``"md5"``, ``"size"``).
        """
        header = self.send_recv({"cmd": "mms_part_pull", "part_id": str(part_id)})
        if header.get("status") != "ok":
            return header

        # Stream binary chunks to dest_path using the same pattern as file_pull
        return self.recv_file_pull(dest_path)

    # ------------------------------------------------------------------
    # SMS role management  (Phase 2, Item #6)
    # ------------------------------------------------------------------

    def acquire_sms_role(self) -> dict:
        """
        Request the companion APK to become the default SMS app.

        This saves the current default SMS app and launches the system
        dialog for the user to approve. The PC should poll
        :meth:`check_sms_role` until it returns ``is_default_sms=True``.

        Returns
        -------
        APK response dict with ``acquired``, ``launched``, ``previous_default``.
        """
        return self.send_recv({"cmd": "acquire_sms_role"})

    def release_sms_role(self) -> dict:
        """
        Restore the default SMS app to whatever it was before
        :meth:`acquire_sms_role`.

        Returns
        -------
        APK response dict with ``released``, ``restored_to``.
        """
        return self.send_recv({"cmd": "release_sms_role"})

    def check_sms_role(self) -> dict:
        """
        Check if the companion APK is currently the default SMS app.

        Returns
        -------
        Dict with ``is_default_sms`` (bool) and ``current_default`` (package name).
        """
        return self.send_recv({"cmd": "check_sms_role"})

    def wait_for_sms_role(self, timeout: float = 30.0, poll_interval: float = 2.0) -> bool:
        """
        Poll :meth:`check_sms_role` until the companion holds the SMS role
        or *timeout* expires.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.
        poll_interval:
            Seconds between polls.

        Returns
        -------
        ``True`` if the role was acquired within the timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self.check_sms_role()
                if result.get("is_default_sms"):
                    return True
            except (ConnectionError, ValueError, OSError):
                pass
            time.sleep(poll_interval)
        return False

    def acquire_sms_role_xiaomi(self, timeout: float = 45.0) -> bool:
        """
        Xiaomi/MIUI-aware SMS role acquisition (Phase 5, Item #14).

        MIUI devices have a non-standard SMS role change flow:
        the standard RoleManager intent gets intercepted by MIUI's
        permissions manager, which can silently fail or show a different
        dialog.  The companion APK's ``ChangeDefaultSmsActivity`` handles
        this by trying MIUI's ``SmsDefaultDialog`` first.

        This method:
        1. Queries ``device_info`` to detect MIUI.
        2. If MIUI, uses an extended timeout (MIUI dialogs take longer)
           and retries once if the first attempt fails silently.
        3. Falls back to the standard flow for non-Xiaomi devices.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for the user to approve.
            Defaults to 45s (vs. 30s standard) because MIUI may show
            an intermediate permissions dialog.

        Returns
        -------
        ``True`` if the SMS role was acquired.
        """
        # Detect MIUI
        is_miui = False
        try:
            info = self.device_info()
            miui_ver = info.get("miui_version")
            manufacturer = (info.get("manufacturer") or "").lower()
            is_miui = bool(miui_ver) and any(
                b in manufacturer for b in ("xiaomi", "redmi", "poco")
            )
            if is_miui:
                logger.info(
                    "Xiaomi MIUI detected (version=%s, manufacturer=%s) "
                    "— using extended SMS role flow",
                    miui_ver, manufacturer,
                )
        except Exception:
            pass

        # First attempt
        result = self.acquire_sms_role()
        if result.get("acquired"):
            return True

        effective_timeout = timeout if is_miui else 30.0
        if self.wait_for_sms_role(timeout=effective_timeout):
            return True

        # MIUI retry: the first dialog may have been MIUI's SmsDefaultDialog
        # which succeeded in handing off to RoleManager.  Give it one more shot.
        if is_miui:
            logger.info("Xiaomi SMS role: first attempt timed out, retrying…")
            retry = self.acquire_sms_role()
            if retry.get("acquired"):
                return True
            return self.wait_for_sms_role(timeout=15.0, poll_interval=1.5)

        return False

    def file_pull(self, remote_path: str, local_path: Path) -> dict:
        """
        Pull a file from the device via the companion APK.

        Sends ``file_pull``, receives the JSON header, streams binary
        chunks to *local_path*, and verifies the MD5 checksum in the
        done frame.

        Parameters
        ----------
        remote_path:
            Absolute path on the Android device.
        local_path:
            Local path to write the file to.

        Returns
        -------
        The final done-frame dict (contains ``"md5"``, ``"size"``).
        """
        # Send the command and receive the initial header response.
        header = self.send_recv({"cmd": "file_pull", "path": remote_path})
        if header.get("status") != "ok":
            return header

        # Now receive binary chunks + done frame with MD5 verification.
        return self.recv_file_pull(local_path)

    def file_pull_resumable(
        self,
        remote_path: str,
        local_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """
        Pull a file with resume support.

        If *local_path* already exists (e.g. from a prior interrupted
        transfer), sends the current file size as ``offset`` so the APK
        skips already-transferred bytes.  Binary chunks are appended to
        the existing file.

        The APK protocol for resumable pulls:
        1. Client sends ``file_pull`` with optional ``offset`` (bytes already
           received).
        2. APK responds with JSON header: ``size`` (total), ``offset`` (ack'd).
        3. APK streams binary chunks from the acknowledged offset.
        4. APK sends JSON done frame with ``md5`` of the *full* file.

        Parameters
        ----------
        remote_path:
            Absolute path on the Android device.
        local_path:
            Local file path.  If it exists, its size is used as the resume
            offset.
        progress_callback:
            Optional ``(bytes_received, total_size) -> None`` called after
            each chunk.

        Returns
        -------
        The done-frame dict (``"md5"``, ``"size"``, ``"resumed"``).
        """
        existing_bytes = 0
        if local_path.exists():
            existing_bytes = local_path.stat().st_size

        cmd: dict = {"cmd": "file_pull", "path": remote_path}
        if existing_bytes > 0:
            cmd["offset"] = existing_bytes
            logger.info(
                "file_pull_resumable: resuming %s from offset %d",
                remote_path, existing_bytes,
            )

        header = self.send_recv(cmd)
        if header.get("status") != "ok":
            return header

        total_size = int(header.get("size", 0))
        ack_offset = int(header.get("offset", 0))

        # If the server acknowledged a different offset than we requested,
        # trust the server and truncate/pad accordingly.
        if ack_offset != existing_bytes and existing_bytes > 0:
            logger.warning(
                "file_pull_resumable: server ack offset %d != local %d; "
                "re-downloading from server offset",
                ack_offset, existing_bytes,
            )
            if ack_offset == 0:
                existing_bytes = 0  # full re-download

        md5_full = hashlib.md5()
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # If resuming, hash the existing portion first for full-file MD5
        if ack_offset > 0 and local_path.exists():
            with open(local_path, "rb") as fh:
                while True:
                    block = fh.read(_CHUNK_SIZE)
                    if not block:
                        break
                    md5_full.update(block)

        mode = "ab" if ack_offset > 0 else "wb"
        received = ack_offset

        with open(local_path, mode) as fh:
            while True:
                raw = self.recv_binary_frame()
                # Check if this is the JSON done frame
                try:
                    text = raw.decode("utf-8")
                    frame = json.loads(text)
                    if isinstance(frame, dict) and "status" in frame:
                        # Done frame — verify full-file MD5
                        remote_md5 = frame.get("md5", "")
                        local_md5 = md5_full.hexdigest()
                        frame["resumed"] = ack_offset > 0
                        frame["resumed_from"] = ack_offset
                        if remote_md5 and remote_md5 != local_md5:
                            logger.warning(
                                "file_pull_resumable MD5 mismatch: "
                                "remote=%s local=%s for %s",
                                remote_md5, local_md5, local_path,
                            )
                        else:
                            logger.debug(
                                "file_pull_resumable MD5 verified: %s "
                                "(resumed=%s, offset=%d)",
                                local_md5, ack_offset > 0, ack_offset,
                            )
                        return frame
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass

                # Binary chunk — append
                fh.write(raw)
                md5_full.update(raw)
                received += len(raw)

                if progress_callback is not None:
                    progress_callback(received, total_size)

    def file_push_resumable(
        self,
        local_path: Path,
        dest: str = "downloads",
        date_taken: int | None = None,
        server_has: int = 0,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """
        Push a local file with resume support.

        If the server already has partial data from a prior interrupted push,
        pass ``server_has`` (bytes the server confirmed receiving).  The client
        skips that many bytes before streaming.

        Parameters
        ----------
        local_path:
            Local file to push.
        dest:
            Destination on device (``"photos"``, ``"videos"``, ``"downloads"``).
        date_taken:
            Optional creation timestamp in epoch milliseconds.
        server_has:
            Bytes the server already has from a prior attempt.
        progress_callback:
            Optional ``(bytes_sent, total_size) -> None``.

        Returns
        -------
        The done-frame dict.
        """
        if not local_path.exists():
            raise FileNotFoundError(f"file_push_resumable: {local_path} does not exist")

        file_size = local_path.stat().st_size
        cmd: dict = {
            "cmd": "file_push",
            "filename": local_path.name,
            "size": file_size,
            "dest": dest,
        }
        if server_has > 0:
            cmd["offset"] = server_has
        if date_taken is not None:
            cmd["date_taken"] = date_taken

        ready = self.send_recv(cmd)
        if ready.get("status") != "ok":
            return ready

        ack_offset = int(ready.get("offset", 0))

        md5 = hashlib.md5()
        sent = 0

        with open(local_path, "rb") as fh:
            # Hash and skip bytes the server already has
            skipped = 0
            while skipped < ack_offset:
                block = fh.read(min(_CHUNK_SIZE, ack_offset - skipped))
                if not block:
                    break
                md5.update(block)
                skipped += len(block)

            if skipped != ack_offset:
                logger.warning(
                    "file_push_resumable: file shorter than ack offset "
                    "(expected %d, got %d) for %s — sending from actual position",
                    ack_offset, skipped, local_path,
                )

            # Stream remaining bytes
            while True:
                chunk = fh.read(_CHUNK_SIZE)
                if not chunk:
                    break
                md5.update(chunk)
                self.send_binary_frame(chunk)
                sent += len(chunk)
                if progress_callback is not None:
                    progress_callback(ack_offset + sent, file_size)

        local_md5 = md5.hexdigest()
        done = self.recv()
        remote_md5 = done.get("md5", "")
        done["resumed"] = ack_offset > 0
        done["resumed_from"] = ack_offset

        if remote_md5 and remote_md5 != local_md5:
            logger.warning(
                "file_push_resumable MD5 mismatch: local=%s remote=%s for %s",
                local_md5, remote_md5, local_path,
            )
        elif remote_md5:
            logger.debug(
                "file_push_resumable MD5 verified: %s (resumed=%s)",
                local_md5, ack_offset > 0,
            )

        return done

    def file_push(
        self,
        local_path: Path,
        dest: str = "downloads",
        date_taken: int | None = None,
    ) -> dict:
        """
        Push a local file to the device via the companion APK.

        Sends ``file_push`` header, waits for "ready", streams binary
        chunks, then reads the done frame and verifies the MD5 checksum.

        Parameters
        ----------
        local_path:
            Local file to push.
        dest:
            Destination on device: ``"photos"``, ``"videos"``, or
            ``"downloads"`` (default).
        date_taken:
            Optional creation timestamp in epoch milliseconds.

        Returns
        -------
        The final done-frame dict (contains ``"md5"``, ``"bytes_received"``).
        """
        if not local_path.exists():
            raise FileNotFoundError(f"file_push: {local_path} does not exist")

        file_size = local_path.stat().st_size
        cmd: dict = {
            "cmd": "file_push",
            "filename": local_path.name,
            "size": file_size,
            "dest": dest,
        }
        if date_taken is not None:
            cmd["date_taken"] = date_taken

        # Send the header and wait for "ready" acknowledgement
        ready = self.send_recv(cmd)
        if ready.get("status") != "ok":
            return ready

        # Stream the file in chunks, computing MD5 as we go
        md5 = hashlib.md5()
        with open(local_path, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK_SIZE)
                if not chunk:
                    break
                md5.update(chunk)
                self.send_binary_frame(chunk)

        local_md5 = md5.hexdigest()

        # Read the done frame
        done = self.recv()
        remote_md5 = done.get("md5", "")
        if remote_md5 and remote_md5 != local_md5:
            logger.warning(
                "file_push MD5 mismatch: local=%s remote=%s for %s",
                local_md5, remote_md5, local_path,
            )
        elif remote_md5:
            logger.debug(
                "file_push MD5 verified: %s for %s", local_md5, local_path,
            )

        return done


# ---------------------------------------------------------------------------
# Module-level helpers for ADB port forwarding
# ---------------------------------------------------------------------------

def verify_companion_identity(
    adb: "ADBManager",
    serial: str,
    port: int = 7337,
) -> bool:
    """
    Verify that the process listening on *port* on *serial* is the expected
    companion package.

    Strategy:
    1. Resolve the companion package UID via ``dumpsys package``.
    2. Find which UID owns the listening port via ``/proc/net/tcp6`` (and
       ``/proc/net/tcp`` as fallback).
    3. Compare.  Mismatch → a different process grabbed the port first.

    Returns ``True`` if the check passes or cannot be performed (non-blocking
    on devices that restrict ``/proc/net``).  Returns ``False`` only when
    a clear UID mismatch is detected, which blocks the ADB forward.
    """
    # Step 1: companion package UID
    pkg_uid: int | None = None
    try:
        stdout, _, rc = adb.shell(
            serial, f"dumpsys package {_COMPANION_PKG}", timeout=10,
        )
        if rc == 0:
            m = re.search(r'\buserId=(\d+)\b', stdout)
            if m:
                pkg_uid = int(m.group(1))
    except Exception as exc:
        logger.debug("verify_companion_identity: package dump failed: %s", exc)

    if pkg_uid is None:
        logger.debug(
            "verify_companion_identity: %s not installed or UID unavailable "
            "— skipping identity check", _COMPANION_PKG,
        )
        return True  # non-blocking when package isn't installed

    # Step 2: UID owning the listening port
    port_hex = f"{port:04X}"
    port_owner_uid: int | None = None
    for proc_file in ("tcp6", "tcp"):
        try:
            stdout, _, rc = adb.shell(
                serial, f"cat /proc/net/{proc_file}", timeout=10,
            )
            if rc != 0:
                continue
            for line in stdout.splitlines():
                parts = line.split()
                if len(parts) < 8:
                    continue
                # Column 1: "local_address:port" in hex, e.g. "0000...0001:1CB9"
                if parts[1].upper().endswith(f":{port_hex}"):
                    try:
                        port_owner_uid = int(parts[7])
                    except ValueError:
                        pass
                    break
            if port_owner_uid is not None:
                break
        except Exception as exc:
            logger.debug(
                "verify_companion_identity: /proc/net/%s read failed: %s",
                proc_file, exc,
            )

    if port_owner_uid is None:
        # Port not yet open — companion app may not have started its server yet
        logger.debug(
            "verify_companion_identity: port %d not found in /proc/net "
            "— skipping check (companion server may not be running yet)", port,
        )
        return True

    if port_owner_uid != pkg_uid:
        logger.error(
            "verify_companion_identity: port %d is owned by UID %d but "
            "%s has UID %d — possible companion impersonation; "
            "blocking ADB forward",
            port, port_owner_uid, _COMPANION_PKG, pkg_uid,
        )
        return False

    logger.debug(
        "verify_companion_identity: port %d verified as %s (UID %d)",
        port, _COMPANION_PKG, pkg_uid,
    )
    return True


def setup_adb_forward(
    adb: "ADBManager",
    serial: str,
    port: int = 7337,
) -> bool:
    """
    Establish an ADB TCP port forward so that ``localhost:<port>`` on the
    host routes to ``<port>`` on the Android device.

    Performs a companion identity check first: if a different process is
    already listening on *port*, the forward is refused and ``False`` is
    returned.

    Parameters
    ----------
    adb:
        An initialised :class:`~core.adb_manager.ADBManager` instance.
    serial:
        ADB device serial string.
    port:
        Port number to forward (both sides).  Defaults to 7337.

    Returns
    -------
    ``True`` if the forward was created successfully, ``False`` otherwise.
    """
    if not verify_companion_identity(adb, serial, port):
        return False
    ok = adb.forward(serial, port, port)
    if ok:
        logger.debug("ADB forward established: localhost:%d -> device:%d on %s", port, port, serial)
    else:
        logger.error("Failed to establish ADB forward on port %d for %s", port, serial)
    return ok


def teardown_adb_forward(
    adb: "ADBManager",
    serial: str,
    port: int = 7337,
) -> None:
    """
    Remove the ADB TCP port forward created by :func:`setup_adb_forward`.

    Exceptions are silently swallowed so this is always safe to call in a
    finally block.

    Parameters
    ----------
    adb:
        An initialised :class:`~core.adb_manager.ADBManager` instance.
    serial:
        ADB device serial string.
    port:
        Host-side port to remove.  Defaults to 7337.
    """
    try:
        adb.forward_remove(serial, port)
        logger.debug("ADB forward removed: localhost:%d on %s", port, serial)
    except Exception as exc:  # noqa: BLE001
        logger.debug("teardown_adb_forward suppressed exception: %s", exc)
