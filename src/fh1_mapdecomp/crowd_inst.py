"""Extract inanimate-prop placements from ``crowd`` PGEO chunks.

A ``crowd`` PGEO (`pgeo.classify` returns ``"crowd"``) is a procedural-
instance container: one descriptor identifies the asset, then N × 12-byte
records carry packed positions. There is no inline mesh — the descriptor
is the asset key, expected to be resolved against the rmb pool at
runtime. See ``docs/pgeo-body.md §3.6`` for the full body layout.

Most crowd chunks describe **animated spectator scatter** (people),
which we explicitly skip — the rig / animation pipeline is out of
scope. Inanimate descriptors (festival metal-stanchion barriers,
booth stalls, festival stages, grandstand supports) survive the
filter. Each is mapped to a single representative rmb-pool handle
and the recovered positions become per-instance translations.

Output schema matches ``pvs_inst`` and ``collobjs_inst``:

    out/<dir>/index.json
        {blob_count, blobs: [{handle, npz, tag, vcount, tri_count, ...}],
         chunks: [{filename, name, origin, sections: [
             {handles, instances: [{pos, rot}]}
         ]}]}
    out/<dir>/blobs/m{NNNNN}.npz
         positions:(N,3) f32 game-space
         faces:    (M,3) u32

Orientation is currently emitted as identity. Bytes 4-7 of the
12-byte record are a packed orientation field whose encoding hasn't
been cracked yet (NOT a clean DEC3N normal — magnitudes 0.27..1.42;
records cluster in triples sharing a low-16-bit "group ID"). All
inanimate props rendered look correct positionally even with identity
rotation; barriers will all face the same way until the orientation
encoding is decoded.
"""
from __future__ import annotations

import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from fh1_mapdecomp.binzip import Entry, list_entries, read_entry
from fh1_mapdecomp.pgeo import classify, parse_header
from fh1_mapdecomp.rmb import parse_blob


ProgressFn = Optional[Callable[[int, int], None]]


# Per-instance record layout — see docs/pgeo-body.md §3.6.3.
INSTANCE_STRIDE = 12

# Inanimate-descriptor classifier. Visual-Blender pass against the
# 2026-05-03 import revealed that descriptors like
# `crowd_proc_clrd_festivalcrowd_stages_*` /
# `*_grandstands_*` / `*_stalls_*` are NOT placing the prop meshes —
# they're placing the *spectator scatter* in front of those props.
# Volume confirms it (3,525 / 4,171 / 24,122 instances respectively —
# people-density, not prop-density). Their positions are real but the
# render target is animated crowd, which we explicitly skip.
#
# Only `_barriers_` is true inanimate-prop placement (489 instances
# total — matches the 4 cardinal barrier loops + 2 stub corners
# around the festival circle observed in
# `probes/out/crowd_descriptors.tsv`).
#
# `_ANIMATE_RE` is kept as a safety belt — even if a descriptor
# matches `_barriers_` it must not also be a "_crowd_" tag. Tested
# against all 1,778 unique Colorado descriptors.
_INANIMATE_RE = re.compile(r"_barriers_")
_ANIMATE_RE = re.compile(r"_crowd_")


# Descriptor-prefix → (rmb_tag, comment). The mapping is best-effort
# and should be revised once we have visual confirmation in Blender.
# Each maps to a single rmb-pool tag; the pool resolver picks the
# first-seen handle for that tag.
#
# `OBJ_FEST_BarrierMetal_LOD00_` is the metal-stanchion-with-rope
# crowd-control barrier used at festival edges (vcount=384, centroid
# (0, 0.56, 0) — a 1.1 m post sitting on the ground at local origin,
# perfect for instancing). The rmb pool ships ~10 byte-identical
# copies of this tag at different handles; first-seen-wins.
# The earlier mapping `O_FEST_BarrierPlastic` placed the orange
# road-construction barrier instead — wrong asset.
DESCRIPTOR_RMB_MAP: list[tuple[str, str, str]] = [
    # category-substring  rmb_tag                       comment
    ("_barriers_",        "OBJ_FEST_BarrierMetal_LOD00_",
     "festival metal-stanchion crowd-control barriers"),
]


@dataclass
class _ParsedCrowd:
    desc: str
    desc_off: int
    bbox_off: int
    rec_off: int
    count: int
    positions: np.ndarray  # (N, 3) f32 game-space (Forza Y-up)


