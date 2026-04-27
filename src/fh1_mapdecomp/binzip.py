"""Reader for Forza Horizon 1 bin.zip containers.

The file is a pseudo-zip: the central directory is standard PKZIP, but local
file headers are absent — entry data lives directly at ``header_offset`` for
``compressed_size`` bytes. Compression methods:

    0   stored — raw bytes.
    21  LZX (Xbox 360 flavour) with Turn 10's per-chunk framing.
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

from fh1_mapdecomp.lzx import DecompressionError, decode_lzx, strip_chunk_headers


@dataclass
class Entry:
    filename: str
    method: int
    header_offset: int
    compressed_size: int
    uncompressed_size: int


def list_entries(zip_path: os.PathLike) -> list[Entry]:
    out = []
    with zipfile.ZipFile(zip_path, "r") as z:
        for info in z.infolist():
            out.append(Entry(
                filename=info.filename,
                method=info.compress_type,
                header_offset=info.header_offset,
                compressed_size=info.compress_size,
                uncompressed_size=info.file_size,
            ))
    return out


def read_entry(zip_path: os.PathLike, entry: Entry) -> bytes:
    with open(zip_path, "rb") as f:
        f.seek(entry.header_offset)
        head = f.read(4)
        if head == b"PK\x03\x04":
            lfh_rest = f.read(26)
            fn_len = int.from_bytes(lfh_rest[22:24], "little")
            ex_len = int.from_bytes(lfh_rest[24:26], "little")
            f.seek(entry.header_offset + 30 + fn_len + ex_len)
            blob = f.read(entry.compressed_size)
        else:
            blob = head + f.read(entry.compressed_size - 4)
    if entry.method == 0:
        return blob
    if entry.method == 21:
        stream = strip_chunk_headers(blob, entry.uncompressed_size)
        return decode_lzx(stream, entry.uncompressed_size)
    raise DecompressionError(
        f"unsupported compression method {entry.method} for {entry.filename}"
    )


def resolve_binzip(source: Path) -> Path:
    """Accept either a bin.zip path directly, or a directory containing one.

    A FH1 install has many ``bin.zip`` files (one per UI track and per real
    track). When ``source`` is a broad directory we score candidates so the
    Colorado freeroam bin.zip wins over the small UI ones. Priority:
      1. ``source`` is a .zip file -> use it
      2. ``source/bin.zip`` exists -> use it (user pointed at the track dir)
      3. parent dir named ``colorado`` (case-insensitive)
      4. largest file size (Colorado is ~3 GB; UI bin.zips top out near 16 MB)
    """
    if source.is_file() and source.suffix.lower() == ".zip":
        return source
    if not source.is_dir():
        raise FileNotFoundError(f"could not find bin.zip under {source}")

    direct = source / "bin.zip"
    if direct.exists():
        return direct

    candidates = list(source.rglob("bin.zip"))
    if not candidates:
        raise FileNotFoundError(f"could not find bin.zip under {source}")

    def score(path: Path) -> tuple[int, int]:
        is_colorado = path.parent.name.lower() == "colorado"
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return (int(is_colorado), size)

    chosen = max(candidates, key=score)
    if len(candidates) > 1:
        try:
            rel = chosen.relative_to(source)
        except ValueError:
            rel = chosen
        print(f"[finder] picked {rel} from {len(candidates)} bin.zip candidates under {source}")
    return chosen
