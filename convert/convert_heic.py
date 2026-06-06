"""
convert_heic.py

HEIC/HEIF → JPEG conversion for Android-bound photo transfers.
Android does not natively decode HEIC on most devices below Android 13,
so all HEIC images from iOS must be converted before injection.

Requires: pip install pillow pillow-heif
"""

from pathlib import Path
from typing import Optional

_HEIF_REGISTERED = False


def _ensure_heif() -> None:
    """Register the HEIF opener with Pillow. Idempotent."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _HEIF_REGISTERED = True
    except ImportError:
        raise ImportError(
            "pillow-heif is required for HEIC conversion. "
            "Install it with: pip install pillow-heif"
        )


HEIC_EXTENSIONS = {".heic", ".heif"}


def convert(
    input_path: str,
    output_path: str,
    quality: int = 85,
) -> str:
    """
    Convert a single HEIC/HEIF image to JPEG.

    Args:
        input_path:  Source .heic or .heif file.
        output_path: Destination .jpg file (extension not enforced — caller's choice).
        quality:     JPEG quality 1-95 (default 85 balances size and fidelity).

    Returns:
        Absolute path to the output JPEG.

    Raises:
        FileNotFoundError  — input missing
        ImportError        — pillow-heif not installed
    """
    _ensure_heif()
    from PIL import Image

    input_p  = Path(input_path)
    output_p = Path(output_path)

    if not input_p.exists():
        raise FileNotFoundError(f"Input HEIC file not found: {input_path}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(str(input_p)) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(str(output_p), format="JPEG", quality=quality, optimize=True)

    return str(output_p.resolve())


def convert_batch(
    input_dir: str,
    output_dir: str,
    quality: int = 85,
    on_progress: Optional[callable] = None,
) -> list[dict]:
    """
    Convert all HEIC/HEIF files in a directory to JPEG.

    Args:
        input_dir:   Directory containing .heic / .heif files.
        output_dir:  Directory for output .jpg files (created if missing).
        quality:     JPEG quality (default 85).
        on_progress: Optional callback(current, total) called after each file.

    Returns:
        List of result dicts:
          {"input": str, "output": str|None, "success": bool, "error": str|None}
    """
    _ensure_heif()

    input_p  = Path(input_dir)
    output_p = Path(output_dir)
    output_p.mkdir(parents=True, exist_ok=True)

    heic_files: list[Path] = []
    for ext in HEIC_EXTENSIONS:
        heic_files.extend(input_p.glob(f"*{ext}"))
        heic_files.extend(input_p.glob(f"*{ext.upper()}"))

    results = []
    total = len(heic_files)

    for idx, heic_file in enumerate(heic_files, start=1):
        out_file = output_p / (heic_file.stem + ".jpg")
        try:
            convert(str(heic_file), str(out_file), quality=quality)
            results.append({
                "input":   str(heic_file),
                "output":  str(out_file),
                "success": True,
                "error":   None,
            })
        except Exception as exc:
            results.append({
                "input":   str(heic_file),
                "output":  None,
                "success": False,
                "error":   str(exc),
            })

        if on_progress:
            try:
                on_progress(idx, total)
            except Exception:
                pass

    return results
