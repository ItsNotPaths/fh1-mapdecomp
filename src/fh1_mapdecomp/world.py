"""Assemble per-chunk world placement JSON from a bin.zip of PGEOs.

Each chunk's world-space bbox is in its own 52-byte header (see ``pgeo.py``),
so no indirect lookup against HEXY / PVSZLookup is needed for placement.

When ``meshes_dir`` is passed, decoded meshes for variants with a registered
``pgeo_body`` decoder are written alongside the JSON. The JSON records
``mesh`` as the filename (relative to ``meshes_dir``) for each chunk that
has a cached decode; the Blender import reads that entry and loads the
``.npz``, falling back to a bbox cube when ``mesh`` is absent.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from fh1_mapdecomp.binzip import Entry, list_entries, read_entry
from fh1_mapdecomp.pgeo import classify, parse_header
from fh1_mapdecomp.pgeo_body import decode as decode_body
from fh1_mapdecomp.pgeo_body.types import save as save_mesh


ProgressFn = Optional[Callable[[int, int], None]]


def build_placement(
    zip_path: Path,
    *,
    meshes_dir: Optional[Path] = None,
    progress: ProgressFn = None,
) -> dict:
    entries = [e for e in list_entries(zip_path) if e.filename.endswith(".pgeo")]
    chunks: list[dict] = []
    failed: list[tuple[str, str]] = []
    meshes_written = 0
    # bin.zip has 5-22x duplicate terrain pgeos per XZ cell (cross-ribbon
    # copies of identical LightMap blobs). Drop the repeats.
    seen_terrain_cells: set[tuple] = set()
    skipped_terrain_dup = 0

    if meshes_dir is not None:
        meshes_dir.mkdir(parents=True, exist_ok=True)

    for i, e in enumerate(entries):
        if progress and i % 2000 == 0:
            progress(i, len(entries))
        try:
            d = read_entry(zip_path, e)
            h = parse_header(d)
            variant = classify(h)
            if variant == "terrain":
                cell_key = (
                    round(h.bbox_min[0], 2), round(h.bbox_min[2], 2),
                    round(h.bbox_max[0], 2), round(h.bbox_max[2], 2),
                )
                if cell_key in seen_terrain_cells:
                    skipped_terrain_dup += 1
                    continue
                seen_terrain_cells.add(cell_key)
            cx = (h.bbox_min[0] + h.bbox_max[0]) * 0.5
            cy = (h.bbox_min[1] + h.bbox_max[1]) * 0.5
            cz = (h.bbox_min[2] + h.bbox_max[2]) * 0.5
            chunk: dict = {
                "filename": e.filename,
                "variant": variant,
                "version": h.version,
                "kind": h.kind,
                "scale": list(h.scale),
                "bbox_min": list(h.bbox_min),
                "bbox_max": list(h.bbox_max),
                "center": [cx, cy, cz],
                "compressed_size": e.compressed_size,
                "uncompressed_size": e.uncompressed_size,
                "header_offset": e.header_offset,
                "method": e.method,
            }
            if meshes_dir is not None:
                mesh = decode_body(variant, d, h)
                if mesh is not None:
                    name = f"{i:08d}.npz"
                    save_mesh(mesh, meshes_dir / name)
                    chunk["mesh"] = name
                    meshes_written += 1
            chunks.append(chunk)
        except Exception as ex:
            failed.append((e.filename, str(ex)))

    if progress:
        progress(len(entries), len(entries))

    return {
        "source": str(zip_path),
        "count": len(chunks),
        "failed": failed,
        "meshes_written": meshes_written,
        "skipped_terrain_dup": skipped_terrain_dup,
        "chunks": chunks,
    }


def write_placement(doc: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1))


def summarise(doc: dict, stream=sys.stderr) -> None:
    chunks = doc["chunks"]
    print(f"chunks: {len(chunks)} ok, {len(doc['failed'])} failed", file=stream)
    variant_counts = Counter(c["variant"] for c in chunks)
    for v, n in variant_counts.most_common():
        with_mesh = sum(1 for c in chunks if c["variant"] == v and "mesh" in c)
        tag = f" ({with_mesh} with mesh)" if with_mesh else ""
        print(f"  {v:18s} {n}{tag}", file=stream)
    if chunks:
        xs = [c["center"][0] for c in chunks]
        ys = [c["center"][1] for c in chunks]
        zs = [c["center"][2] for c in chunks]
        print("world extents:", file=stream)
        print(f"  X: {min(xs):10.1f} .. {max(xs):10.1f}", file=stream)
        print(f"  Y: {min(ys):10.1f} .. {max(ys):10.1f}", file=stream)
        print(f"  Z: {min(zs):10.1f} .. {max(zs):10.1f}", file=stream)
