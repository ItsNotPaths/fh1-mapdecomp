"""Extract foliage scatter placements from ``grass`` PGEO chunks.

A ``grass`` PGEO (engine class ``CProceduralVegetation``) is a per-cell
foliage-scatter container: a descriptor `Grass_Ungrouped_NNNN_0`,
n_minor "underlying terrain context" handles, a restated bbox, and N
per-instance 12-byte records carrying packed positions.

See ``docs/pgeo-body.md §3.4`` for the full body layout. Position
decoding is verified at 100% in-bbox across all 11,292 Colorado grass
chunks (1.05M instance positions).

Output schema matches ``pvs_inst`` / ``collobjs_inst`` / ``crowd_inst``.
The "blade-template" mesh hasn't been positively identified — the
n_minor handles inside each grass PGEO point at the *underlying terrain
patch* (e.g. `TERR_CLRD_ZONE09_AREA3_PATCH_046_LOD01`,
`Plains_Area4_PATCH_10_LOD01`), not at a grass-blade rmb. We therefore
pick a single representative grass-blade rmb (`Grass_LOD01`) from the
pool and use it as the placeholder mesh for every instance. The Blender
importer instances it via geometry nodes so 1M placements stay cheap.

Per-instance orientation is currently emitted as identity. Bytes 6..7
of each 12-byte record carry a packed orientation/yaw value whose
encoding hasn't been cracked yet (semantics TBD); positions render
correctly without it for placement-visualisation purposes.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from fh1_mapdecomp.binzip import Entry, list_entries, read_entry
from fh1_mapdecomp.pgeo import classify, parse_header
from fh1_mapdecomp.rmb import parse_blob


ProgressFn = Optional[Callable[[int, int], None]]


# Per-instance record stride (see docs/pgeo-body.md §3.4).
INSTANCE_STRIDE = 12
SENTINEL = 0xffff

# Placeholder mesh used for every grass instance. The
# template→rmb-handle lookup table isn't stored in the grass PGEO
# (verified 2026-05-03: scanned all 11k trailing blocks; top
# Grass-tagged handle hit was 0.4% of files — handles live in an
# external runtime/zone registry we haven't decoded). Until that's
# found we ship a synthetic bright-yellow vertical cylinder so the
# placement positions are obvious in Blender for visual validation.
PLACEHOLDER_TAG = "GrassPlaceholderCylinder"
PLACEHOLDER_HEIGHT = 0.8  # metres — tall enough to spot from a flyover
PLACEHOLDER_RADIUS = 0.15
PLACEHOLDER_SEGMENTS = 8


def _scan_records_start(
    buf: bytes, start: int, end: int, min_records: int = 3,
) -> int | None:
    """Find first offset in ``[start, end)`` where ``min_records``
    consecutive 12-byte records all carry 0xffff at +10..+11.

    The 0xffff sentinel is the strongest single anchor — it survives all
    n_minor / count-field variation observed across 11,292 samples.
    """
    cap = end if end <= len(buf) - 12 * min_records else len(buf) - 12 * min_records
    for off in range(start, cap, 4):
        ok = True
        for i in range(min_records):
            o = off + i * 12
            if struct.unpack_from(">H", buf, o + 10)[0] != SENTINEL:
                ok = False
                break
        if ok:
            return off
    return None


def _parse_grass(buf: bytes) -> Optional[np.ndarray]:
    """Decode positions for a single grass PGEO body.

    Returns an ``(N, 3) float32`` array of world-space (Forza Y-up)
    positions, or ``None`` for a malformed / non-grass file.
    """
    if len(buf) < 0xc0:
        return None
    h = parse_header(buf)
    if classify(h) != "grass":
        return None
    if struct.unpack_from(">I", buf, 0x48)[0] != len(buf):
        return None  # 0x14 invariant — see docs/pgeo-body.md §1.2

    null_off = buf.find(b"\x00", 0x60, 0x60 + 96)
    if null_off < 0:
        return None

    n_minor = struct.unpack_from(">I", buf, 0x40)[0]
    cursor = (null_off + 4) & ~3  # 4-byte align past null + 1-byte tail
    # Skip n_minor × (u32 BE handle, u32 BE pad).
    cursor += 8 * n_minor
    # Restated bbox (3×f32 + 4-byte pad, twice).
    if cursor + 32 > len(buf):
        return None
    cursor += 32
    if cursor + 8 > len(buf):
        return None
    count_a = struct.unpack_from(">I", buf, cursor)[0]
    cursor += 8  # skip count_a + count_b — count_b is post-record metadata

    # Find record start. count_a is the live-instance count; small files
    # need a low min-records threshold for the sentinel scan.
    min_recs = max(3, min(count_a if 0 < count_a < 10000 else 3, 10))
    rec_start = _scan_records_start(buf, cursor, cursor + 64,
                                     min_records=min_recs)
    if rec_start is None:
        return None

    # Decode contiguous 12B records until first non-sentinel record.
    rng_x = max(1e-6, h.bbox_max[0] - h.bbox_min[0])
    rng_y = max(1e-6, h.bbox_max[1] - h.bbox_min[1])
    rng_z = max(1e-6, h.bbox_max[2] - h.bbox_min[2])

    positions: list[tuple[float, float, float]] = []
    for i in range(count_a + 4):  # +4 slack; the loop breaks at first invalid record
        o = rec_start + i * 12
        if o + 12 > len(buf):
            break
        sx, sy, sz, _extra, _flag, sent = struct.unpack_from(">HHHHHH", buf, o)
        if sent != SENTINEL:
            break
        x = h.bbox_min[0] + (sx / 65535.0) * rng_x
        y = h.bbox_min[1] + (sy / 65535.0) * rng_y
        z = h.bbox_min[2] + (sz / 65535.0) * rng_z
        positions.append((x, y, z))

    if not positions:
        return np.zeros((0, 3), dtype=np.float32)
    return np.asarray(positions, dtype=np.float32)


def _build_placeholder_cylinder() -> tuple[np.ndarray, np.ndarray]:
    """Return ``(positions, faces)`` for a vertical capped cylinder.

    Geometry sits with its base at the local origin (Forza Y-up world,
    so the cylinder grows along +Y). Sized for marker visibility:
    radius 0.15 m, height 0.8 m, 8 radial segments → 18 verts, 28 tris.
    """
    n = PLACEHOLDER_SEGMENTS
    angles = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    cx = np.cos(angles) * PLACEHOLDER_RADIUS
    cz = np.sin(angles) * PLACEHOLDER_RADIUS
    bot = np.stack([cx, np.zeros(n), cz], axis=1)
    top = np.stack([cx, np.full(n, PLACEHOLDER_HEIGHT), cz], axis=1)
    base_centre = np.array([[0.0, 0.0, 0.0]])
    top_centre = np.array([[0.0, PLACEHOLDER_HEIGHT, 0.0]])
    positions = np.vstack([bot, top, base_centre, top_centre]).astype(
        np.float32
    )
    base_centre_idx = 2 * n
    top_centre_idx = 2 * n + 1

    tris: list[tuple[int, int, int]] = []
    for i in range(n):
        j = (i + 1) % n
        # side: two triangles per segment
        tris.append((i, j, n + i))
        tris.append((j, n + j, n + i))
        # bottom fan
        tris.append((base_centre_idx, j, i))
        # top fan
        tris.append((top_centre_idx, n + i, n + j))
    faces = np.asarray(tris, dtype=np.uint32)
    return positions, faces


def _emit_placeholder_npz(dst: Path) -> tuple[int, int]:
    """Write the synthetic cylinder mesh as a ``positions+faces`` npz.

    Returns ``(vcount, tri_count)`` mirroring ``_emit_blob_npz``'s shape
    so callers can fill in the index.json metadata uniformly.
    """
    positions, faces = _build_placeholder_cylinder()
    np.savez(dst, positions=positions, faces=faces)
    return int(positions.shape[0]), int(faces.shape[0])


_IDENTITY_ROT = [
    [1.0, 0.0, 0.0],
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
]


def extract_grass_instances(
    zip_path: Path,
    out_dir: Path,
    *,
    progress: ProgressFn = None,
) -> dict:
    """Build ``out_dir/{index.json, blobs/m*.npz}`` from grass PGEOs.

    Walks every `.pgeo` entry, keeps grass-classified ones, accumulates
    decoded positions into a single section under one placeholder rmb
    handle. Same-position duplicates from chunk-pool replication
    (`__R00G05782.pgeo` / `__r00g05782.pgeo` etc.) collapse via a
    `(rounded_x, rounded_y, rounded_z)` set so each blade is placed
    once.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    blob_dir = out_dir / "blobs"
    blob_dir.mkdir(exist_ok=True)

    if progress:
        progress(0, 4)
    # No rmb-pool resolve — the placeholder is a synthetic cylinder.
    if progress:
        progress(1, 4)

    pgeo_entries = [
        e for e in list_entries(zip_path)
        if e.filename.lower().endswith(".pgeo")
    ]
    pgeo_entries.sort(key=lambda e: e.uncompressed_size)

    placed: set[tuple[float, float, float]] = set()
    counts = {
        "scanned": 0,
        "classified_grass": 0,
        "decoded_ok": 0,
        "decode_fail": 0,
        "instances_decoded": 0,
        "instances_unique": 0,
    }
    total = len(pgeo_entries)
    for idx, e in enumerate(pgeo_entries):
        if progress and idx % 2000 == 0:
            progress(1 + 2.0 * idx / max(1, total), 4)
        counts["scanned"] += 1
        try:
            d = read_entry(zip_path, e)
            h = parse_header(d)
            if classify(h) != "grass":
                continue
        except Exception:
            continue
        counts["classified_grass"] += 1
        try:
            positions = _parse_grass(d)
        except Exception:
            counts["decode_fail"] += 1
            continue
        if positions is None:
            counts["decode_fail"] += 1
            continue
        counts["decoded_ok"] += 1
        counts["instances_decoded"] += positions.shape[0]
        for p in positions:
            # Round to mm so byte-identical chunks dedupe; spatial tolerance
            # is irrelevant — packed positions are already at 0.025-1.5 m
            # precision per axis depending on chunk extent.
            placed.add((
                round(float(p[0]), 3),
                round(float(p[1]), 3),
                round(float(p[2]), 3),
            ))
    if progress:
        progress(3, 4)

    blobs_meta: list[dict] = []
    sections: list[dict] = []
    blob_failed: list[tuple[int, str]] = []

    if placed:
        # Synthetic cylinder marker — bright yellow material is applied
        # by the Blender importer (see VARIANT_COLORS["grass_inst"]).
        # Handle 0 is a sentinel: this isn't a real rmb-pool entry, it's
        # an in-tree placeholder. Visual confirmation that the position
        # decode is right; swap to real meshes once the per-instance
        # template→handle table is found.
        handle = 0
        npz_name = f"m{handle:05d}.npz"
        try:
            vcount, tri_count = _emit_placeholder_npz(blob_dir / npz_name)
        except Exception as ex:
            blob_failed.append((handle, str(ex)))
        else:
            instances = [
                {"pos": list(pos), "rot": _IDENTITY_ROT}
                for pos in sorted(placed)
            ]
            blobs_meta.append({
                "handle": handle,
                "npz": f"blobs/{npz_name}",
                "tag": PLACEHOLDER_TAG,
                "vcount": vcount,
                "tri_count": tri_count,
                "is_placeholder": True,
            })
            sections.append({
                "handles": [handle],
                "instances": instances,
            })
            counts["instances_unique"] = len(instances)

    chunks = [{
        "filename": "grass_pgeos",
        "name": "Grass::Foliage",
        "origin": [0.0, 0.0, 0.0],
        "sections": sections,
    }] if sections else []

    doc = {
        "source_zip": str(zip_path),
        "policy": "freeroam",
        "blob_count": len(blobs_meta),
        "chunk_count": 1 if sections else 0,
        "counts": counts,
        "placeholder": {
            "kind": "synthetic_cylinder",
            "tag": PLACEHOLDER_TAG,
            "height": PLACEHOLDER_HEIGHT,
            "radius": PLACEHOLDER_RADIUS,
            "segments": PLACEHOLDER_SEGMENTS,
        },
        "blob_failed": blob_failed,
        "blobs": blobs_meta,
        "chunks": chunks,
    }
    (out_dir / "index.json").write_text(json.dumps(doc))
    if progress:
        progress(4, 4)
    return doc


def summarise(doc: dict, stream=sys.stderr) -> None:
    c = doc["counts"]
    placeholder = doc.get("placeholder") or {}
    print(
        f"grass_inst: {c['instances_unique']} placed instances "
        f"(of {c['instances_decoded']} decoded; "
        f"chunk-pool dedupe collapsed "
        f"{c['instances_decoded'] - c['instances_unique']})",
        file=stream,
    )
    print(
        f"  scanned: {c['scanned']} pgeo entries; "
        f"grass-classified: {c['classified_grass']}; "
        f"decoded: {c['decoded_ok']}; failed: {c['decode_fail']}",
        file=stream,
    )
    if placeholder:
        print(
            f"  placeholder mesh: {placeholder.get('kind')} "
            f"r={placeholder.get('radius')} h={placeholder.get('height')} "
            f"(temporary — for visual position validation)",
            file=stream,
        )
    if doc.get("blob_failed"):
        print(f"  {len(doc['blob_failed'])} blob parse failures",
              file=stream)
