"""Extract high-detail terrain meshes from the rmb.bin geometry pool.

Terrain PGEOs only reference strip tables — the actual heightmap vertices
live in the shared ``rmb.bin`` pool as ``TERR_*_LOD00_*`` blobs. Each blob
is a point cloud of world-space f32 BE positions at full elevation; we
stage them as per-tile ``.npz`` files and let the Blender importer triangulate
the XZ projection into real surface meshes.

Output layout (under ``out/terrain_hi/``):

    index.json          {tiles: [{npz, tag, lod, filename, header_offset,
                                  vcount, bbox_min, bbox_max}, ...]}
    {i:06d}.npz         positions:(N,3) f32 game-space (Y-up)

No faces are generated here — a point cloud is cheaper to cache and the
Delaunay pass is easier to keep deterministic inside Blender where it's
actually consumed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from fh1_mapdecomp.rmb import decode_positions, iter_terr_blobs


ProgressFn = Optional[Callable[[int, int], None]]


def extract_terrain_hi(
    zip_path: Path,
    out_dir: Path,
    *,
    lod: str = "00",
    include_middist: bool = False,
    progress: ProgressFn = None,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles: list[dict] = []
    failed: list[tuple[str, str]] = []
    seen_tags: set[str] = set()
    skipped_dup = 0
    skipped_middist = 0
    written = 0

    for i, (entry, buf, blob) in enumerate(iter_terr_blobs(zip_path, lod=lod)):
        if progress and i % 500 == 0:
            progress(i, 0)
        if not include_middist and "MIDDIST" in blob.tag:
            skipped_middist += 1
            continue
        if blob.tag in seen_tags:
            skipped_dup += 1
            continue
        seen_tags.add(blob.tag)
        try:
            positions = decode_positions(buf, blob)
        except Exception as ex:
            failed.append((entry.filename, str(ex)))
            continue
        name = f"{written:06d}.npz"
        written += 1
        np.savez_compressed(out_dir / name, positions=positions.astype(np.float32, copy=False))
        bb_min = positions.min(axis=0)
        bb_max = positions.max(axis=0)
        tiles.append({
            "npz": name,
            "tag": blob.tag,
            "lod": blob.lod,
            "filename": entry.filename,
            "header_offset": entry.header_offset,
            "vcount": int(blob.vcount),
            "stride": int(blob.stride),
            "bbox_min": [float(bb_min[0]), float(bb_min[1]), float(bb_min[2])],
            "bbox_max": [float(bb_max[0]), float(bb_max[1]), float(bb_max[2])],
        })

    if progress:
        progress(len(tiles), len(tiles))

    doc = {
        "source": str(zip_path),
        "lod": lod,
        "count": len(tiles),
        "skipped_duplicate": skipped_dup,
        "skipped_middist": skipped_middist,
        "failed": failed,
        "tiles": tiles,
    }
    index_path = out_dir / "index.json"
    index_path.write_text(json.dumps(doc, indent=1))
    return doc


def summarise(doc: dict, stream=sys.stderr) -> None:
    tiles = doc["tiles"]
    extras = []
    if doc.get("skipped_duplicate"):
        extras.append(f"{doc['skipped_duplicate']} dup")
    if doc.get("skipped_middist"):
        extras.append(f"{doc['skipped_middist']} MIDDIST")
    tag = f" ({', '.join(extras)} skipped)" if extras else ""
    print(f"terrain_hi: {len(tiles)} tiles ok, {len(doc['failed'])} failed{tag}", file=stream)
    if not tiles:
        return
    total_v = sum(t["vcount"] for t in tiles)
    xs = [t["bbox_min"][0] for t in tiles] + [t["bbox_max"][0] for t in tiles]
    ys = [t["bbox_min"][1] for t in tiles] + [t["bbox_max"][1] for t in tiles]
    zs = [t["bbox_min"][2] for t in tiles] + [t["bbox_max"][2] for t in tiles]
    print(f"  {total_v} vertices across {len(tiles)} tiles", file=stream)
    print("  extents:", file=stream)
    print(f"    X: {min(xs):10.1f} .. {max(xs):10.1f}", file=stream)
    print(f"    Y: {min(ys):10.1f} .. {max(ys):10.1f}", file=stream)
    print(f"    Z: {min(zs):10.1f} .. {max(zs):10.1f}", file=stream)
