"""
adb_manager.py

Thin, ergonomic wrapper around bin/adb/adb.exe subprocess calls.

All public methods return a (stdout, stderr, returncode) tuple so callers
can inspect results without catching exceptions.  Exceptions from subprocess
itself (TimeoutExpired, FileNotFoundError) are caught and translated into
non-zero return codes with the error message in stderr.

Usage
-----
    from core.config_loader import get_config
    from core.adb_manager import ADBManager

    adb = ADBManager(get_config())
    stdout, stderr, rc = adb.shell("emulator-5554", "getprop ro.product.model")
    if rc == 0:
        print(stdout.strip())
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import Config, get_config

logger = logging.getLogger(__name__)


# Tuple alias used throughout for return values
ADBResult = tuple[str, str, int]


class ADBManager:
    """
    Wraps bin/adb/adb.exe with typed, logged methods.

    Every method that calls ADB returns ADBResult = (stdout, stderr, returncode).
    returncode == 0 indicates success; non-zero indicates failure.
    """

    def __init__(self, config: Config | None = None) -> None:
        self._cfg = config or get_config()
        self._adb = str(self._cfg.adb_exe)
        logger.debug("ADBManager initialised with binary: %s", self._adb)

    # -----------------------------------------------------------------------
    # Core execution helpers
    # -----------------------------------------------------------------------

    def run(self, *args: str, timeout: int = 30) -> ADBResult:
        """
        Execute 'adb <args>' and return (stdout, stderr, returncode).

        Example:
            stdout, stderr, rc = adb.run("version")
        """
        cmd = [self._adb, *args]
        return self._execute(cmd, timeout=timeout)

    def run_device(self, serial: str, *args: str, timeout: int = 30) -> ADBResult:
        """
        Execute 'adb -s <serial> <args>' and return (stdout, stderr, returncode).

        Example:
            stdout, stderr, rc = adb.run_device("emulator-5554", "get-state")
        """
        cmd = [self._adb, "-s", serial, *args]
        return self._execute(cmd, timeout=timeout)

    def shell(self, serial: str, cmd: str, timeout: int = 30) -> ADBResult:
        """
        Execute 'adb -s <serial> shell <cmd>' and return (stdout, stderr, returncode).

        Example:
            stdout, stderr, rc = adb.shell("emulator-5554", "ls /sdcard")
        """
        full_cmd = [self._adb, "-s", serial, "shell", cmd]
        return self._execute(full_cmd, timeout=timeout)

    def shell_root(self, serial: str, cmd: str, timeout: int = 30) -> ADBResult:
        """
        Execute 'adb -s <serial> shell su -c <cmd>' for rooted devices.

        The command is passed as a single quoted argument to su -c.

        Example:
            stdout, stderr, rc = adb.shell_root("serial", "cat /data/data/com.example/db.sqlite")
        """
        # Wrap the inner command in single quotes to pass it intact to su
        full_cmd = [self._adb, "-s", serial, "shell", "su", "-c", cmd]
        return self._execute(full_cmd, timeout=timeout)

    # -----------------------------------------------------------------------
    # File transfer
    # -----------------------------------------------------------------------

    def push(
        self,
        serial: str,
        local: Path,
        remote: str,
        timeout: int = 120,
    ) -> bool:
        """
        Push a local file or directory to the device.
        Returns True on success, False on failure.
        """
        if not local.exists():
            logger.error("push: local path does not exist: %s", local)
            return False

        stdout, stderr, rc = self._execute(
            [self._adb, "-s", serial, "push", str(local), remote],
            timeout=timeout,
        )
        if rc != 0:
            logger.error(
                "adb push failed (rc=%d) %s -> %s: %s", rc, local, remote, stderr
            )
            return False
        logger.debug("adb push OK: %s -> %s", local, remote)
        return True

    def pull(
        self,
        serial: str,
        remote: str,
        local: Path,
        timeout: int = 120,
    ) -> bool:
        """
        Pull a file or directory from the device to a local path.
        The local parent directory is created if it does not exist.
        Returns True on success, False on failure.
        """
        local.parent.mkdir(parents=True, exist_ok=True)
        stdout, stderr, rc = self._execute(
            [self._adb, "-s", serial, "pull", remote, str(local)],
            timeout=timeout,
        )
        if rc != 0:
            logger.error(
                "adb pull failed (rc=%d) %s -> %s: %s", rc, remote, local, stderr
            )
            return False
        logger.debug("adb pull OK: %s -> %s", remote, local)
        return True

    def pull_verified(
        self,
        serial: str,
        remote: str,
        local: Path,
        timeout: int = 120,
    ) -> bool:
        """
        Pull a file from the device and verify its size matches the remote.

        After a successful ``adb pull``, queries ``stat -c %s`` on the device
        to get the remote file size and compares it against the local file
        size.  A mismatch indicates USB corruption or a truncated transfer;
        the local file is deleted and False is returned.

        Returns True only if the pull succeeded AND the sizes match.
        Falls back to a plain pull result if ``stat`` is unavailable.
        """
        if not self.pull(serial, remote, local, timeout=timeout):
            return False

        # Size verification via Android stat
        try:
            stdout, _, rc = self.shell(serial, f"stat -c %s '{remote}'", timeout=10)
            if rc == 0:
                remote_size = int(stdout.strip())
                local_size = local.stat().st_size
                if remote_size != local_size:
                    logger.warning(
                        "pull_verified: size mismatch for %s "
                        "(remote=%d local=%d) — discarding corrupt file",
                        remote, remote_size, local_size,
                    )
                    try:
                        local.unlink()
                    except OSError:
                        pass
                    return False
                logger.debug(
                    "pull_verified: size OK %d bytes for %s", local_size, remote
                )
        except (OSError, ValueError) as exc:
            logger.debug("pull_verified: stat check skipped for %s: %s", remote, exc)

        return True

    # -----------------------------------------------------------------------
    # Port forwarding
    # -----------------------------------------------------------------------

    def forward(
        self,
        serial: str,
        local_port: int,
        remote_port: int,
    ) -> bool:
        """
        Create a TCP port forward: host:<local_port> -> device:<remote_port>.

        Example:
            adb.forward("emulator-5554", 7777, 7777)
        Returns True on success.
        """
        stdout, stderr, rc = self._execute(
            [
                self._adb, "-s", serial,
                "forward",
                f"tcp:{local_port}",
                f"tcp:{remote_port}",
            ],
            timeout=15,
        )
        if rc != 0:
            logger.error(
                "adb forward failed (rc=%d) %d->%d on %s: %s",
                rc, local_port, remote_port, serial, stderr,
            )
            return False
        logger.debug(
            "adb forward OK: localhost:%d -> device:%d on %s",
            local_port, remote_port, serial,
        )
        return True

    def forward_remove(self, serial: str, local_port: int) -> bool:
        """Remove a previously created TCP port forward."""
        _, _, rc = self._execute(
            [self._adb, "-s", serial, "forward", "--remove", f"tcp:{local_port}"],
            timeout=10,
        )
        return rc == 0

    # -----------------------------------------------------------------------
    # APK installation
    # -----------------------------------------------------------------------

    def install_apk(
        self,
        serial: str,
        apk_path: Path,
        timeout: int = 60,
    ) -> bool:
        """
        Install an APK on the device.
        Uses -r (replace existing) and -d (allow downgrade) flags.
        Returns True on success.
        """
        if not apk_path.exists():
            logger.error("install_apk: APK not found: %s", apk_path)
            return False

        stdout, stderr, rc = self._execute(
            [self._adb, "-s", serial, "install", "-r", "-d", str(apk_path)],
            timeout=timeout,
        )
        # adb install can return rc=0 but still report FAILURE in stdout
        combined = stdout + stderr
        if rc != 0 or "Failure" in combined or "FAILED" in combined:
            logger.error(
                "APK install failed (rc=%d) %s on %s: %s",
                rc, apk_path.name, serial, combined,
            )
            return False
        logger.info("APK installed: %s on %s", apk_path.name, serial)
        return True

    def install_multiple(
        self,
        serial: str,
        apk_paths: list[Path],
        timeout: int = 180,
    ) -> bool:
        """
        Install a set of split APKs via 'adb install-multiple'.

        Required when an app ships as base.apk + config split APKs.
        Falls back to install_apk() if only a single APK is provided.
        Returns True on success.
        """
        if not apk_paths:
            logger.error("install_multiple: no APK paths provided")
            return False
        if len(apk_paths) == 1:
            return self.install_apk(serial, apk_paths[0], timeout=timeout)

        missing = [p for p in apk_paths if not p.exists()]
        if missing:
            logger.error("install_multiple: missing APKs: %s", missing)
            return False

        cmd = [self._adb, "-s", serial, "install-multiple", "-r", "-d"] + [
            str(p) for p in sorted(apk_paths)  # base.apk sorts first alphabetically
        ]
        stdout, stderr, rc = self._execute(cmd, timeout=timeout)
        combined = stdout + stderr
        if rc != 0 or "Failure" in combined or "FAILED" in combined:
            logger.error(
                "install-multiple failed (rc=%d) on %s: %s",
                rc, serial, combined,
            )
            return False
        logger.info(
            "APK install-multiple OK: %d splits on %s",
            len(apk_paths), serial,
        )
        return True

    # -----------------------------------------------------------------------
    # Device enumeration
    # -----------------------------------------------------------------------

    def devices(self) -> list[dict[str, str]]:
        """
        Return a parsed list of attached devices.

        Each entry is a dict with at minimum:
            serial   : str  — ADB serial / IP:port
            status   : str  — "device" | "offline" | "unauthorized" | ...
            product  : str  — from 'adb devices -l' (may be empty)
            model    : str  — from 'adb devices -l' (may be empty)
            transport: str  — from 'adb devices -l' (may be empty)
        """
        stdout, stderr, rc = self.run("devices", "-l", timeout=15)
        if rc != 0:
            logger.error("adb devices -l failed: %s", stderr)
            return []
        return _parse_devices_output(stdout)

    # -----------------------------------------------------------------------
    # Internal execution
    # -----------------------------------------------------------------------

    def _execute(self, cmd: list[str], timeout: int) -> ADBResult:
        """
        Run *cmd* as a subprocess and return (stdout, stderr, returncode).
        TimeoutExpired and FileNotFoundError are caught and mapped to rc=-1.
        """
        logger.debug("adb exec: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",   # ADB output can contain emoji/CJK; cp1252 (Windows
                errors="replace",   # default) crashes on byte values it can't decode.
                timeout=timeout,
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            msg = f"ADB command timed out after {timeout}s: {' '.join(cmd)}"
            logger.warning(msg)
            return "", msg, -1
        except FileNotFoundError as exc:
            msg = f"adb.exe not found at {self._adb}: {exc}"
            logger.error(msg)
            return "", msg, -1
        except Exception as exc:
            msg = f"Unexpected error running adb: {exc}"
            logger.error(msg)
            return "", msg, -1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_devices_output(output: str) -> list[dict[str, str]]:
    """
    Parse 'adb devices -l' output into a list of dicts.

    Expected format (after the header line):
        emulator-5554          device product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 ...
        192.168.1.5:5555       device product:... model:... transport_id:2
    """
    devices: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        status = parts[1]
        entry: dict[str, str] = {
            "serial": serial,
            "status": status,
            "product": "",
            "model": "",
            "transport": "",
        }
        # Parse key:value tokens from the rest of the line
        for token in parts[2:]:
            if ":" in token:
                k, _, v = token.partition(":")
                if k == "product":
                    entry["product"] = v
                elif k == "model":
                    entry["model"] = v
                elif k == "transport_id":
                    entry["transport"] = v
        devices.append(entry)
    return devices
