"""PGEO (Turn 10 geometry container) header parser.

The magic reads ``OEGP`` on disk because the Xbox 360 format is big-endian
and the ASCII tag is ``PGEO``. Header is 52 bytes and shared across variants;
``(version, kind)`` at offsets 4 and 48 selects the layout after byte 52.
See ``memory/project_fh1_extract.md`` for the observed variant taxonomy.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional


PGEO_MAGIC = b"OEGP"
HEADER_SIZE = 52


@dataclass
class PgeoHeader:
    version: int
    scale: tuple[float, float]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    kind: int


@dataclass
class Pgeo:
    header: PgeoHeader
    raw: bytes
    variant: str = ""


def parse_header(buf: bytes) -> PgeoHeader:
    if len(buf) < HEADER_SIZE:
        raise ValueError(f"buffer too short: {len(buf)} bytes")
    if buf[:4] != PGEO_MAGIC:
        raise ValueError(f"bad magic: {buf[:4]!r}")
    ver, = struct.unpack_from(">I", buf, 4)
    sx, sy = struct.unpack_from(">ff", buf, 8)
    bmin = struct.unpack_from(">fff", buf, 16)
    bmax = struct.unpack_from(">fff", buf, 32)
    kind, = struct.unpack_from(">I", buf, 48)
    return PgeoHeader(
        version=ver, scale=(sx, sy),
        bbox_min=bmin, bbox_max=bmax, kind=kind,
    )


def classify(h: PgeoHeader) -> str:
    return {
        (42, 2): "grass",
        (42, 3): "crowd",
        (42, 7): "v42k7",
        (42, 8): "terrain",
        (43, 6): "vegetation",
        (44, 4): "landmark_anim",
        (44, 5): "v44k5",
    }.get((h.version, h.kind), f"unknown_v{h.version}k{h.kind}")


def parse(buf: bytes) -> Pgeo:
    h = parse_header(buf)
    return Pgeo(header=h, raw=buf, variant=classify(h))


def _find_cstring(buf: bytes, start: int = HEADER_SIZE, max_scan: int = 512) -> Optional[tuple[int, str]]:
    end = min(len(buf), start + max_scan)
    i = start
    while i < end:
        if 32 <= buf[i] < 127:
            j = i
            while j < end and 32 <= buf[j] < 127:
                j += 1
            if j - i >= 4:
                return i, buf[i:j].decode("ascii", errors="replace")
            i = j
        else:
            i += 1
    return None


def dump(buf: bytes) -> str:
    h = parse_header(buf)
    tag = classify(h)
    s = _find_cstring(buf)
    out = [
        f"variant={tag}  ver={h.version} kind={h.kind} scale={h.scale}",
        f"bbox_min={h.bbox_min}  bbox_max={h.bbox_max}",
        f"size={len(buf)}",
    ]
    if s:
        out.append(f"first_string @0x{s[0]:04x}: {s[1]!r}")
    return "\n".join(out)
