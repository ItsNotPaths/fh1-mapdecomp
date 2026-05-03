"""BIX texture reader for FH1.

A BIX texture is split across two files:

    {hash}.bix    — 28-byte header + mip-tail (mips 1..N concatenated)
    {hash}_B.bix  — main texture (mip 0)

Both pieces use the same XG-tiled GPU layout as CAFF textures, and use
the same D3DFORMAT bitfield. We share the deswizzle code with
``caff.py``.

Header (28 bytes, all big-endian):

    u32 magic        = 0x42495831 ('BIX1') — also seen 0x42495830 'BIX0'
    u32 width
    u32 height
    u32 levels       (mip count)
    u32 format       (D3DFORMAT bitfield: low 6 bits = GPU format,
                      bits 6-7 = endian, bit 8 = tiled, etc.)
    u32 total_size   (entire pixel data including all mips)
    u32 main_size    (size of mip 0; lives in _B.bix)

The .bix file holds (total_size - main_size) bytes of mip-tail data
right after the header. We typically only want mip 0 (=highest
resolution), which is what `_B.bix` contains in its entirety.

Filename casing: bin.zip ships duplicate entries with different casing
(e.g. ``_0x00001F08_B.bix`` AND ``_0x00001f08_b.bix``). The pairing
below normalises to lowercase to dedupe.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

import numpy as np

from fh1_mapdecomp.caff import (
    TextureData,
    _calc_texture_size,
    _flip_byte_order,
    _untile_to_linear,
    to_dds,
)


_MAGIC_BIX0 = 0x42495830  # 'BIX0'
_MAGIC_BIX1 = 0x42495831  # 'BIX1'


@dataclass
class BixHeader:
    width: int
    height: int
    levels: int
    format: int
    total_size: int
    main_size: int


def parse_header(buf: bytes) -> BixHeader:
    """Parse a BIX header from the start of a .bix file."""
    if len(buf) < 28:
        raise ValueError("buffer too short for BIX header")
    magic, width, height, levels, fmt, total_size, main_size = struct.unpack_from(
        ">7I", buf, 0,
    )
    if magic not in (_MAGIC_BIX0, _MAGIC_BIX1):
        raise ValueError(f"not a BIX header (magic=0x{magic:08x})")
    return BixHeader(
        width=width, height=height, levels=levels, format=fmt,
        total_size=total_size, main_size=main_size,
    )


def decode_texture(bix_buf: bytes, b_bix_buf: bytes) -> Optional[TextureData]:
    """Decode a BIX texture given both halves.

    `bix_buf` is the .bix file (header + mip-tail).
    `b_bix_buf` is the _B.bix file (mip 0 raw pixels, GPU-tiled).

    Returns ``None`` if the format isn't supported by the shared
    decoder.
    """
    header = parse_header(bix_buf)
    if len(b_bix_buf) < header.main_size:
        # Allow ≤ — Forza occasionally pads down to a smaller stored size.
        return None

    # Pull mip 0 out of _B.bix. main_size may include trailing alignment
    # padding to GPU page boundary; the pixel-data window we untile is
    # _calc_texture_size(format, width, height).
    needed = _calc_texture_size(header.format, header.width, header.height)
    pixels_raw = np.frombuffer(b_bix_buf, dtype=np.uint8, count=needed)

    # Endian flip BEFORE detile (matches CAFF reader / FM3 reference).
    endian = (header.format >> 6) & 0x3
    if endian == 1:
        pixels_raw = _flip_byte_order(pixels_raw, 2)
    elif endian == 2:
        pixels_raw = _flip_byte_order(pixels_raw, 4)

    pixels_linear = _untile_to_linear(
        pixels_raw, header.width, header.height, header.format, 1,
    )

    return TextureData(
        width=header.width,
        height=header.height,
        format=header.format,
        levels=1,            # we only decode mip 0 for now
        pixels=pixels_linear.tobytes(),
    )


# Re-export so callers can use ``bix.to_dds(...)`` if convenient
__all__ = ["BixHeader", "parse_header", "decode_texture", "to_dds"]