def _parse_crowd(buf: bytes) -> Optional[_ParsedCrowd]:
    """Parse a crowd PGEO body.

    Returns ``None`` for any of: not a crowd PGEO, malformed prolog,
    descriptor not findable, restated bbox not findable (the 11
    Format-C outliers — see §3.6.4), record region overflow.

    ``positions`` are world-space Y-up. Run them through the importer's
    basis swap to land in Blender Z-up.
    """
    if len(buf) < 0x80:
        return None
    if struct.unpack_from(">I", buf, 4)[0] != 42: return None
    if struct.unpack_from(">I", buf, 0x30)[0] != 3: return None
    bmin = struct.unpack_from(">fff", buf, 0x10)
    bmax = struct.unpack_from(">fff", buf, 0x20)
    if struct.unpack_from(">I", buf, 0x48)[0] != len(buf):
        return None  # 0x14 invariant — see §1.2

    # Descriptor: scan body for the first 'crowd' / 'CROWD' / 'FESTI'
    # anchor between 0x5c and 0xb0. Format A puts it at 0x60; Format B
    # at 0x98. Both are handled by this scan.
    desc_off = None
    for off in range(0x5c, min(len(buf) - 5, 0xb0)):
        b5 = buf[off:off + 5]
        if b5 in (b"crowd", b"CROWD", b"FESTI"):
            desc_off = off
            break
    if desc_off is None:
        return None
    null = buf.find(b"\x00", desc_off)
    if null < 0:
        return None
    desc = buf[desc_off:null].decode("ascii", "replace")

    # Restated bbox-min: u32-aligned scan after descriptor null.
    # Tolerate ±1 m on X/Z and ±(bbox_height + 5 m) on Y to absorb
    # the 1-bit-of-mantissa drift between header and body copies.
    bbox_height = max(1.0, bmax[1] - bmin[1])
    start = (null + 4) & ~3
    bbox_off = None
    for off in range(start, len(buf) - 12, 4):
        try:
            vals = struct.unpack_from(">fff", buf, off)
        except struct.error:
            break
        if (abs(vals[0] - bmin[0]) < 1.0
                and abs(vals[2] - bmin[2]) < 1.0
                and abs(vals[1] - bmin[1]) < bbox_height + 5.0):
            bbox_off = off
            break
    if bbox_off is None:
        return None  # Format C — large-bbox / f32-explicit, deferred

    # bbox_min(12) pad(4) bbox_max(12) pad(4) -> count
    count_off = bbox_off + 32
    if count_off + 4 > len(buf):
        return None
    count = struct.unpack_from(">I", buf, count_off)[0]

    # ptr_a(4) pad(4) ptr_b(4) pad(4) marker(4) pad(8) -> records
    rec_off = count_off + 32
    if rec_off + count * INSTANCE_STRIDE > len(buf):
        return None

    if count == 0:
        return _ParsedCrowd(
            desc=desc, desc_off=desc_off, bbox_off=bbox_off,
            rec_off=rec_off, count=0,
            positions=np.zeros((0, 3), dtype=np.float32),
        )

    # Decode N × position records. The 12-byte record packs world position
    # as three u16 BE values, one per axis (NOT 10:10:10:2 as initially
    # assumed):
    #   bytes 0..1  u16 BE  X fraction of bbox X-extent
    #   bytes 2..3  u16 BE  Y fraction of bbox Y-extent
    #   bytes 4..5  u16 BE  Z fraction of bbox Z-extent
    #   bytes 6..7  u16 BE  packed orientation (semantics still TBD —
    #                       records cluster in triples sharing the same
    #                       byte 6, suggesting a quantized yaw + group ID)
    #   bytes 8..11 u32     flag (always 0x00000000 on barrier samples)
    #
    # Verified against the 6 fest_area3 barrier descriptors: per-chunk
    # Y-spread drops from a full-bbox 6 m (the broken 10:10:10:2 reading)
    # to 0.3..2.4 m — real terrain undulation across the chunk's
    # footprint, not noise. X and Z both vary, producing recognizable
    # barrier-perimeter curves around the festival circle (NE/NW/SE/SW
    # corners + 2 NE stubs).
    bmin_a = np.array(bmin, dtype=np.float64)
    rng = np.array([bmax[0] - bmin[0], bmax[1] - bmin[1], bmax[2] - bmin[2]],
                   dtype=np.float64)
    view = np.frombuffer(buf, dtype=np.uint8,
                         count=count * INSTANCE_STRIDE, offset=rec_off)
    records = view.reshape(count, INSTANCE_STRIDE)
    x16 = (records[:, 0].astype(np.uint32) << 8) | records[:, 1].astype(np.uint32)
    y16 = (records[:, 2].astype(np.uint32) << 8) | records[:, 3].astype(np.uint32)
    z16 = (records[:, 4].astype(np.uint32) << 8) | records[:, 5].astype(np.uint32)
    positions = np.empty((count, 3), dtype=np.float32)
    positions[:, 0] = bmin_a[0] + x16.astype(np.float64) / 65535.0 * rng[0]
    positions[:, 1] = bmin_a[1] + y16.astype(np.float64) / 65535.0 * rng[1]
    positions[:, 2] = bmin_a[2] + z16.astype(np.float64) / 65535.0 * rng[2]
    return _ParsedCrowd(
        desc=desc, desc_off=desc_off, bbox_off=bbox_off,
        rec_off=rec_off, count=count, positions=positions,
    )


