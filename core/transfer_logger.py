"""
transfer_logger.py

Singleton logger for the transfer session.
Writes INFO+ to console and DEBUG+ to a timestamped log file.
Call get_logger() from anywhere — always returns the same instance.
Call reset() between test runs to force a fresh instance.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

_instance: Optional[logging.Logger] = None
_log_file_path: Optional[Path] = None


def get_logger(log_dir: Optional[str] = None) -> logging.Logger:
    """
    Return the session logger, creating it on first call.
    log_dir is only used on the first call; subsequent calls ignore it.
    """
    global _instance, _log_file_path

    if _instance is not None:
        return _instance

    logger = logging.getLogger("PhoneTransfer")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console — INFO and above
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # Resolve log directory
    if log_dir is None:
        try:
            from core.config_loader import get_config
            log_dir = str(get_config().project_root / "logs")
        except Exception:
            log_dir = "logs"

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file_path = log_path / f"transfer_{timestamp}.log"

    # File — DEBUG and above (full detail)
    file_handler = logging.FileHandler(_log_file_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _instance = logger
    return _instance


def get_log_file() -> Optional[Path]:
    """Return the path to the current session log file, or None if not started."""
    return _log_file_path


def reset() -> None:
    """
    Tear down the singleton and close all handlers.
    Primarily used between test runs.
    """
    global _instance, _log_file_path

    if _instance is not None:
        for handler in list(_instance.handlers):
            handler.close()
            _instance.removeHandler(handler)

    _instance = None
    _log_file_path = None
