"""Extract v42k7 instance placements + referenced rmb meshes.

Each v42k7 PGEO chunk carries a section list whose entries reference
1..3 handles into the global rmb.bin geometry pool (handle == index in
``list_rmb_entries`` order). Blob vertices are LOCAL-space — the same
handle is re-instanced across thousands of chunks at very different
world positions (12k chunks → ~300 unique blobs in Colorado), so the
per-instance world transform must come from the v42k7 chunk.

Two chunk flavours:

1. **Multi-instance**: chunk has a 96B-stride position table (decoded
   by ``parse_position_table``). Each section maps to a contiguous
   slice; each entry yields a world position and a 3x3 rotation.
2. **Single-instance / sparse**: no position table. Falls back to the
   chunk-anchor scheme (one instance per section at chunk origin +
   section local-bbox centre).

Output layout (under ``out/v42k7_inst/``):

    index.json          {blobs: [{handle, npz, tag, vcount, tri_count}],
                         chunks: [{filename, name, origin, sections: [
                                    {handles, instances: [
                                        {pos: [x,y,z], rot: [[..],[..],[..]]}
                                    ]}]}]}
    blobs/HHHHHH.npz    positions:(N,3) f32 game-space (Y-up), local
                        faces:    (M,3) u32

``pos`` values are world-space game coords (Y-up); ``rot`` is a
row-major 3x3 in the same basis. The Blender importer is responsible
for the Y-up → Z-up swap.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from fh1_mapdecomp.binzip import list_entries, read_entry
from fh1_mapdecomp.pgeo import classify, parse_header
from fh1_mapdecomp.pgeo_body.v42k7 import (
    parse_layout, parse_position_table, parse_proc_inline_positions,
    parse_table_b, _is_proc_subvariant,
)
from fh1_mapdecomp.rmb import parse_blob


_IDENTITY_ROT = (
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
)


# Free-roam tag-prefix policy. v42k7 chunks intermix freeroam content
# (buildings, mountains, debris, cables) with race-event dressing
# (barriers, grandstands, festival props). 943/1377 chunks reference
# both classes, so filtering must be per-section. Authority for CO_*/
# OBJ_CLRD_* placement lives in Ribbon_00/CollObjs.xml; placing them
# from v42k7 too duplicates the prop. Authority for Barnfind_* lives
# in Ribbon_00/GameObjs.xml; v42k7 over-emits this handle ~2,897x for
# ~30 real Barn Finds. See docs/state-of-extraction.md and
# docs/freeroam-placement.md for derivation.
_KEEP_TAG_PREFIXES: tuple[str, ...] = (
    "BLDG_",          # buildings
    "MainTown_",      # main-town props
    "Maintown_",      # main-town walls (case variant)
    "MT_Area",        # mountain area terrain/decals
    "MOUN_",          # mountain props
    "Plains_Area",    # plains terrain
    "RV_",            # area environment (RV_Dawning_*, etc.)
    "S_Debris",       # roadside debris
    "MiningTower",    # landmark
    "Pavementcap_",   # road-finish caps
    "cables_",        # utility cables
    "TWALL_",         # town walls
    "PLA_",           # interstate signs
    "TERR_",          # terrain (skipped separately by skipped_terrain
                      # but listed here for documentation)
)
_DROP_TAG_PREFIXES: tuple[str, ...] = (
    # Race / festival dressing.
    "OBJ_BarrierBrand", "OBJ_FEST", "OBJ_Fest",
    "Barrier_", "Grandstand", "Ambulance",
    "Countdown", "Shadow_Caster",
    "PROC_cars",
    # CollObjs.xml owns these (de-dupe).
    "CO_", "OBJ_CLRD_", "O_CO_",
    # GameObjs.xml owns Barn Find placements (over-emission cause).
    "Barnfind_",
)


# Proc subvariant ('models_proc_clrd_*'): per-chunk descriptor → class.
# Each chunk's inline 40B vbuf carries N world-space placements; the
# descriptor name (not handle tags — those misresolve to LOD01/02) is
# the only reliable signal. Only KEEP classes emit instances; DROP
# classes are race-event dressing already excluded by the tag policy.
_PROC_KEEP_DESCRIPTORS: tuple[str, ...] = (
    "trees_", "street_", "redrock_", "redstone_",
)
_PROC_DROP_DESCRIPTORS: tuple[str, ...] = (
    "festival", "barriers", "fest_", "multiplayer", "showcase",
    "prhub", "pr_", "foot_", "track_",
)
_PROC_NAME_PREFIXES = ("models_proc_clrd_", "models_proc_")


def _proc_descriptor_class(name: str) -> str | None:
    """Return one of {'trees', 'street', 'redrock'} or None to drop.

    The proc inline vbuf only encodes positions, not handles — so we
    pick a placeholder mesh class from the descriptor's leading word.
    """
    rest = name
    for p in _PROC_NAME_PREFIXES:
        if rest.startswith(p):
            rest = rest[len(p):]
            break
    if any(rest.startswith(d) for d in _PROC_DROP_DESCRIPTORS):
        return None
    if rest.startswith("trees_"):
        return "trees"
    if rest.startswith("street_"):
        return "street"
    if rest.startswith("redrock_") or rest.startswith("redstone_"):
        return "redrock"
    return None


# Synthetic placeholder blobs for proc-inline emissions. The real
# per-position mesh isn't decoded yet — we use class-specific upright
# markers so trees / street props / redrock dressing are visually
# distinguishable in Blender. Handles sit above the rmb pool size so
# they never collide with a real handle.
_PROC_SYNTH_HANDLE_BASE = 0x100000  # well past any real rmb handle (max ~0x40000)
_PROC_SYNTH_HANDLES: dict[str, int] = {
    "trees":   _PROC_SYNTH_HANDLE_BASE + 0,
    "street":  _PROC_SYNTH_HANDLE_BASE + 1,
    "redrock": _PROC_SYNTH_HANDLE_BASE + 2,
}
_PROC_SYNTH_TAGS: dict[str, str] = {
    "trees":   "PROC_trees",
    "street":  "PROC_street",
    "redrock": "PROC_redrock",
}


def _proc_synth_mesh(klass: str) -> tuple[np.ndarray, np.ndarray]:
    """Tiny upright placeholder mesh for one proc class. Y-up game frame.

    - trees:   ~6m tall trunk + crown triangle (forward-facing)
    - street:  low ~1m × 3m horizontal slab (barrier-like)
    - redrock: ~3m blocky boulder (small triangle pyramid)
    """
    if klass == "trees":
        positions = np.array([
            [-0.30, 0.0,  0.0], [ 0.30, 0.0, 0.0],
            [-1.20, 3.0,  0.0], [ 1.20, 3.0, 0.0],
            [ 0.0,  6.5,  0.0],
        ], dtype=np.float32)
        faces = np.array([[0, 1, 3], [0, 3, 2], [2, 3, 4]], dtype=np.uint32)
    elif klass == "street":
        positions = np.array([
            [-1.50, 0.0, -0.20], [1.50, 0.0, -0.20],
            [-1.50, 0.9, -0.20], [1.50, 0.9, -0.20],
        ], dtype=np.float32)
        faces = np.array([[0, 1, 3], [0, 3, 2]], dtype=np.uint32)
    else:  # redrock
        positions = np.array([
            [-1.0, 0.0, -1.0], [1.0, 0.0, -1.0],
            [ 1.0, 0.0,  1.0], [-1.0, 0.0, 1.0],
            [ 0.0, 2.5,  0.0],
        ], dtype=np.float32)
        faces = np.array(
            [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]], dtype=np.uint32,
        )
    return positions, faces


def _classify_tag(tag: str) -> str:
    """Return 'keep', 'drop', or 'unknown' for a freeroam policy decision.

    Order: explicit drop list wins over keep list (e.g. 'OBJ_CLRD_*'
    matches both 'OBJ_' and 'CLRD' family but the drop is intentional —
    CollObjs owns it).
    """
    if any(tag.startswith(p) for p in _DROP_TAG_PREFIXES):
        return "drop"
    if any(tag.startswith(p) for p in _KEEP_TAG_PREFIXES):
        return "keep"
    return "unknown"


def _normalise_rot(rot: tuple) -> list[list[float]]:
    """Return a 3x3 rotation matrix (row-major) as nested lists.

    The three rows from parse_position_table are the local +X, +Y, +Z
    axes of the instance expressed in world space, scaled by a per-entry
    model radius. Empirically r0×r1 = -r2, so the stored axes form a
    left-handed frame (FH1 is a D3D-era Xbox 360 title).

    To feed Blender a usable rotation matrix we:
      1. Normalise each axis row to unit length.
      2. Flip the third axis so (r0, r1, r2') form a right-handed frame.
      3. Transpose so the axes become the COLUMNS of R — i.e. a standard
         row-major matrix where ``R @ local_v = world_v``.
    """
    axes: list[tuple[float, float, float]] = []
    for i, row in enumerate(rot):
        m2 = row[0] * row[0] + row[1] * row[1] + row[2] * row[2]
        if m2 > 1e-12:
            inv = m2 ** -0.5
            axes.append((row[0] * inv, row[1] * inv, row[2] * inv))
        else:
            axes.append(_IDENTITY_ROT[i])
    # LH -> RH by negating the third axis.
    axes[2] = (-axes[2][0], -axes[2][1], -axes[2][2])
    # axes[i] is local axis i in world space → columns of R. Transpose
    # into row-major rotation matrix: out[i][j] = axes[j][i].
    return [
        [axes[0][i], axes[1][i], axes[2][i]]
        for i in range(3)
    ]


ProgressFn = Optional[Callable[[int, int], None]]


def extract_v42k7_instances(
    zip_path: Path,
    out_dir: Path,
    *,
    include_all_lods: bool = False,
    policy: str = "freeroam",
    progress: ProgressFn = None,
) -> dict:
    if policy not in ("freeroam", "all"):
        raise ValueError(f"policy must be 'freeroam' or 'all', got {policy!r}")
    blob_dir = out_dir / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)

    entries = list_entries(zip_path)
    rmb_entries = [e for e in entries if "rmb.bin" in e.filename]
    # bin.zip stores each pgeo chunk multiple times under case-variant
    # filenames (e.g. __R00G06579.pgeo plus 18 lowercase copies). The bytes
    # are identical; iterating all of them emits each placement ~5-8×,
    # which is the dominant cause of "crowds of blast furnaces" and
    # "scattered shadow casters" in the rendered scene. Keep one entry per
    # case-insensitive filename. Handle resolution still indexes the full
    # rmb_entries list — that pool's duplicates are addressed by handle,
    # not by filename, and must not be collapsed.
    pgeo_entries = []
    seen: set[str] = set()
    for e in entries:
        if not e.filename.endswith(".pgeo"):
            continue
        key = e.filename.lower()
        if key in seen:
            continue
        seen.add(key)
        pgeo_entries.append(e)

    chunks: list[dict] = []
    referenced: set[int] = set()
    failed: list[tuple[str, str]] = []
    proc_classes_used: set[str] = set()
    proc_inline_chunks = 0
    proc_inline_positions = 0
    proc_dropped_descriptors: dict[str, int] = {}

    for i, e in enumerate(pgeo_entries):
        if progress and i % 2000 == 0:
            progress(i, len(pgeo_entries))
        try:
            data = read_entry(zip_path, e)
            h = parse_header(data)
            if classify(h) != "v42k7":
                continue
            layout = parse_layout(data, h)
            if layout is None:
                continue
            cx = (h.bbox_min[0] + h.bbox_max[0]) * 0.5
            cy = (h.bbox_min[1] + h.bbox_max[1]) * 0.5
            cz = (h.bbox_min[2] + h.bbox_max[2]) * 0.5

            # Proc subvariant chunks (`models_proc_clrd_*`): the section
            # handle stream misresolves to LOD01/02 race dressing, so we
            # skip the regular handle path entirely. Two position sources
            # contribute, both emitted against a class-keyed synthetic blob:
            #   - inline 40B vbuf at file tail (mostly trees_ chunks)
            #   - 96B position table indexed by Table B (mostly street_,
            #     redrock_, no-vbuf trees_)
            # Proc chunks store cull-distance metadata in the rotation rows,
            # so the position-table walk runs with `keep_all_rotations=True`
            # and we substitute identity rotation. In-bbox guard rejects any
            # stray sentinel records that slip through the count.
            if _is_proc_subvariant(data):
                klass = _proc_descriptor_class(layout.name)
                if klass is None:
                    proc_dropped_descriptors[layout.name] = (
                        proc_dropped_descriptors.get(layout.name, 0) + 1
                    )
                    continue
                synth_handle = _PROC_SYNTH_HANDLES[klass]

                positions_xyz: list[tuple[float, float, float]] = []

                # Source 1: inline 40B vbuf (when vbuf_size > 0).
                inline_pos = parse_proc_inline_positions(data, h)
                if inline_pos is not None:
                    for p in inline_pos:
                        positions_xyz.append(
                            (float(p[0]), float(p[1]), float(p[2]))
                        )

                # Source 2: 96B position table (when Table B is present).
                records = parse_table_b(data, layout)
                if records is not None:
                    per_section = parse_position_table(
                        data, layout, records, keep_all_rotations=True,
                    )
                    if per_section is not None:
                        bxmin, bymin, bzmin = h.bbox_min
                        bxmax, bymax, bzmax = h.bbox_max
                        for sec_insts in per_section:
                            for inst in sec_insts:
                                px, py, pz = inst.position
                                if (bxmin <= px <= bxmax
                                        and bymin <= py <= bymax
                                        and bzmin <= pz <= bzmax):
                                    positions_xyz.append(
                                        (float(px), float(py), float(pz))
                                    )

                if positions_xyz:
                    instances_meta = [
                        {"pos": [p[0], p[1], p[2]],
                         "rot": [list(r) for r in _IDENTITY_ROT]}
                        for p in positions_xyz
                    ]
                    proc_inline_positions += len(positions_xyz)
                else:
                    # Last-resort fallback: anchor at chunk-bbox centre
                    # (only ~7 chunks hit this on Colorado).
                    instances_meta = [{
                        "pos": [cx, cy, cz],
                        "rot": [list(r) for r in _IDENTITY_ROT],
                    }]
                    proc_inline_positions += 1
                chunks.append({
                    "filename": e.filename,
                    "name": layout.name,
                    "origin": [cx, cy, cz],
                    "sections": [{
                        "handles": [synth_handle],
                        "instances": instances_meta,
                    }],
                })
                referenced.add(synth_handle)
                proc_classes_used.add(klass)
                proc_inline_chunks += 1
                continue

            # Try per-instance placement from the 96B position table.
            # Returns None for single-instance / sparse chunks.
            records = parse_table_b(data, layout)
            per_section_instances = None
            if records is not None:
                per_section_instances = parse_position_table(data, layout, records)

            sections_meta: list[dict] = []
            handle_acc: list[int] = []
            for sec_idx, sec in enumerate(layout.sections):
                # Slot 0 is the renderable main mesh. Slots 1/2 are
                # auxiliary (shadow caster, LOD sibling, cull proxy) —
                # placing them at every instance position produces the
                # classic "crowd of shadow casters in a ring" artefact.
                primary = sec.rmb_handles[0] if sec.rmb_handles else 0
                if primary == 0:
                    continue
                handles = [primary]

                # Drop Table B sections with u0=0. These correlate with
                # high instance counts, uniformly zero section_constant,
                # and distinctive handle patterns (e.g. BlastFurnaceLow
                # placed 136× in a 240×200m chunk — the NW "ring of
                # smelters" artefact). Real per-instance placements use
                # u0=1 with jittered section_constant floats; u0=0 sections
                # appear to be occluder/impostor/spawn metadata whose true
                # semantics is not yet decoded. Safe to skip until then.
                if records is not None and sec_idx < len(records) and records[sec_idx].u0 == 0:
                    continue

                instances_meta: list[dict] = []
                if per_section_instances is not None and sec_idx < len(per_section_instances):
                    for inst in per_section_instances[sec_idx]:
                        instances_meta.append({
                            "pos": list(inst.position),
                            "rot": _normalise_rot(inst.rotation),
                        })

                if not instances_meta:
                    # Chunk-anchor fallback: single instance at
                    # chunk origin + section local-bbox centre.
                    f = sec.floats
                    instances_meta.append({
                        "pos": [
                            cx + (f[0] + f[2]) * 0.5,
                            cy + (f[1] + f[3]) * 0.5,
                            cz + (f[4] + f[5]) * 0.5,
                        ],
                        "rot": [list(r) for r in _IDENTITY_ROT],
                    })

                sections_meta.append({
                    "handles": handles,
                    "instances": instances_meta,
                })
                handle_acc.extend(handles)
            if not sections_meta:
                continue
            referenced.update(handle_acc)
            chunks.append({
                "filename": e.filename,
                "name": layout.name,
                "origin": [cx, cy, cz],
                "sections": sections_meta,
            })
        except Exception as ex:
            failed.append((e.filename, str(ex)))

    if progress:
        progress(len(pgeo_entries), len(pgeo_entries))

    # TERR_* blobs carry world-space terrain heightfield vertices and are
    # already exported by the terrain_hi pipeline. Adding a chunk-origin
    # offset to them would push verts to absurd magnitudes — skip here.
    # Non-LOD0 (LOD01/02 + MIDDIST impostors) duplicates the hero mesh at
    # the same anchor, so skip those too unless ``include_all_lods``.
    blobs_meta: list[dict] = []
    blob_failed: list[tuple[int, str]] = []
    skipped_terrain: set[int] = set()
    skipped_lod: set[int] = set()
    skipped_filter: list[dict] = []   # populated when policy == 'freeroam'
    skipped_unknown: list[dict] = []  # tags that don't match keep or drop list
    sorted_handles = sorted(referenced)
    synth_by_handle = {v: k for k, v in _PROC_SYNTH_HANDLES.items()}
    for n, handle in enumerate(sorted_handles):
        if progress and n % 500 == 0:
            progress(n, len(sorted_handles))
        # Synthetic proc-inline placeholder: write a tiny class-keyed
        # marker mesh and skip the rmb pool lookup.
        if handle in synth_by_handle:
            klass = synth_by_handle[handle]
            sp, sf = _proc_synth_mesh(klass)
            name = f"{handle:06d}.npz"
            np.savez_compressed(blob_dir / name, positions=sp, faces=sf)
            blobs_meta.append({
                "handle": handle,
                "npz": f"blobs/{name}",
                "tag": _PROC_SYNTH_TAGS[klass],
                "lod": "00",
                "vcount": int(sp.shape[0]),
                "tri_count": int(sf.shape[0]),
                "sub_blobs": 0,
            })
            continue
        if not (0 <= handle < len(rmb_entries)):
            blob_failed.append((handle, f"out of pool range ({len(rmb_entries)})"))
            continue
        rmb_e = rmb_entries[handle]
        try:
            buf = read_entry(zip_path, rmb_e)
            blob = parse_blob(buf)
            if blob is None:
                blob_failed.append((handle, "parse_blob returned None"))
                continue
            if blob.tag.startswith("TERR_"):
                skipped_terrain.add(handle)
                continue
            if not include_all_lods:
                if blob.lod and blob.lod != "00":
                    skipped_lod.add(handle)
                    continue
                if "MIDDIST" in blob.tag:
                    skipped_lod.add(handle)
                    continue
            if policy == "freeroam":
                k = _classify_tag(blob.tag)
                if k == "drop":
                    skipped_filter.append({"handle": handle, "tag": blob.tag})
                    continue
                if k == "unknown":
                    # Default: drop unknowns since race dressing dominates,
                    # but list them so a future session can graduate the
                    # legitimate ones into _KEEP_TAG_PREFIXES.
                    skipped_unknown.append({"handle": handle, "tag": blob.tag})
                    continue
            # Merge primary + sub-blob geometry into a single mesh.
            # Sub-blobs share the primary's coordinate frame (verified on
            # TERR world-space and Grandstand local-space examples) — they
            # are additional LOD/part meshes for the same asset. Many
            # handles have tiny primary vbufs (vc=4) and the real geometry
            # lives in sub-blobs (e.g. handle 61909 Grandstand: primary
            # vc=4 vs sub-blob vc=1601). See docs/rmb-subblobs.md.
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
            })
        except Exception as ex:
            blob_failed.append((handle, str(ex)))

    # Drop skipped handles from chunks so the importer doesn't have to filter.
    filtered_handles = {d["handle"] for d in skipped_filter}
    unknown_handles = {d["handle"] for d in skipped_unknown}
    drop = skipped_terrain | skipped_lod | filtered_handles | unknown_handles
    kept_inst_before = sum(len(s["instances"])
                           for c in chunks for s in c["sections"])
    if drop:
        for c in chunks:
            new_secs = []
            for s in c["sections"]:
                kept = [h for h in s["handles"] if h not in drop]
                if kept:
                    new_secs.append({"handles": kept, "instances": s["instances"]})
            c["sections"] = new_secs
        chunks[:] = [c for c in chunks if c["sections"]]
    kept_inst_after = sum(len(s["instances"])
                          for c in chunks for s in c["sections"])

    if progress:
        progress(len(sorted_handles), len(sorted_handles))

    doc = {
        "source": str(zip_path),
        "policy": policy,
        "rmb_pool_size": len(rmb_entries),
        "chunk_count": len(chunks),
        "blob_count": len(blobs_meta),
        "instances_before_filter": kept_inst_before,
        "instances_after_filter": kept_inst_after,
        "proc_inline": {
            "chunks": proc_inline_chunks,
            "positions": proc_inline_positions,
            "classes": sorted(proc_classes_used),
            "dropped_descriptor_count": sum(proc_dropped_descriptors.values()),
        },
        "skipped_terrain": sorted(skipped_terrain),
        "skipped_lod": sorted(skipped_lod),
        "skipped_filter": sorted(skipped_filter, key=lambda d: d["tag"]),
        "skipped_unknown": sorted(skipped_unknown, key=lambda d: d["tag"]),
        "failed": failed,
        "blob_failed": blob_failed,
        "blobs": blobs_meta,
        "chunks": chunks,
    }
    (out_dir / "index.json").write_text(json.dumps(doc))
    return doc


def summarise(doc: dict, stream=sys.stderr) -> None:
    chunks = doc["chunks"]
    blobs = doc["blobs"]
    policy = doc.get("policy", "all")
    print(
        f"v42k7_inst[{policy}]: {len(chunks)} chunks, {len(blobs)} unique blobs, "
        f"{len(doc['failed'])} chunk failures, {len(doc['blob_failed'])} blob failures",
        file=stream,
    )
    before = doc.get("instances_before_filter")
    after = doc.get("instances_after_filter")
    if before is not None and after is not None and before != after:
        dropped = before - after
        print(
            f"  filter[{policy}]: kept {after} / {before} instances "
            f"(dropped {dropped}, {dropped / before * 100:.1f}%)",
            file=stream,
        )
    sf = doc.get("skipped_filter", [])
    su = doc.get("skipped_unknown", [])
    if sf:
        print(f"  dropped {len(sf)} blobs by tag policy "
              f"(top: {', '.join(d['tag'] for d in sf[:5])})", file=stream)
    if su:
        print(f"  {len(su)} unknown-tag blobs treated as drop "
              f"(review: {', '.join(d['tag'] for d in su[:5])})", file=stream)
    pi = doc.get("proc_inline")
    if pi and pi.get("positions"):
        print(
            f"  proc_inline: {pi['positions']} placements across "
            f"{pi['chunks']} chunks ({', '.join(pi['classes'])})",
            file=stream,
        )
    if not blobs:
        return
    total_v = sum(b["vcount"] for b in blobs)
    total_t = sum(b["tri_count"] for b in blobs)
    with_tris = sum(1 for b in blobs if b["tri_count"] > 0)
    print(
        f"  {total_v} vertices, {total_t} triangles "
        f"({with_tris}/{len(blobs)} blobs have triangles)",
        file=stream,
    )
