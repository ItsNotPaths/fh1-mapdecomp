"""Extract PVS-based world placements + referenced rmb meshes.

The master ``Ribbon_NN/<Track>_NN.pvs`` is the authored placement
description for an entire ribbon. After ``pvs.parse_pvs`` +
``pvs.hydrate_transforms`` we have, for each placement slot:

    PVSModelInstance(model_index, transform, …)

Every ``model_index`` resolves to ``{prefix}.{NNNNN}.rmb.bin`` in
bin.zip; that rmb's geometry (primary + sub-blobs) is the asset.

Output layout (under ``out/pvs_inst/``):

    index.json          {blobs: [{handle, npz, tag, lod, vcount,
                                  tri_count, sub_blobs, model_index}],
                         chunks: [{filename, name, origin,
                                  sections: [{handles, instances: [
                                    {pos: [x,y,z], rot: [[..],[..],[..]]}
                                  ]}]}]}
    blobs/m{NNNNN}.npz  positions:(N,3) f32 game-space (Y-up), local
                        faces:    (M,3) u32

The schema is intentionally identical to ``v42k7_inst`` so the existing
``import_world.py`` ``_import_inst_dir`` GN-instance loader handles it
unchanged. ``handle`` is the PVS ``model_index`` (kept disjoint from
v42k7 rmb-pool handles by living in its own output directory).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import re

from fh1_mapdecomp.binzip import Entry, list_entries, read_entry
from fh1_mapdecomp.pvs import (
    Pvs, parse_pvs, hydrate_transforms,
    find_pvs, make_binzip_pvsz_reader,
)
from fh1_mapdecomp.rmb import parse_blob


_LOD_NOLOD_RE = re.compile(r"(_LOD\d+_*|_NOLOD)$")


def _strip_lod_suffix(tag: str) -> str:
    """Drop a trailing ``_LOD\\d+`` (with optional trailing underscores)
    or ``_NOLOD`` from a tag, returning the family-key form. Used to
    group LOD siblings of the same authored asset.
    """
    return _LOD_NOLOD_RE.sub("", tag)


ProgressFn = Optional[Callable[[int, int], None]]


# Race / event dressing dropped by the freeroam policy. Keep the
# prefix list narrow — PVS is the engine's authoritative placement
# table; anything the engine spawns in free-roam should pass through.
_DROP_TAG_PREFIXES_FREEROAM: tuple[str, ...] = (
    "Festival_Area01_", "Festival_Area02_",
    "Festival_Area03_", "Festival_Area04_",
    "Festival_Area1_", "Festival_Area2_",
    "Festival_Area3_", "Festival_Area4_",
    "Ambulance",
    "Countdown",
    "Shadow_Caster",
    "PROC_cars",
)
# Substring match (anywhere in the tag). User-confirmed: no freeroam
# placement asset has 'RACE' in the tag, so any RACE-bearing tag
# (`FEST_RACE_*`, `Race_*`, `_RACE_`, `RACECountdown`, etc.) is race-
# event dressing safe to drop.
#
# `_Grp_` was tested as an additional substring (assumed batched-
# impostor LOD swap for individually-placed siblings) but reverted —
# the group composites turned out to be the *only* placements at
# their positions, not duplicates of nearby individuals.
_DROP_TAG_SUBSTRINGS_FREEROAM: tuple[str, ...] = (
    "RACE",
)
# TERR_ is owned by the terrain_hi pipeline regardless of policy.
_ALWAYS_DROP_PREFIXES: tuple[str, ...] = ("TERR_",)


def _list_rmb_files_by_index(zip_path: Path, prefix: str) -> dict[int, Entry]:
    """Map ``model_index`` → bin.zip entry by filename pattern."""
    out: dict[int, Entry] = {}
    needle = prefix + "."
    for e in list_entries(zip_path):
        fn = e.filename
        if not fn.endswith(".rmb.bin") or not fn.startswith(needle):
            continue
        # Some entries have a __dup-N suffix in repacked archives; the model
        # index is always the second dot-separated token.
        try:
            mi = int(fn[len(needle):].split(".", 1)[0])
        except ValueError:
            continue
        # First-write-wins: bin.zip can contain duplicate entries for the
        # same model index (cross-ribbon copies). The pool that ``parse_blob``
        # would parse is identical between copies for our purposes.
        out.setdefault(mi, e)
    return out


def _merge_blob_geometry(blob) -> tuple[np.ndarray, np.ndarray]:
    """Merge primary + sub-blob vertices/triangles into one mesh.

    Sub-blobs share the primary's coordinate frame (same logic as
    ``v42k7_inst``).

    No centroid subtraction. We tried it (re-centring every mesh on
    its authored pivot) and it broke world-authored landmarks: their
    vertices live in world coords with PVS pos (0,0,0), and after
    subtraction every world-authored asset landed at the world origin.
    The world-authored discriminator below handles the duplicate-
    landmark case explicitly instead.
    """
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
    return positions.astype(np.float32, copy=False), tris


_WORLD_AUTHORED_THRESHOLD_M = 100.0


def _is_world_authored(blob) -> bool:
    """True when the blob's vertices are baked at world coordinates.

    For these rmbs (smelter blast furnace, dam, REDROCKS amphitheatre,
    etc.) PVS lists multiple slots — typically one with translation
    (0,0,0) and several "visibility hint" slots at far-off coords. The
    engine renders the asset once at the world location its vertices
    already encode; the offset slots are streaming hints, not real
    placements. Without this discriminator we double-place: every PVS
    translation gets added to already-world vertices, producing offset
    duplicate shells across the map.
    """
    cx, cy, cz = blob.centroid
    return (cx * cx + cy * cy + cz * cz) > _WORLD_AUTHORED_THRESHOLD_M ** 2


def _classify_tag(tag: str, policy: str) -> str:
    """Return ``'drop'``, ``'terrain'``, or ``'keep'``."""
    if any(tag.startswith(p) for p in _ALWAYS_DROP_PREFIXES):
        return "terrain"
    if policy == "freeroam":
        if any(tag.startswith(p) for p in _DROP_TAG_PREFIXES_FREEROAM):
            return "drop"
        if any(s in tag for s in _DROP_TAG_SUBSTRINGS_FREEROAM):
            return "drop"
    return "keep"


def extract_pvs_instances(
    zip_path: Path,
    ribbon_dir: Path,
    out_dir: Path,
    *,
    policy: str = "freeroam",
    progress: ProgressFn = None,
) -> dict:
    """Build ``out_dir/{index.json, blobs/m*.npz}`` from a PVS+PVSZ ribbon."""
    out_dir.mkdir(parents=True, exist_ok=True)
    blob_dir = out_dir / "blobs"
    blob_dir.mkdir(exist_ok=True)

    pvs_path = find_pvs(ribbon_dir)
    if progress:
        progress(0, 4)
    pvs_doc, tbzu = parse_pvs(pvs_path.read_bytes())
    if progress:
        progress(1, 4)
    reader = make_binzip_pvsz_reader(zip_path, pvs_doc.ribbon_index)
    hydrated = hydrate_transforms(
        pvs_doc, tools_based_zone_unioning=tbzu, pvsz_reader=reader,
    )
    if progress:
        progress(2, 4)

    rmb_by_idx = _list_rmb_files_by_index(zip_path, pvs_doc.prefix)

    # Group placed instances by model_index so we emit one mesh per model.
    grouped: dict[int, list[tuple[list[float], list[list[float]]]]] = {}
    placed_total = 0
    unhydrated = 0
    for inst in pvs_doc.models_instances:
        if inst.material_data is None:
            unhydrated += 1
            continue
        t = inst.transform
        pos = [t[0][3], t[1][3], t[2][3]]
        rot = [
            [t[0][0], t[0][1], t[0][2]],
            [t[1][0], t[1][1], t[1][2]],
            [t[2][0], t[2][1], t[2][2]],
        ]
        grouped.setdefault(inst.model_index, []).append((pos, rot))
        placed_total += 1

    blobs_meta: list[dict] = []
    sections: list[dict] = []
    blob_failed: list[tuple[int, str]] = []
    skipped_filter: list[dict] = []
    skipped_terrain: list[int] = []
    skipped_missing_rmb: list[int] = []
    world_authored_count = 0
    pvs_translations_dropped = 0

    # Race-event duplicate detection. Each race ribbon ships its own
    # copy of shared festival props (LrgTV, banners, tollbooth marquees,
    # workshop trailers, road signs) under a *different* model_index
    # but the rmb mesh is byte-identical. The lowest model_index per
    # mesh-hash is the persistent freeroam asset (e.g. LrgTV mi=1132 at
    # the festival centre); higher model_indexes are race-only spawns
    # we want to drop. Verified empirically — 41% of placed instances
    # are in dup-tag blobs.
    #
    # Hashing the merged primary+sub-blob vertex bytes (after the same
    # geometry pipeline used for emit) lets us treat truly-different
    # mesh variants (e.g. each `OBJ_CLRD_SignB_NOLOD_001` carries its
    # own decal/texture-keyed vbuf) as distinct, while collapsing
    # genuine race duplicates.
    seen_mesh_hashes: dict[int, int] = {}        # byte-hash -> first_seen_mi
    canonical_section_idx: dict[int, int] = {}   # canonical_mi -> sections[] index
    skipped_lod = 0
    merged_dup_mi_count = 0
    merged_dup_instance_count = 0

    # Phase A: parse everything we can, build per-family minimum-LOD map.
    # Catches tag-pair LOD copies the mesh-hash dedup misses (different
    # LODs are different meshes — e.g. FEST_MISC_Tollbooth_Marquee_LOD00
    # mi=1715 vs LOD01 mi=6248 for the race-only copy). For each family
    # (`_LOD\d+_*` / `_NOLOD` stripped), the lowest LOD number across all
    # placed model_indexes is the family's hero detail; lower-quality
    # LODs are dropped.
    sorted_indexes = sorted(grouped.keys())
    parsed: dict[int, tuple] = {}                # mi -> (blob, positions, tris)
    family_min_lod: dict[str, int] = {}
    for n, mi in enumerate(sorted_indexes):
        if progress and n % 500 == 0:
            progress(3 + 0.5 * n / max(1, len(sorted_indexes)), 4)
        rmb_e = rmb_by_idx.get(mi)
        if rmb_e is None:
            skipped_missing_rmb.append(mi)
            continue
        try:
            buf = read_entry(zip_path, rmb_e)
            blob = parse_blob(buf)
            if blob is None:
                blob_failed.append((mi, "parse_blob returned None"))
                continue
            positions, tris = _merge_blob_geometry(blob)
            parsed[mi] = (blob, positions, tris)
            if blob.lod:
                try:
                    lod_n = int(blob.lod)
                except ValueError:
                    lod_n = None
                if lod_n is not None:
                    family = _strip_lod_suffix(blob.tag)
                    prev = family_min_lod.get(family)
                    if prev is None or lod_n < prev:
                        family_min_lod[family] = lod_n
        except Exception as ex:
            blob_failed.append((mi, str(ex)))

    # Phase B: filter + emit using parsed cache.
    for n, mi in enumerate(sorted_indexes):
        if progress and n % 500 == 0:
            progress(3.5 + 0.5 * n / max(1, len(sorted_indexes)), 4)
        rec = parsed.get(mi)
        if rec is None:
            continue
        blob, positions, tris = rec
        try:
            klass = _classify_tag(blob.tag, policy)
            if klass == "terrain":
                skipped_terrain.append(mi)
                continue
            if klass == "drop":
                skipped_filter.append({"model_index": mi, "tag": blob.tag})
                continue
            # Per-family LOD floor: keep only the highest-quality LOD
            # for each family.
            if blob.lod:
                try:
                    lod_n = int(blob.lod)
                except ValueError:
                    lod_n = None
                if lod_n is not None:
                    family = _strip_lod_suffix(blob.tag)
                    target = family_min_lod.get(family)
                    if target is not None and lod_n != target:
                        skipped_lod += 1
                        continue
            mesh_hash = hash(positions.tobytes()) ^ hash(tris.tobytes())
            if mesh_hash in seen_mesh_hashes:
                # Byte-identical mesh already emitted under a lower mi.
                # MERGE this mi's PVS instances into the canonical
                # section instead of dropping — the duplicate model
                # entries usually represent unique placements (e.g. 17
                # OBJ_BarrierBrand_LOD00 mis = 17 individually-placed
                # barriers using one shared mesh; dropping loses 16
                # placements). For LrgTV-style cases (persistent
                # freeroam + race-event copies) this also brings race
                # placements back, which the spatial filter is meant
                # to handle separately.
                canonical = seen_mesh_hashes[mesh_hash]
                sec_idx = canonical_section_idx.get(canonical)
                if sec_idx is not None:
                    insts_to_merge = grouped[mi]
                    sections[sec_idx]["instances"].extend(
                        {"pos": p, "rot": r} for (p, r) in insts_to_merge
                    )
                    merged_dup_mi_count += 1
                    merged_dup_instance_count += len(insts_to_merge)
                continue
            seen_mesh_hashes[mesh_hash] = mi
            name = f"m{mi:05d}.npz"
            np.savez(blob_dir / name, positions=positions, faces=tris)
            blobs_meta.append({
                "handle": mi,
                "npz": f"blobs/{name}",
                "tag": blob.tag,
                "lod": blob.lod,
                "vcount": int(positions.shape[0]),
                "tri_count": int(tris.shape[0]),
                "sub_blobs": len(blob.sub_blobs),
                "model_index": mi,
            })
            if _is_world_authored(blob):
                # Vertices already at world coords. Emit one identity
                # instance at origin (PVS translation for these is 0
                # for the real placement; offset slots are streaming
                # hints the engine filters at runtime).
                world_authored_count += 1
                pvs_translations_dropped += len(grouped[mi])
                sections.append({
                    "handles": [mi],
                    "instances": [{
                        "pos": [0.0, 0.0, 0.0],
                        "rot": [[1.0, 0.0, 0.0],
                                [0.0, 1.0, 0.0],
                                [0.0, 0.0, 1.0]],
                    }],
                })
            else:
                insts = grouped[mi]
                sections.append({
                    "handles": [mi],
                    "instances": [{"pos": p, "rot": r} for (p, r) in insts],
                })
            canonical_section_idx[mi] = len(sections) - 1
        except Exception as ex:
            blob_failed.append((mi, str(ex)))

    if progress:
        progress(4, 4)

    chunks = [{
        "filename": pvs_path.name,
        "name": f"PVS::Ribbon_{pvs_doc.ribbon_index:02d}",
        "origin": [0.0, 0.0, 0.0],
        "sections": sections,
    }]

    instances_after = sum(len(s["instances"]) for s in sections)

    doc = {
        "source_pvs": str(pvs_path),
        "source_zip": str(zip_path),
        "policy": policy,
        "pvs_version": pvs_doc.header.version,
        "ribbon_index": pvs_doc.ribbon_index,
        "prefix": pvs_doc.prefix,
        "models_in_pvs": len(pvs_doc.models),
        "model_instances_in_pvs": len(pvs_doc.models_instances),
        "instances_hydrated": hydrated,
        "instances_unhydrated": unhydrated,
        "instances_before_filter": placed_total,
        "instances_after_filter": instances_after,
        "blob_count": len(blobs_meta),
        "chunk_count": 1,
        "world_authored_blobs": world_authored_count,
        "world_authored_pvs_translations_dropped": pvs_translations_dropped,
        "merged_dup_mi_count": merged_dup_mi_count,
        "merged_dup_instance_count": merged_dup_instance_count,
        "skipped_lod_count": skipped_lod,
        "family_min_lod_count": len(family_min_lod),
        "skipped_terrain_count": len(skipped_terrain),
        "skipped_missing_rmb_count": len(skipped_missing_rmb),
        "skipped_filter": sorted(skipped_filter, key=lambda d: d["tag"]),
        "blob_failed": blob_failed,
        "blobs": blobs_meta,
        "chunks": chunks,
    }
    (out_dir / "index.json").write_text(json.dumps(doc))
    return doc


def summarise(doc: dict, stream=sys.stderr) -> None:
    print(
        f"pvs_inst[{doc.get('policy', 'all')}]: "
        f"{doc['blob_count']} unique meshes, "
        f"{doc['instances_after_filter']} placed instances "
        f"(of {doc['model_instances_in_pvs']} PVS slots; "
        f"{doc['instances_hydrated']} hydrated, "
        f"{doc['instances_unhydrated']} unreferenced)",
        file=stream,
    )
    sf = doc.get("skipped_filter", [])
    if sf:
        print(f"  dropped {len(sf)} models by tag policy "
              f"(top: {', '.join(d['tag'] for d in sf[:5])})",
              file=stream)
    if doc.get("world_authored_blobs"):
        print(f"  {doc['world_authored_blobs']} world-authored rmbs "
              f"emitted once at origin "
              f"({doc['world_authored_pvs_translations_dropped']} PVS "
              f"streaming-hint translations dropped)",
              file=stream)
    if doc.get("skipped_lod_count"):
        print(f"  {doc['skipped_lod_count']} non-hero-LOD models dropped "
              f"(family-min-LOD across {doc.get('family_min_lod_count', 0)} "
              f"distinct LOD families)",
              file=stream)
    if doc.get("merged_dup_mi_count"):
        print(f"  {doc['merged_dup_mi_count']} byte-identical model "
              f"copies merged onto canonical mi "
              f"({doc['merged_dup_instance_count']} instances "
              f"redirected onto a lower-numbered handle)",
              file=stream)
    if doc.get("skipped_missing_rmb_count"):
        print(f"  {doc['skipped_missing_rmb_count']} models had no rmb.bin "
              f"in the pool (likely streamed from a sibling archive)",
              file=stream)
    if doc.get("blob_failed"):
        print(f"  {len(doc['blob_failed'])} parse failures", file=stream)
