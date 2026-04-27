"""Parsers for the per-ribbon placement indices.

Files live in ``media/tracks/colorado/Ribbon_NN/``:
  Colorado_NN.hex        HEXY grid: pitch, origin, tile coords, flags.
  PVSZLookup_NN.dat      ``(u32 hash, u32 zone_index)`` pairs.
  FilenameMap_NN.dat     ``u32 BE`` offsets into its own file; NUL-terminated
                         ASCII strings (the full list of assets on the ribbon).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


HEXY_MAGIC = b"HEXY"


@dataclass
class HexGrid:
    pitch: float
    origin: tuple[float, float]
    width: int
    height: int
    count: int
    version: int
    grid: list[int]
    tiles: list[tuple[int, int, int]]

    def tile_world(self, col: int, row: int) -> tuple[float, float]:
        return (self.origin[0] + col * self.pitch,
                self.origin[1] + row * self.pitch)


def parse_hexy(path: Path) -> HexGrid:
    b = Path(path).read_bytes()
    if b[:4] != HEXY_MAGIC:
        raise ValueError(f"bad magic: {b[:4]!r}")
    ver, = struct.unpack_from(">I", b, 4)
    pitch, ox, oz = struct.unpack_from(">fff", b, 8)
    count, = struct.unpack_from(">I", b, 20)
    W, H = struct.unpack_from(">II", b, 24)
    hdr = 32
    grid = list(struct.unpack_from(f">{W*H}I", b, hdr))
    coords_off = hdr + W * H * 4
    coords = struct.unpack_from(f">{count*2}I", b, coords_off)
    flags_off = coords_off + count * 8
    tiles = [(coords[i*2], coords[i*2+1], b[flags_off + i]) for i in range(count)]
    return HexGrid(pitch=pitch, origin=(ox, oz), width=W, height=H,
                   count=count, version=ver, grid=grid, tiles=tiles)


@dataclass
class PvszLookup:
    hash_to_zone: dict[int, int]

    @property
    def count(self) -> int:
        return len(self.hash_to_zone)


def parse_pvszlookup(path: Path) -> PvszLookup:
    b = Path(path).read_bytes()
    n = len(b) // 8
    out: dict[int, int] = {}
    for i in range(n):
        h, z = struct.unpack_from(">II", b, i * 8)
        out[h] = z
    return PvszLookup(hash_to_zone=out)


def parse_filenamemap(path: Path) -> list[str]:
    b = Path(path).read_bytes()
    first_off, = struct.unpack_from(">I", b, 0)
    n = first_off // 4
    offsets = struct.unpack_from(f">{n}I", b, 0)
    out = []
    for o in offsets:
        end = b.index(b"\0", o)
        out.append(b[o:end].decode("ascii", errors="replace"))
    return out
