"""Decoder for terrain PGEO bodies — "LightMap_XX_YY" tiles.

Terrain bodies don't carry vertex positions or heights — they're a
border-stitch strip table referencing a shared heightmap grid whose
storage isn't yet identified (see ``docs/pgeo-body.md`` §3.1 and the
roadmap's M1/M2 milestones). The roadmap's "ship v0 flat tiles"
guidance applies: this decoder emits a flat two-triangle quad at
``y = 0`` covering the tile's XZ bbox.

Body layout (file offsets; header is 52 B, body starts at 0x34):

    0x60  C-string  "LightMap_XX_YY\\0"
    0x70  2×f32 BE  lightmap origin (world x, z)
    0x78  f32 BE    1.0
    0x7c  u32 BE    102    (constant across samples)
    0x80  u32 BE    80     (constant)
    0x84  u32 BE    128    (likely lightmap texel resolution)
    0x88  2×f32 BE  bbox_min.x, bbox_min.z
    0x90  u32 BE    lightmap column (XX)
    0x94  u32 BE    lightmap row (YY)
    0x98  u32 BE    material / texture hash
    0x9c  u32 BE    n_strips
    0xa0  …         strip table, 8-byte stride, FFFF-prefix rows are inactive

Bbox.y is a ``±100000`` placeholder in every terrain tile, so the
header gives us no vertical extent. Flat at zero is correct-at-ground
until the heightmap source is resolved.
"""
from __future__ import annotations

import struct

import numpy as np

from fh1_mapdecomp.pgeo import PgeoHeader
from fh1_mapdecomp.pgeo_body import register
from fh1_mapdecomp.pgeo_body.types import MeshData


MATERIAL_HASH_OFFSET = 0x98
N_STRIPS_OFFSET = 0x9c
STRIP_TABLE_OFFSET = 0xa0
STRIP_STRIDE = 8


def decode(buf: bytes, header: PgeoHeader) -> MeshData | None:
    if len(buf) < STRIP_TABLE_OFFSET:
        return None

    mat_hash = struct.unpack_from(">I", buf, MATERIAL_HASH_OFFSET)[0]
    n_strips = struct.unpack_from(">I", buf, N_STRIPS_OFFSET)[0]
    expected_tail = STRIP_TABLE_OFFSET + n_strips * STRIP_STRIDE
    # Some tiles have a 4-byte trailer past the strip table; tolerate ±4.
    if not (expected_tail <= len(buf) <= expected_tail + 4):
        return None

    x0, _, z0 = header.bbox_min
    x1, _, z1 = header.bbox_max
    positions = np.array(
        [
            (x0, 0.0, z0),
            (x1, 0.0, z0),
            (x1, 0.0, z1),
            (x0, 0.0, z1),
        ],
        dtype=np.float32,
    )
    faces = np.array([(0, 1, 2), (0, 2, 3)], dtype=np.uint32)
    material_refs = np.array([mat_hash], dtype=np.uint32)
    return MeshData(positions=positions, faces=faces, material_refs=material_refs)


register("terrain", decode)
