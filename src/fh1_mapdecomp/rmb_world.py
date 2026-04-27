"""Extract world-placed rmb blobs at their header centroid.

Roughly 70% of the 102,012 rmb.bin entries are authored pre-placed in
world space: the ``blob_centre`` field at offset 0x7c is their world
position and the vertex positions are already world-space too. The
``bbox_min/bbox_max`` at 0x8c/0x9c are in LOCAL space (symmetric around
the origin), so "centroid lies outside the bbox" is a reliable
discriminator between the two kinds of blob.

Local-authored blobs (centroid ~ origin, vertices centred) are placed
separately by CollObjs.xml / GameObjs.xml / v42k7 and must be skipped
here to avoid double-placement.

Output layout (under ``out/rmb_world/``):

    index.json          {blobs: [{handle, npz, tag, lod, vcount,
                                  tri_count, centroid, bbox_min,
                                  bbox_max}]}
    blobs/HHHHHH.npz    positions:(N,3) f32 WORLD-space game coords
                        faces:(M,3) u32

The Blender importer places each blob at origin (verts already carry
the world transform). Y-up → Z-up is the only basis change applied.

TERR_* blobs are already emitted by the terrain_hi pipeline; they are
dropped here to avoid duplicate terrain. A ``freeroam`` tag-policy
drops race-event / festival dressing (Barrier_, Grandstand, Festival_,
FEST_, OBJ_FEST, Ambulance, Countdown, Shadow_Caster, PROC_cars) while
keeping everything else — the default is "if it's world-placed, it's
freeroam" because the authored positions *are* the free-roam world.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from fh1_mapdecomp.binzip import read_entry
from fh1_mapdecomp.rmb import list_rmb_entries, parse_blob


ProgressFn = Optional[Callable[[int, int], None]]


# Race / festival / event dressing — dropped under the 'freeroam' policy.
# The rmb-world set is mostly legit free-roam authored geometry, so we
# treat this as a drop-list (default keep) rather than a keep-list. TERR_
# is owned by the terrain_hi pipeline and always dropped here regardless
# of policy.
#
# Festival_ subgroup split (probe G, 2026-04-27): Festival_Area5_*,
# Festival_Area6_*, Festival_Road_* are permanent hub geometry with
# authored world centroids around (-1900, -300) — keep them. The
# per-race ribbon segments (Festival_Area01_* .. Festival_Area04_*)
# scatter along race routes and don't belong in free-roam.
_DROP_TAG_PREFIXES_FREEROAM: tuple[str, ...] = (
    "Barrier_",             # race-course barriers
    "Grandstand",
    "Festival_Area01_", "Festival_Area02_",
    "Festival_Area03_", "Festival_Area04_",
    "Festival_Area1_", "Festival_Area2_",
    "Festival_Area3_", "Festival_Area4_",
    "FEST_",
    "OBJ_FEST", "OBJ_Fest",
    "OBJ_BarrierBrand",
    "Ambulance",
    "Countdown",
    "Shadow_Caster",
    "PROC_cars",
    # Authored placement lives elsewhere; including them double-places.
    "CO_", "OBJ_CLRD_", "O_CO_",   # Ribbon_00/CollObjs.xml
    "Barnfind_",                    # Ribbon_00/GameObjs.xml
)
_ALWAYS_DROP_PREFIXES: tuple[str, ...] = (
    "TERR_",                # owned by terrain_hi
)

# Specific tags whose authored centroid passes the 20 m threshold by a
# small margin but whose real world placement source is unknown — they
# leak in as a pile near origin. Drop until we decode their proper
# placement layer. Probe H (2026-04-27) found ~73 such emissions; this
# list catches the dominant offenders.
_LOCAL_AUTHORED_LEAK_TAGS: frozenset[str] = frozenset({
    "Mesh008",
    "MAINTOWN_Warehouse1",
    "AuctionShowTent",
    "MiningTower02",
    "MOUN_GM_HillSideMine",
    "REDROCKS_amphitheatre",
    "Gondola",
    "MOUN_DAM_BLDG_MainDam",
    "MOUN_DAM_BLDG_Lower",
    "MOUN_DAM_BLDG_LowerB",
    "MOUN_DAM_BLDG_VisitorCentre",
    "MOUN_DAM_BLDG_ViewPlatform",
    "MOUN_DAM_Bridge",
    "MOUN_DAM_BLDG_InletTower",
    "MOUN_DAM_RoadWall1",
    "MOUN_DAM_POWERPLANT_Machinery",
    "MOUN_DAM_POWERPLANT_Pylon",
    "MOUN_DAM_STFN_Bollards",
    "MOUN_DAM_STFN_Transformer",
    "MOUN_DAM_Substation_Hut",
    "MOUN_DAM_Substation_MachineHut",
    "MOUN_DAM_Substation_Fencing",
})


# XZ-distance threshold for the "is this blob pre-placed?" decision.
# Validated against the full 102,012-entry Colorado pool: produces a clean
# 71,570 / 30,442 split. The bbox-containment discriminator was tried
# first but false-positives on local-authored signs/billboards whose
# authored pivot sits meters above or in front of the geometry (e.g.
# CO_CLRD_Sign_Deer has bbox ±0.3m but centroid Y=2.5m, Z=+5m).
_PLACEMENT_THRESHOLD_M = 20.0


def _is_world_placed(
    centroid: tuple[float, float, float],
    bbox_min: tuple[float, float, float],
    bbox_max: tuple[float, float, float],
) -> bool:
    """True if horizontal centroid magnitude exceeds the 20m threshold.

    Local-authored blobs (destined for CollObjs/v42k7 placement) have
    |centroid_XZ| < 20m — essentially centred on origin. World-placed
    blobs carry the world transform in their header centroid so the
    magnitude matches their XZ distance from the map origin.
    """
    cx, _cy, cz = centroid
    return (cx * cx + cz * cz) > (_PLACEMENT_THRESHOLD_M * _PLACEMENT_THRESHOLD_M)


def _strip_lod_suffix(tag: str) -> str:
    """Return tag without trailing '_LOD\\d\\d' or '_NOLOD' suffix."""
    for suf in ("_LOD00", "_LOD01", "_LOD02", "_LOD03", "_LOD04",
                "_NOLOD", "_MIDDIST"):
        i = tag.rfind(suf)
        if i >= 0:
            return tag[:i]
    return tag


def _policy_drop(tag: str, policy: str) -> bool:
    if any(tag.startswith(p) for p in _ALWAYS_DROP_PREFIXES):
        return True
    if policy == "freeroam":
        if _strip_lod_suffix(tag) in _LOCAL_AUTHORED_LEAK_TAGS:
            return True
        return any(tag.startswith(p) for p in _DROP_TAG_PREFIXES_FREEROAM)
    return False


def extract_rmb_world(
    zip_path: Path,
    out_dir: Path,
    *,
    policy: str = "freeroam",
    include_all_lods: bool = False,
    progress: ProgressFn = None,
) -> dict:
    if policy not in ("freeroam", "all"):
        raise ValueError(f"policy must be 'freeroam' or 'all', got {policy!r}")
    blob_dir = out_dir / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)

    rmb_entries = list_rmb_entries(zip_path)
    blobs_meta: list[dict] = []
    failed: list[tuple[int, str]] = []
    skipped_local = 0
    skipped_terr = 0
    skipped_policy: list[str] = []   # sample of dropped tags
    skipped_policy_count = 0
    skipped_lod = 0
    skipped_no_triangles = 0

    for handle, e in enumerate(rmb_entries):
        if progress and handle % 2000 == 0:
            progress(handle, len(rmb_entries))
        try:
            buf = read_entry(zip_path, e)
        except Exception as ex:
            failed.append((handle, f"read: {ex}"))
            continue
        blob = parse_blob(buf)
        if blob is None:
            failed.append((handle, "parse_blob None"))
            continue

        if not _is_world_placed(blob.centroid, blob.bbox_min, blob.bbox_max):
            skipped_local += 1
            continue

        if blob.tag.startswith("TERR_"):
            skipped_terr += 1
            continue

        if _policy_drop(blob.tag, policy):
            skipped_policy_count += 1
            if len(skipped_policy) < 20:
                skipped_policy.append(blob.tag)
            continue

        if not include_all_lods:
            # Keep LOD00 and LOD-less tags ("", e.g. "NoLOD" variants).
            # LOD01/02/MIDDIST duplicate the hero at near-identical pos
            # and just clutter the scene.
            if blob.lod not in ("", "00"):
                skipped_lod += 1
                continue
            if "MIDDIST" in blob.tag:
                skipped_lod += 1
                continue

        # Merge primary + sub-blob geometry into one mesh. Same pattern as
        # v42k7_inst: sub-blobs share the primary's coordinate frame and
        # hold the real triangles when the primary vbuf is a stub.
        pos_list = [blob.positions]
        tris_list = [s.triangles for s in blob.sections if s.triangles.size]
        vert_offset = int(blob.vcount)
        for sub in blob.sub_blobs:
            pos_list.append(sub.positions)
            for s in sub.sections:
                if s.triangles.size:
                    tris_list.append(s.triangles + vert_offset)
            vert_offset += int(sub.vcount)
        positions = (
            np.concatenate(pos_list, axis=0)
            if len(pos_list) > 1 else blob.positions
        )
        tris = (
            np.concatenate(tris_list, axis=0).astype(np.uint32, copy=False)
            if tris_list else np.zeros((0, 3), dtype=np.uint32)
        )

        if tris.shape[0] == 0:
            skipped_no_triangles += 1
            continue

        name = f"{handle:06d}.npz"
        np.savez_compressed(
            blob_dir / name,
            positions=positions.astype(np.float32, copy=False),
            faces=tris,
        )
        blobs_meta.append({
            "handle": handle,
            "npz": f"blobs/{name}",
            "tag": blob.tag,
            "lod": blob.lod,
            "vcount": int(positions.shape[0]),
            "tri_count": int(tris.shape[0]),
            "sub_blobs": len(blob.sub_blobs),
            "centroid": [
                float(blob.centroid[0]),
                float(blob.centroid[1]),
                float(blob.centroid[2]),
            ],
            "bbox_min": [
                float(blob.bbox_min[0]),
                float(blob.bbox_min[1]),
                float(blob.bbox_min[2]),
            ],
            "bbox_max": [
                float(blob.bbox_max[0]),
                float(blob.bbox_max[1]),
                float(blob.bbox_max[2]),
            ],
        })

    if progress:
        progress(len(rmb_entries), len(rmb_entries))

    doc = {
        "source": str(zip_path),
        "policy": policy,
        "rmb_pool_size": len(rmb_entries),
        "blob_count": len(blobs_meta),
        "skipped_local": skipped_local,
        "skipped_terrain": skipped_terr,
        "skipped_policy": skipped_policy_count,
        "skipped_policy_sample": skipped_policy,
        "skipped_lod": skipped_lod,
        "skipped_no_triangles": skipped_no_triangles,
        "failed": failed,
        "blobs": blobs_meta,
    }
    (out_dir / "index.json").write_text(json.dumps(doc))
    return doc


def summarise(doc: dict, stream=sys.stderr) -> None:
    n = doc["blob_count"]
    pool = doc["rmb_pool_size"]
    print(
        f"rmb_world[{doc['policy']}]: {n} world-placed blobs out of "
        f"{pool} pool entries",
        file=stream,
    )
    print(
        f"  skipped: {doc['skipped_local']} local, "
        f"{doc['skipped_terrain']} terrain (owned by terrain_hi), "
        f"{doc['skipped_policy']} by policy, "
        f"{doc['skipped_lod']} LOD01+/MIDDIST, "
        f"{doc['skipped_no_triangles']} no-triangles",
        file=stream,
    )
    fails = doc.get("failed", [])
    if fails:
        print(f"  failures: {len(fails)}", file=stream)
    if not n:
        return
    blobs = doc["blobs"]
    total_v = sum(b["vcount"] for b in blobs)
    total_t = sum(b["tri_count"] for b in blobs)
    xs = [b["centroid"][0] for b in blobs]
    ys = [b["centroid"][1] for b in blobs]
    zs = [b["centroid"][2] for b in blobs]
    print(
        f"  {total_v} vertices, {total_t} triangles across {n} placements",
        file=stream,
    )
    print("  centroid extents:", file=stream)
    print(f"    X: {min(xs):10.1f} .. {max(xs):10.1f}", file=stream)
    print(f"    Y: {min(ys):10.1f} .. {max(ys):10.1f}", file=stream)
    print(f"    Z: {min(zs):10.1f} .. {max(zs):10.1f}", file=stream)
    # Top tag prefixes by placement count.
    prefixes: dict[str, int] = {}
    for b in blobs:
        p = b["tag"].split("_")[0]
        prefixes[p] = prefixes.get(p, 0) + 1
    top = sorted(prefixes.items(), key=lambda kv: -kv[1])[:10]
    print(
        "  top prefixes: " + ", ".join(f"{p}={c}" for p, c in top),
        file=stream,
    )
