"""Unpack FH1's ``default.xex`` to a flat PE image.

The retail XEX2 uses ``encryption_type=1`` (AES-128-CBC) and
``compression_type=1`` (basic) — both handled by ``xex2``'s built-in
pure-Python backends, so no external libraries are needed. The AES is
unoptimised; expect ~1 minute on the 20.9 MB body.

The decompressed image is written verbatim to ``<out>/default.bin`` so
later probes can grep it for ``(rmb_handle u32 BE, 3×f32 BE pos)``
runs (see ``docs/parsed-inventory.md`` § "Items left to scout" — the
PPC executable is the prime suspect for the 461 local-authored landmark
placements that bin.zip does not carry).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import xex2


def resolve_xex(source: Path) -> Path:
    """Accept a ``default.xex`` path or a directory containing one.

    The retail layout is ``<title_id>/00007000/<dlc_id>/default.xex``
    next to companion XEXes (``XMediaFacade_default.xex`` etc.). We pick
    the file literally named ``default.xex``.
    """
    if source.is_file():
        if source.suffix.lower() == ".xex" or xex2.is_xex(source):
            return source
        raise FileNotFoundError(f"{source} is not a XEX2 file")
    if not source.is_dir():
        raise FileNotFoundError(f"could not find default.xex under {source}")

    direct = source / "default.xex"
    if direct.exists():
        return direct

    candidates = [p for p in source.rglob("default.xex") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"could not find default.xex under {source}")
    if len(candidates) == 1:
        return candidates[0]

    chosen = max(candidates, key=lambda p: p.stat().st_size)
    try:
        rel = chosen.relative_to(source)
    except ValueError:
        rel = chosen
    print(
        f"[finder] picked {rel} from {len(candidates)} default.xex candidates under {source}",
        file=sys.stderr,
    )
    return chosen


def extract_xex(source: Path, out_dir: Path) -> dict:
    """Unpack the xex at ``source`` into ``out_dir``.

    Writes ``default.bin`` (the decompressed PE image) and
    ``index.json`` (load address, entry point, key used, format flags).
    Returns the index dict.
    """
    xex_path = resolve_xex(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[xex] unpacking {xex_path}", file=sys.stderr)
    print(
        "[xex] pure-Python AES — ~1 min on FH1's 20 MB body",
        file=sys.stderr,
    )
    image = xex2.unpack(xex_path)

    image_path = out_dir / "default.bin"
    image_path.write_bytes(image.image_bytes)

    info = {
        "source": str(xex_path),
        "image_path": str(image_path),
        "image_size": len(image.image_bytes),
        "image_base": image.image_base,
        "entry_point": image.entry_point,
        "module_flags": image.module_flags,
        "key_used": image.key_used,
        "encryption_type": image.probe.file_format.encryption_type,
        "compression_type": image.probe.file_format.compression_type,
    }
    (out_dir / "index.json").write_text(
        json.dumps(info, indent=2), encoding="utf-8"
    )
    return info


def summarise(info: dict) -> None:
    mb = info["image_size"] / (1024 * 1024)
    base = info["image_base"]
    entry = info["entry_point"] or 0
    key = info["key_used"]
    print(
        f"[xex] image {mb:.1f} MB at base 0x{base:08x} "
        f"(entry 0x{entry:08x}) key={key}",
        file=sys.stderr,
    )