def _classify_descriptor(desc: str) -> Optional[str]:
    """Return the inanimate category (currently only "barriers") or None.

    Filter: must match `_INANIMATE_RE` AND not match `_ANIMATE_RE`.
    """
    if _ANIMATE_RE.search(desc):
        return None
    if _INANIMATE_RE.search(desc):
        return "barriers"
    return None


def _resolve_rmb_handle(
    desc: str,
    rmb_pool: dict[str, tuple[int, Entry]],
) -> Optional[tuple[int, Entry, str]]:
    """Look up the rmb pool handle that should render this descriptor.

    Returns ``(handle, entry, rmb_tag)`` or ``None`` if no mapping
    matched. Handle is the index of the rmb.bin entry in bin.zip
    pool order — disjoint from PVS/CollObjs handle namespaces because
    we ship in our own ``out/crowd_inst/blobs/`` directory.
    """
    for substr, rmb_tag, _ in DESCRIPTOR_RMB_MAP:
        if substr not in desc:
            continue
        rec = rmb_pool.get(rmb_tag)
        if rec is None:
            continue
        handle, entry = rec
        return handle, entry, rmb_tag
    return None


def _build_rmb_pool(
    zip_path: Path, progress: ProgressFn = None,
) -> dict[str, tuple[int, Entry]]:
    """Scan rmb.bin entries and return ``{tag: (pool_handle, entry)}``.

    Handle is the index of the rmb.bin entry within the bin.zip pool
    (matches the global rmb-pool handle convention used elsewhere).
    First-seen wins — duplicates across LOD/case-variant don't matter
    for our purposes.
    """
    rmb_entries: list[Entry] = [
        e for e in list_entries(zip_path) if "rmb.bin" in e.filename
    ]
    out: dict[str, tuple[int, Entry]] = {}
    needed = {tag for _, tag, _ in DESCRIPTOR_RMB_MAP}
    total = len(rmb_entries)
    for i, e in enumerate(rmb_entries):
        if progress and i % 4000 == 0:
            progress(i, total)
        # Early-exit once every needed tag is found.
        if needed.issubset(out.keys()):
            break
        try:
            buf = read_entry(zip_path, e)
            blob = parse_blob(buf)
        except Exception:
            continue
        if blob is None or not blob.tag:
            continue
        if blob.tag in needed and blob.tag not in out:
            out[blob.tag] = (i, e)
    if progress:
        progress(total, total)
    return out


def _emit_blob_npz(
    zip_path: Path, entry: Entry, dst: Path,
) -> tuple[int, int]:
    """Decode the rmb blob's geometry and write positions+faces npz.

    Returns ``(vcount, tri_count)``. Sub-blobs are merged the same way
    ``pvs_inst._merge_blob_geometry`` does so the prop renders as one
    object per instance.
    """
    buf = read_entry(zip_path, entry)
    blob = parse_blob(buf)
    if blob is None:
        raise ValueError(f"parse_blob returned None for {entry.filename}")

    pos_list = [blob.positions]
    tris_list = [s.triangles for s in blob.sections if s.triangles.size]
    vert_offset = int(blob.vcount)
    for sub in blob.sub_blobs:
        pos_list.append(sub.positions)
        for s in sub.sections:
            if s.triangles.size:
                tris_list.append(s.triangles + vert_offset)
        vert_offset += int(sub.vcount)
    positions = np.concatenate(pos_list, axis=0) if len(pos_list) > 1 else blob.positions
    if tris_list:
        tris = np.concatenate(tris_list, axis=0).astype(np.uint32, copy=False)
    else:
        tris = np.zeros((0, 3), dtype=np.uint32)
    np.savez(
        dst,
        positions=positions.astype(np.float32, copy=False),
        faces=tris,
    )
    return int(positions.shape[0]), int(tris.shape[0])


_IDENTITY_ROT = [
    [1.0, 0.0, 0.0],
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
]


