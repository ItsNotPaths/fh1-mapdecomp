"""Extract world-placed rmb blobs at their header centroid, with
per-section labelling for wrappers that carry foreign asset tags.

Roughly 70% of the 102,012 rmb.bin entries are authored pre-placed in
world space: the ``blob_centre`` field at offset 0x7c is their world
position and the vertex positions are already world-space too. The
``bbox_min/bbox_max`` at 0x8c/0x9c are in LOCAL space (symmetric around
the origin), so "centroid lies outside the bbox" is a reliable
discriminator between the two kinds of blob.

Local-authored blobs (centroid ~ origin, vertices centred) are placed
separately by CollObjs.xml / GameObjs.xml / v42k7 and skipped here to
avoid double-placement.

**Named-section labelling.** Many wrappers (``Object010_LOD00`` etc.)
carry sections whose ``RmbSection.name`` is a foreign asset tag like
``MOUN_DAM_BLDG_MainDam_003`` rather than a generic ``Material__NN``.
The wrapper's vbuf is already in world coordinates and the section's
triangle list indexes into that vbuf — i.e. the section IS the dam
piece. When a wrapper has at least one foreign-named section we emit
**one .npz per section** with ``tag = section.name`` (re-mapped to a
minimal vbuf containing only the vertices the section actually uses),
so the dam, REDROCKS, etc. show up under their authored asset names in
the Blender outliner. Wrappers with only ``Material__NN`` sections
keep the existing single-merged-blob output keyed on the wrapper tag.

Output layout (under ``out/rmb_world/``):

    index.json          {blobs: [{handle, section_idx, npz, tag,
                                  section_name, lod, vcount, tri_count,
                                  centroid, bbox_min, bbox_max}]}
    blobs/HHHHHH[_NN].npz
                        positions:(N,3) f32 WORLD-space game coords
                        faces:(M,3) u32

The Blender importer places each blob at origin (verts already carry
the world transform). Y-up → Z-up is the only basis change applied.

TERR_* blobs are emitted by the terrain_hi pipeline and dropped here.
A ``freeroam`` tag-policy drops race-event / festival dressing while
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


# Race-only / event dressing — dropped under the 'freeroam' policy.
# The festival hub itself (workshops, marquees, gantries, autoshow,
# Festival_Area5/6) is permanent in free-roam and stays in. Only the
# numbered race-segment ribbons (Festival_Area01_..04_) and the
# explicit race props (countdown, ambulance, course barriers, PROC
# traffic) are stripped. TERR_ is owned by terrain_hi and always
# dropped regardless of policy.
_DROP_TAG_PREFIXES_FREEROAM: tuple[str, ...] = (
    "Barrier_",             # race-course barriers
    "Festival_Area01_", "Festival_Area02_",
    "Festival_Area03_", "Festival_Area04_",
    "Festival_Area1_", "Festival_Area2_",
    "Festival_Area3_", "Festival_Area4_",
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

# High-detail "authoring source" rmbs whose centroid happens to fall
# 20-30 m from origin (just past the world-placed threshold). Their
# vertex buffer is in asset-local space, so they emit as a pile near
# origin if not filtered. The shipping mesh for each lives under an
# anonymous wrapper rmb (Object010_LOD00 etc.) that the named-section
# labelling pass already renames to the same authored asset name.
# Drop the detail rmbs so the labelled wrappers are the only emission.
# Tag matched on _strip_lod_suffix(tag) so all LODs are caught.
_DETAIL_AUTHORING_SOURCES: frozenset[str] = frozenset({
    "Mesh008",
    "MAINTOWN_Warehouse1",
    "AuctionShowTent",
    "MiningTower01", "MiningTower02",
    "MOUN_GM_HillSideMine",
    "REDROCKS_amphitheatre",
    "Gondola",
    "MOUN_DAM_BLDG_MainDam",
    "MOUN_DAM_BLDG_Lower", "MOUN_DAM_BLDG_LowerB",
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
    "maintownBridge", "northmaintownBridge",
    "FOOTHILLS_BLDG_SteelBridge",
    "BLDG_MainTown_Warehouse04_grunge",
    "BLDG_Maintown_Warehouse03_Grundge",
    "MAINTOWN_BLDG_WarehouseOffices",
    "REDROCKS_BLDG_Observatory",
    "REDROCKS_BLDG_Restroom",
    "REDROCKS_GotG_VisitorCentre",
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
    """True if horizontal centroid magnitude exceeds the 20m threshold."""
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


def _policy_drop(
    tag: str, policy: str,
    centroid: tuple[float, float, float] | None = None,
) -> bool:
    """Drop decision for an rmb pool entry.

    ``_DETAIL_AUTHORING_SOURCES`` was originally a tag-only filter to
    suppress near-origin "authoring source" rmbs that pass the 20m
    world-placed threshold but whose vbuf is in asset-local coords —
    they would render as a pile near origin. Tag-only matching also
    incorrectly dropped *world-positioned* variants of the same tags
    (validated against user-provided truth coords:
    ``MOUN_DAM_BLDG_InletTower`` has 11 world-positioned entries near
    the dam, ``MOUN_DAM_BLDG_ViewPlatform`` 6, ``MOUN_DAM_RoadWall1``
    2 — all real placements that the tag filter was suppressing).
    Apply the filter only when the centroid is also near origin.
    """
    if any(tag.startswith(p) for p in _ALWAYS_DROP_PREFIXES):
        return True
    if _strip_lod_suffix(tag) in _DETAIL_AUTHORING_SOURCES:
        if centroid is None:
            return True
        cx, _cy, cz = centroid
        if (cx * cx + cz * cz) < (100.0 * 100.0):
            return True
    if policy == "freeroam":
        return any(tag.startswith(p) for p in _DROP_TAG_PREFIXES_FREEROAM)
    return False


_PHANTOM_WRAPPER_RE = __import__("re").compile(r"^Box\d+_LOD\d+", __import__("re").IGNORECASE)


def _is_phantom_wrapper(tag: str) -> bool:
    """True if the wrapper tag is a streaming/visibility-buffer wrapper
    that bakes foreign asset geometry but is NOT a real placement.

    Verified across smelter, dam, bunker, amphitheatre, redrocks: the
    ``Box NNN_LOD\\d+`` wrappers consistently emit at phantom positions
    (8000m+ from the user-truth landmark coords). Their sections are
    duplicates of real placements that live in named-asset rmbs
    elsewhere in the pool.

    ``Object NNN_LOD\\d+`` wrappers are NOT phantoms — Object001_LOD00
    sits at the real dam zone (within 200m of user-truth) and carries
    the only emission of ``MOUN_DAM_BLDG_Lower_*`` (no world-positioned
    named-asset variant for that part). The cross-pool dedup against
    ``world_placed_assets`` handles those case-by-case.
    """
    return bool(_PHANTOM_WRAPPER_RE.match(tag or ""))


def _is_named_asset_tag(tag: str) -> bool:
    """True if tag looks like a real-asset name (not a generic wrapper).

    Generic wrappers: ``Object NNN``, ``Box NNN``, ``BUILDINGS_NNN``,
    ``GLOB_*``, ``Hub NNN``, ``Mesh NNN``, ``Set NNN``. Everything else
    counts as a named asset (has prefix like ``MOUN_DAM_``, ``PLA_``,
    ``MAINTOWN_``, ``MT_``, ``OBJ_``, ``BLDG_``, ``REDROCKS_``, etc.).
    """
    if not tag:
        return False
    GENERIC = (
        "Object", "Box", "BUILDINGS_", "GLOB_", "Hub", "Mesh", "Set",
        "Subset", "Group", "Cube", "Plane", "Cyl",
    )
    return not any(tag.startswith(p) for p in GENERIC)


def _is_foreign_asset_name(name: str) -> bool:
    """True if a section name looks like a foreign asset tag.

    Asset tags use SCREAMING_CASE / CamelCase with underscores, e.g.
    ``MOUN_DAM_BLDG_MainDam_003`` or ``Object010_LOD00``. Material /
    blend-mode names are lowercase (``grass_to_mud``, ``sediment_rock``)
    and are filtered out by requiring at least one uppercase character.
    Empty names and explicit ``Material_*`` / ``material_*`` prefixes
    are also rejected.
    """
    if not name:
        return False
    if name.startswith(("Material_", "material_")):
        return False
    if "_" not in name:
        return False
    return any("A" <= c <= "Z" for c in name)


# Chunk-AABB ownership index. The world's prop chunks (v42k7 variant)
# are scene-authored irregular AABBs, ~14k of them, ~300m on a side,
# overlapping such that each map point sits inside ~6 chunk AABBs on
# average. The "owner" of an asset is the chunk whose AABB tightly
# contains the asset's centroid — i.e., the smallest containing AABB.
# Other chunks containing the same point are visitors (they pre-load
# the asset for streaming/visibility prefetch, and their copy of the
# asset in the rmb pool is a duplicate of the owner's copy).
#
# Implemented as a uniform 256m grid bucket (cheaper than R-tree, no
# extra dependency). Each chunk inserts itself into every grid cell
# its AABB overlaps; lookup queries the centroid's cell and walks the
# chunks there to find the smallest containing AABB.
_CHUNK_GRID_CELL_SIZE = 256.0


def _build_chunk_aabb_index(
    out_dir: Path,
) -> tuple[list[tuple[float, float, float, float, float, float]],
           dict[tuple[int, int], list[int]]]:
    """Load chunk AABBs from the world pipeline output and return
    (chunk_list, grid_index).

    chunk_list[i] = (xmin, zmin, xmax, zmax, cx, cz) where cx/cz are
    the precomputed center for nearest-chunk fallback.

    grid_index[(gx, gz)] = list of chunk indices whose AABB overlaps
    that 256m cell. Returns (empty_list, empty_dict) if world output
    is missing — caller falls back to position-rounding dedup.

    NOTE: chunk AABBs cover only drivable / playable zones. Off-track
    landmarks (smelter, observatory, etc.) sit OUTSIDE all AABBs and
    are owned by the nearest-on-the-road chunk via its PVS list. The
    user-supplied truth coords confirm: smelter (-1958, -2616) has no
    containing AABB; the nearest is 89m away (a v44k5 chunk). PVSZ
    membership would be the correct long-term ownership signal — see
    `__R00Z*.pvsz` format in docs/format.md.

    Includes ALL world variants except terrain (whose ±100000m Y
    bbox makes XZ overlap meaningless) and crowd (NPC walk volumes,
    not asset placements).
    """
    world_path = out_dir.parent / "world" / "ribbon_00.json"
    if not world_path.exists():
        return [], {}
    try:
        d = json.loads(world_path.read_text())
    except Exception:
        return [], {}
    OWNER_VARIANTS = ("v42k7", "v44k5", "vegetation", "landmark_anim")
    chunks: list[tuple[float, float, float, float, float, float]] = []
    for c in d.get("chunks", []):
        if c.get("variant") not in OWNER_VARIANTS:
            continue
        bmin, bmax = c.get("bbox_min"), c.get("bbox_max")
        if not bmin or not bmax:
            continue
        xmin, zmin = float(bmin[0]), float(bmin[2])
        xmax, zmax = float(bmax[0]), float(bmax[2])
        cx = (xmin + xmax) * 0.5
        cz = (zmin + zmax) * 0.5
        chunks.append((xmin, zmin, xmax, zmax, cx, cz))
    grid: dict[tuple[int, int], list[int]] = {}
    cs = _CHUNK_GRID_CELL_SIZE
    for i, (xmin, zmin, xmax, zmax, _cx, _cz) in enumerate(chunks):
        # Insert into the AABB cells (for containment query)
        gxmin = int(xmin // cs)
        gzmin = int(zmin // cs)
        gxmax = int(xmax // cs)
        gzmax = int(zmax // cs)
        for gx in range(gxmin, gxmax + 1):
            for gz in range(gzmin, gzmax + 1):
                grid.setdefault((gx, gz), []).append(i)
    return chunks, grid


def _find_owner_chunk(
    cx: float, cz: float,
    chunks: list[tuple[float, float, float, float, float, float]],
    grid: dict[tuple[int, int], list[int]],
) -> int:
    """Find the primary owner chunk for an asset at (cx, cz).

    1. If any chunk's AABB contains the point → smallest containing
       AABB wins (geometric containment = primary).
    2. Else (point is off-road / outside all AABBs) → nearest chunk
       by center distance, searched within a few grid cells.

    Returns -1 only if `chunks` is empty (no AABB index loaded).
    """
    if not chunks:
        return -1

    cs = _CHUNK_GRID_CELL_SIZE
    cell = (int(cx // cs), int(cz // cs))

    # First: try strict containment. Smallest containing AABB wins.
    best_contain = -1
    best_area = float("inf")
    for i in grid.get(cell, ()):
        xmin, zmin, xmax, zmax, _, _ = chunks[i]
        if xmin <= cx <= xmax and zmin <= cz <= zmax:
            area = (xmax - xmin) * (zmax - zmin)
            if area < best_area:
                best_area = area
                best_contain = i
    if best_contain >= 0:
        return best_contain

    # Fallback: nearest center within a search radius. Probe expanding
    # cell rings around (cx, cz) to limit scan; each ring is +1 cell
    # outward. Stop when we find any candidate, then return the closest.
    best_near = -1
    best_dist = float("inf")
    for ring in range(1, 5):  # up to 4 cells = 1024m radius
        candidates: list[int] = []
        for dgx in range(-ring, ring + 1):
            for dgz in range(-ring, ring + 1):
                if abs(dgx) != ring and abs(dgz) != ring:
                    continue  # only the boundary of this ring
                key = (cell[0] + dgx, cell[1] + dgz)
                candidates.extend(grid.get(key, ()))
        for i in candidates:
            _xmin, _zmin, _xmax, _zmax, ccx, ccz = chunks[i]
            d = (cx - ccx) ** 2 + (cz - ccz) ** 2
            if d < best_dist:
                best_dist = d
                best_near = i
        if best_near >= 0:
            return best_near
    return best_near


def _remap_to_used_verts(
    positions: np.ndarray, triangles: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Trim ``positions`` to the verts referenced by ``triangles`` and
    rewrite the triangle indices to match. Avoids emitting orphan verts
    when splitting a wrapper into per-section .npz files.
    """
    if triangles.size == 0:
        return np.zeros((0, 3), dtype=np.float32), np.zeros((0, 3), dtype=np.uint32)
    flat = triangles.reshape(-1).astype(np.int64, copy=False)
    used, inverse = np.unique(flat, return_inverse=True)
    sub_pos = positions[used]
    sub_tris = inverse.reshape(triangles.shape).astype(np.uint32, copy=False)
    return sub_pos.astype(np.float32, copy=False), sub_tris


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
    skipped_phantom_wrapper = 0
    skipped_phantom_section = 0
    wrappers_split = 0
    sections_labelled = 0

    # Pass 0: per-family LOD-floor scan. The previous LOD filter dropped
    # everything except literal "" (NOLOD) and "00" — but some asset
    # families have NO LOD00 variant in the pool (e.g.
    # ``REDROCKS_BLDG_Observatory_LOD01`` is the only Observatory variant
    # outside NOLOD). Globally requiring LOD00 silently drops those whole
    # families. Smarter rule: for each stripped-tag family, find the
    # LOWEST LOD number present (highest detail), and keep ONLY that
    # LOD level. NOLOD ("" or "NoLOD") is treated as "no LOD" — kept
    # if no numeric-LOD variant exists for the family.
    family_min_lod: dict[str, int] = {}
    family_has_nolod: set[str] = set()
    for handle, e in enumerate(rmb_entries):
        try:
            buf = read_entry(zip_path, e)
            blob = parse_blob(buf)
            if blob is None:
                continue
        except Exception:
            continue
        if blob.tag.startswith("TERR_"):
            continue
        stripped = _strip_lod_suffix(blob.tag)
        if not blob.lod:
            family_has_nolod.add(stripped)
            continue
        try:
            lod_num = int(blob.lod)
        except ValueError:
            continue
        if stripped not in family_min_lod or lod_num < family_min_lod[stripped]:
            family_min_lod[stripped] = lod_num

    # Pass 1: scan the rmb pool to build two dicts for cross-pool dedup
    # in pass 2:
    #
    # - ``world_placed_assets``: stripped_tag → (X, Z) of one
    #   world-positioned named-asset rmb (centroid > 100m from origin).
    #   Used to determine if an asset has a "real placement" source
    #   elsewhere in the pool.
    # - ``library_only_assets``: stripped_tags whose every rmb sits at
    #   origin (centroid < 100m). These are "library only" — the engine
    #   uses them as occluder/streaming references baked into wrappers,
    #   but they never directly render. Verified examples (Colorado):
    #   PLA_SMELTER_BLDG_GasScrubber, PLA_SMELTER_BLDG_CokeOvens,
    #   PLA_SMELTER_BLDG_QuenchTower, PLA_SMELTER_BLDG_TallBuilding,
    #   PLA_SMELTER_BLDG_WaterTower, PLA_SMELTER_BLDG_WideShed,
    #   PLA_SMELTER_BLDG_CircularBuilding, PLA_SMELTER_BLDG_CokeBuilding,
    #   PLA_SMELTER_STFN_Gasometer (all 0/N origin in the audit).
    #   The user's ground-truth report ("only the BlastFurnace smelter
    #   renders, the GasScrubbers don't") confirms these are not real
    #   placements. Drop their references from wrapper splits in pass 2.
    world_placed_assets: dict[str, tuple[float, float]] = {}
    origin_only_assets: dict[str, int] = {}  # stripped_tag → count of origin entries seen (only meaningful if no world entry)
    for handle, e in enumerate(rmb_entries):
        if progress and handle % 5000 == 0:
            progress(handle, len(rmb_entries) * 2)  # x2 = pass 1 + pass 2
        try:
            buf = read_entry(zip_path, e)
            blob = parse_blob(buf)
            if blob is None:
                continue
        except Exception:
            continue
        if blob.tag.startswith("TERR_"):
            continue
        if not _is_named_asset_tag(blob.tag):
            continue
        cx, _cy, cz = blob.centroid
        stripped = _strip_lod_suffix(blob.tag)
        if (cx * cx + cz * cz) < (100.0 * 100.0):
            origin_only_assets[stripped] = origin_only_assets.get(stripped, 0) + 1
        else:
            if stripped not in world_placed_assets:
                world_placed_assets[stripped] = (float(cx), float(cz))
    # library_only = stripped tags that ONLY appear at origin (no world entry).
    library_only_assets: set[str] = {
        s for s in origin_only_assets if s not in world_placed_assets
    }
    pool_offset = len(rmb_entries)  # progress offset for pass 2

    # Chunk-AABB ownership index. The rmb pool is the flat union of every
    # v42k7 chunk's local rmb table (see docs/world-architecture.md §2);
    # the same asset baked into N chunks shows up as N pool entries at
    # the same world centroid. To recover the originally-global scene
    # we keep one entry per (asset, primary_owner_chunk), where the
    # primary owner is the smallest v42k7 chunk AABB that contains the
    # asset's centroid. If the world-pipeline output isn't available
    # (the v42k7 chunk AABBs are needed) the ownership pass becomes a
    # no-op and we fall back to (stripped_tag, rounded_centroid)
    # dedup as a degraded heuristic.
    chunk_aabbs, chunk_grid = _build_chunk_aabb_index(out_dir)
    use_owner_dedup = bool(chunk_aabbs)

    # Per-(asset, owner_chunk) dedup keys. For owner-dedup mode the key
    # is (stripped_tag, owner_chunk_idx); for fallback mode it's
    # (stripped_tag, rounded_centroid_cell). Either way: first seen
    # wins, subsequent are dropped as visitor copies.
    owner_seen: set[tuple] = set()
    skipped_stacked = 0

    for handle, e in enumerate(rmb_entries):
        if progress and handle % 2000 == 0:
            progress(pool_offset + handle, len(rmb_entries) * 2)
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

        # Phantom Box NNN_LOD\d+ wrappers — drop entirely. Validated
        # across smelter/dam/bunker/amphitheatre: their split sections
        # consistently land at world positions thousands of meters from
        # the user-truth landmark; the engine treats these as visibility
        # buffers, not render targets.
        if _is_phantom_wrapper(blob.tag):
            skipped_phantom_wrapper += 1
            continue

        if _policy_drop(blob.tag, policy, blob.centroid):
            skipped_policy_count += 1
            if len(skipped_policy) < 20:
                skipped_policy.append(blob.tag)
            continue

        if not include_all_lods:
            # Per-family-min-LOD rule. Within each stripped-tag family,
            # the highest-detail variant present is the LOD with the
            # smallest number. Some families have only LOD01 (e.g. the
            # 6 Observatory entries are all LOD01); the global "keep
            # LOD00 only" filter dropped those entirely.
            stripped_lod = _strip_lod_suffix(blob.tag)
            target_lod = family_min_lod.get(stripped_lod)
            if blob.lod:
                # Numeric LOD variant — keep iff it matches the family's
                # min (the highest-detail variant present in the pool).
                try:
                    lod_num = int(blob.lod)
                except ValueError:
                    lod_num = None
                if target_lod is not None and lod_num != target_lod:
                    skipped_lod += 1
                    continue
            else:
                # LOD-less (NOLOD / unspecified) — keep iff the family
                # has no numeric-LOD variant, or this tag is the canonical
                # un-LOD'd asset (e.g. ``cables_570``).
                if target_lod is not None:
                    # Family has numeric LODs; the LOD-less version is a
                    # separate variant (often a NOLOD wall piece etc.).
                    # Keep — these don't usually conflict with numeric LODs.
                    pass
            if "MIDDIST" in blob.tag:
                skipped_lod += 1
                continue

        # Chunk-AABB ownership dedup (named-asset wrappers only — keeps
        # the split-mode emission working for Object NNN that wrap
        # foreign assets). Ordered AFTER the LOD filter so LOD01/02
        # don't claim the dedup key and starve out the LOD00 that
        # arrives later in pool order. Whatever survives to here is
        # LOD00 or LOD-less NOLOD — the highest available detail.
        if _is_named_asset_tag(blob.tag):
            cx, _cy, cz = blob.centroid
            stripped = _strip_lod_suffix(blob.tag)
            if use_owner_dedup:
                owner = _find_owner_chunk(cx, cz, chunk_aabbs, chunk_grid)
                # owner == -1 means no v42k7 chunk contains this asset.
                # Could be a truly orphan rmb, or the chunk grid doesn't
                # cover this point (e.g. very-edge map data). Use a
                # synthetic per-position bucket key so orphans still get
                # stacked-deduped instead of blasting all out.
                if owner < 0:
                    owner_key = ("orphan", int(cx / 5.0), int(cz / 5.0))
                else:
                    owner_key = ("chunk", owner)
                dedup_key = (stripped, owner_key)
            else:
                # Fallback when world pipeline output is missing: use
                # rounded position as the dedup key (degraded but works).
                dedup_key = (stripped, int(cx / 5.0), int(cz / 5.0))
            if dedup_key in owner_seen:
                skipped_stacked += 1
                continue
            owner_seen.add(dedup_key)

        # Gather (positions_array, triangles, section_name) tuples for
        # every section carrying triangles, drawing from the primary blob
        # AND every sub-blob (each sub-blob has its own vbuf).
        section_emissions: list[tuple[np.ndarray, np.ndarray, str]] = []
        for s in blob.sections:
            if s.triangles.size:
                section_emissions.append((blob.positions, s.triangles, s.name))
        for sub in blob.sub_blobs:
            for s in sub.sections:
                if s.triangles.size:
                    section_emissions.append((sub.positions, s.triangles, s.name))

        if not section_emissions:
            skipped_no_triangles += 1
            continue

        has_foreign = any(
            _is_foreign_asset_name(name) for _pos, _tri, name in section_emissions
        )

        centroid = (
            float(blob.centroid[0]),
            float(blob.centroid[1]),
            float(blob.centroid[2]),
        )
        bbox_min = (
            float(blob.bbox_min[0]),
            float(blob.bbox_min[1]),
            float(blob.bbox_min[2]),
        )
        bbox_max = (
            float(blob.bbox_max[0]),
            float(blob.bbox_max[1]),
            float(blob.bbox_max[2]),
        )

        if has_foreign:
            wrap_x, _, wrap_z = blob.centroid

            # Wrapper-level phantom check: for anonymous wrappers
            # (Object/BUILDINGS/GLOB/Hub) the wrapper is a
            # streaming/visibility buffer that may bake foreign assets
            # at a phantom centroid. Keep the wrapper only if it sits
            # within 500m of at least ONE of its foreign sections' real
            # world position (per pass-1's world_placed_assets map). If
            # none of its baked sections has a nearby real-world
            # placement, the wrapper is at a phantom zone — drop it
            # entirely. Named-asset wrappers (PLA_/MOUN_/MAINTOWN_/etc.)
            # are skipped from this check; they're real-asset rmbs whose
            # centroid IS the placement (and the stacked-dedup above
            # collapses duplicates).
            _re = __import__("re")
            if not _is_named_asset_tag(blob.tag):
                wrapper_in_zone = False
                wrapper_has_world_section = False
                for _pos, _tri, name in section_emissions:
                    if not _is_foreign_asset_name(name):
                        continue
                    sec_stripped = _re.sub(
                        r'_\d{2,4}$', '',
                        _strip_lod_suffix(name),
                    )
                    ref = world_placed_assets.get(sec_stripped)
                    if ref is None:
                        continue
                    wrapper_has_world_section = True
                    dx = wrap_x - ref[0]
                    dz = wrap_z - ref[1]
                    if (dx * dx + dz * dz) <= (500.0 * 500.0):
                        wrapper_in_zone = True
                        break
                # Drop only if at least one section has a known real
                # position (so we can judge "in zone" vs "phantom") AND
                # the wrapper isn't near any of them. Wrappers whose
                # foreign sections are *all* origin-only (no world
                # counterpart) keep emitting — there's no other source
                # for those assets.
                if wrapper_has_world_section and not wrapper_in_zone:
                    skipped_phantom_wrapper += 1
                    continue

            # Split mode: emit one .npz per section. Foreign-named
            # sections take their own asset name as tag; bare-Material
            # leftovers in the same wrapper keep the wrapper tag. The
            # per-section dedup below is a finer-grained filter for
            # named-asset wrappers (which we always keep at the wrapper
            # level) — drops individual sections that reference a far
            # named asset.
            wrappers_split += 1
            for idx, (pos, tris, name) in enumerate(section_emissions):
                sub_pos, sub_tris = _remap_to_used_verts(pos, tris)
                if sub_tris.shape[0] == 0:
                    continue
                out_tag = name if _is_foreign_asset_name(name) else blob.tag
                if _is_foreign_asset_name(name):
                    sec_stripped = _strip_lod_suffix(name)
                    # Strip trailing _\d+ suffix too (sections like
                    # PLA_SMELTER_BLDG_BlastFurnaceLow_001 have the
                    # numbered variant; their authoritative rmb is
                    # PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00 stripped
                    # to PLA_SMELTER_BLDG_BlastFurnaceLow).
                    sec_stripped = __import__("re").sub(
                        r'_\d{2,4}$', '', sec_stripped,
                    )
                    # Library-only assets (every rmb of this stripped tag
                    # sits at origin; no world-positioned named-asset rmb
                    # exists). Engine uses these as occluder/streaming
                    # references baked into wrappers, never directly
                    # rendered. User confirms via ground-truth report:
                    # "only the BlastFurnace smelter renders, gas
                    # scrubbers / coke ovens etc. don't appear in-game".
                    if sec_stripped in library_only_assets:
                        skipped_phantom_section += 1
                        continue
                    ref = world_placed_assets.get(sec_stripped)
                    if ref is not None:
                        dx = wrap_x - ref[0]
                        dz = wrap_z - ref[1]
                        if (dx * dx + dz * dz) > (500.0 * 500.0):
                            skipped_phantom_section += 1
                            continue
                    sections_labelled += 1
                npz_name = f"{handle:06d}_{idx:02d}.npz"
                np.savez(
                    blob_dir / npz_name,
                    positions=sub_pos,
                    faces=sub_tris,
                )
                blobs_meta.append({
                    "handle": handle,
                    "section_idx": idx,
                    "npz": f"blobs/{npz_name}",
                    "tag": out_tag,
                    "wrapper_tag": blob.tag,
                    "section_name": name,
                    "lod": blob.lod,
                    "vcount": int(sub_pos.shape[0]),
                    "tri_count": int(sub_tris.shape[0]),
                    "centroid": list(centroid),
                    "bbox_min": list(bbox_min),
                    "bbox_max": list(bbox_max),
                })
            continue

        # Merge mode (existing behaviour): combine primary + all
        # sub-blob geometry into a single .npz keyed on the wrapper tag.
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

        npz_name = f"{handle:06d}.npz"
        np.savez(
            blob_dir / npz_name,
            positions=positions.astype(np.float32, copy=False),
            faces=tris,
        )
        blobs_meta.append({
            "handle": handle,
            "section_idx": 0,
            "npz": f"blobs/{npz_name}",
            "tag": blob.tag,
            "wrapper_tag": blob.tag,
            "section_name": "",
            "lod": blob.lod,
            "vcount": int(positions.shape[0]),
            "tri_count": int(tris.shape[0]),
            "sub_blobs": len(blob.sub_blobs),
            "centroid": list(centroid),
            "bbox_min": list(bbox_min),
            "bbox_max": list(bbox_max),
        })

    if progress:
        progress(len(rmb_entries) * 2, len(rmb_entries) * 2)

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
        "skipped_phantom_wrapper": skipped_phantom_wrapper,
        "skipped_phantom_section": skipped_phantom_section,
        "skipped_stacked": skipped_stacked,
        "owner_dedup": "chunk_aabb" if use_owner_dedup else "rounded_position_fallback",
        "v42k7_chunk_aabbs_loaded": len(chunk_aabbs),
        "world_placed_assets_count": len(world_placed_assets),
        "library_only_assets_count": len(library_only_assets),
        # Per-family LOD floor (highest detail = smallest LOD number per
        # stripped-tag family). Exposed so v42k7_inst can apply the same
        # rule consistently across pipelines. Stored as
        # {stripped_tag: lod_num} where lod_num is the smallest LOD digit
        # present in the pool for that family.
        "family_min_lod": {k: v for k, v in family_min_lod.items()},
        # LOD distribution of emitted blobs — sanity check. After LOD filter
        # we only keep "" (NOLOD) and "00". If "01"/"02" appear, something's
        # wrong with the filter or the LOD parser.
        "lod_distribution": {
            lod: sum(1 for b in blobs_meta if b.get("lod", "") == lod)
            for lod in sorted({b.get("lod", "") for b in blobs_meta})
        },
        "wrappers_split": wrappers_split,
        "sections_labelled": sections_labelled,
        "world_placed_assets": len(world_placed_assets),
        "failed": failed,
        "blobs": blobs_meta,
    }
    (out_dir / "index.json").write_text(json.dumps(doc))
    return doc


def summarise(doc: dict, stream=sys.stderr) -> None:
    n = doc["blob_count"]
    pool = doc["rmb_pool_size"]
    print(
        f"rmb_world[{doc['policy']}]: {n} world-placed emissions out of "
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
    wrappers_split = doc.get("wrappers_split", 0)
    sections_labelled = doc.get("sections_labelled", 0)
    if wrappers_split:
        print(
            f"  labelling: {wrappers_split} wrappers split, "
            f"{sections_labelled} sections renamed to authored asset tags",
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