def extract_crowd_instances(
    zip_path: Path,
    out_dir: Path,
    *,
    progress: ProgressFn = None,
) -> dict:
    """Build ``out_dir/{index.json, blobs/m*.npz}`` from crowd PGEOs.

    Iterates every ``.pgeo`` entry in ``bin.zip``; keeps only crowd-
    classified files whose descriptor matches an inanimate prop
    category (barriers, stalls, stages, grandstands). Every keeper
    contributes its packed positions to a per-rmb-handle section.
    Same-position duplicates from chunk-pool replication
    (e.g. ``__R00G00537.pgeo`` and ``__r00g00537.pgeo``) collapse via
    a (handle, x, y, z) set so each barrier post is placed once.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    blob_dir = out_dir / "blobs"
    blob_dir.mkdir(exist_ok=True)

    if progress:
        progress(0, 4)
    rmb_pool = _build_rmb_pool(zip_path, progress=None)
    if progress:
        progress(1, 4)

    # Walk every .pgeo entry, classify, parse, accumulate positions
    # per rmb handle.
    pgeo_entries = [
        e for e in list_entries(zip_path)
        if e.filename.lower().endswith(".pgeo")
    ]
    pgeo_entries.sort(key=lambda e: e.uncompressed_size)

    # handle -> (rmb_tag, set of (rounded_x, rounded_y, rounded_z))
    placed: dict[int, dict] = {}
    counts = {
        "scanned": 0,
        "classified_crowd": 0,
        "kept_inanimate": 0,
        "skipped_animate": 0,
        "skipped_uncategorized": 0,
        "skipped_unresolved": 0,
        "skipped_format_c": 0,
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
            if classify(h) != "crowd":
                continue
        except Exception:
            continue
        counts["classified_crowd"] += 1
        parsed = _parse_crowd(d)
        if parsed is None:
            counts["skipped_format_c"] += 1
            continue
        cat = _classify_descriptor(parsed.desc)
        if cat is None:
            if _ANIMATE_RE.search(parsed.desc):
                counts["skipped_animate"] += 1
            else:
                counts["skipped_uncategorized"] += 1
            continue
        resolved = _resolve_rmb_handle(parsed.desc, rmb_pool)
        if resolved is None:
            counts["skipped_unresolved"] += 1
            continue
        handle, entry, rmb_tag = resolved
        counts["kept_inanimate"] += 1
        counts["instances_decoded"] += parsed.count
        slot = placed.setdefault(handle, {
            "rmb_tag": rmb_tag,
            "rmb_entry": entry,
            "category": cat,
            "positions": set(),
            "descriptors": set(),
        })
        slot["descriptors"].add(parsed.desc)
        for p in parsed.positions:
            # Round to mm so byte-identical chunks dedupe; spatial tolerance
            # is irrelevant — packed positions are already at 0.025 m.
            key = (round(float(p[0]), 3),
                   round(float(p[1]), 3),
                   round(float(p[2]), 3))
            slot["positions"].add(key)
    if progress:
        progress(3, 4)

    # Emit blobs + sections.
    blobs_meta: list[dict] = []
    sections: list[dict] = []
    blob_failed: list[tuple[int, str]] = []
    for handle in sorted(placed.keys()):
        slot = placed[handle]
        npz_name = f"m{handle:05d}.npz"
        try:
            vcount, tri_count = _emit_blob_npz(
                zip_path, slot["rmb_entry"], blob_dir / npz_name,
            )
        except Exception as ex:
            blob_failed.append((handle, str(ex)))
            continue
        instances = [
            {"pos": list(pos), "rot": _IDENTITY_ROT}
            for pos in sorted(slot["positions"])
        ]
        blobs_meta.append({
            "handle": handle,
            "npz": f"blobs/{npz_name}",
            "tag": slot["rmb_tag"],
            "category": slot["category"],
            "vcount": vcount,
            "tri_count": tri_count,
            "descriptor_count": len(slot["descriptors"]),
        })
        sections.append({
            "handles": [handle],
            "instances": instances,
        })
        counts["instances_unique"] += len(instances)

    chunks = [{
        "filename": "crowd_pgeos",
        "name": "Crowd::Inanimate",
        "origin": [0.0, 0.0, 0.0],
        "sections": sections,
    }] if sections else []

    doc = {
        "source_zip": str(zip_path),
        "policy": "inanimate",
        "blob_count": len(blobs_meta),
        "chunk_count": 1 if sections else 0,
        "counts": counts,
        "rmb_pool_resolved": {
            tag: handle for tag, (handle, _) in rmb_pool.items()
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
    print(
        f"crowd_inst[inanimate]: {doc['blob_count']} unique meshes, "
        f"{c['instances_unique']} placed instances "
        f"(of {c['instances_decoded']} decoded; "
        f"chunk-pool dedupe collapsed {c['instances_decoded'] - c['instances_unique']})",
        file=stream,
    )
    print(
        f"  scanned: {c['scanned']} pgeo entries; "
        f"crowd-classified: {c['classified_crowd']}; "
        f"kept inanimate: {c['kept_inanimate']}",
        file=stream,
    )
    print(
        f"  skipped: {c['skipped_animate']} animate, "
        f"{c['skipped_uncategorized']} uncategorized, "
        f"{c['skipped_unresolved']} unresolved, "
        f"{c['skipped_format_c']} format-C outliers",
        file=stream,
    )
    if doc.get("blob_failed"):
        print(f"  {len(doc['blob_failed'])} blob parse failures", file=stream)
