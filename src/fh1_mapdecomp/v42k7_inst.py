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


# Free-roam tag-prefix drop-list. The v42k7 sections list 1-3 rmb
# handles each (slots 0/1/2) — these are compound placements where the
# engine puts every listed handle at every instance position the
# section's Table-B record points at. Most slot pairs co-place two
# different assets (e.g. BLDG_MainTown_Corner011 + Modular_020 — a
# corner piece plus an adjacent wall section); a minority list two
# LODs of the same asset (e.g. OBJ_BarrierBrand_LOD00 + _LOD01).
#
# We emit every handle the chunk references EXCEPT race-event dressing
# and assets owned by other manifests:
#   - CollObjs.xml owns CO_*/OBJ_CLRD_*/O_CO_* (placing them from v42k7
#     duplicates the prop).
#   - GameObjs.xml owns Barnfind_* (real Barn Find positions live there;
#     v42k7 lists Barnfind_Barn__LOD00 in 500+ section slots as a
#     shared library reference, not a placement).
# All other handles pass through; trying to maintain a keep-list bottoms
# the output to ~48 unique handles, which is the slot-0 over-emission
# artifact.
_DROP_TAG_PREFIXES: tuple[str, ...] = (
    # Strict race-only drops. OBJ_BarrierBrand / OBJ_FEST_* / FEST_* /
    # GrandstandStraight ARE permanent freeroam geometry at the
    # festival hub (verified: sec[12] of __R00G07014 places 71
    # OBJ_BarrierBrand instances right at the festival entrance — those
    # are real metal barriers; sec[2] co-places OBJ_FEST_CanopyClosed
    # with its RV_Dawning_01 chassis). They're kept here.
    "Ambulance",
    "Countdown",
    "Shadow_Caster",
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


# u0=0 cull-box rendering threshold. Table B's `cull_box[0]` discriminates
# u0=0 streaming/metadata sections from real-placement u0=0 sections:
#
#   cull[0]   role
#   -------   ----
#   0         metadata-only (always inst=0)
#   60        streaming impostor — all sentinel-bbox sections live here,
#             plus a set of unique-bbox sections that pair unrelated
#             handles (Sign+Commercial, Maintown+Festival, etc.)
#   80+       real placement (mostly building modules, walls, decals,
#             cables; the per-handle LOD filter handles LOD01/MIDDIST
#             stragglers). Threshold-80 verified: 10 cull=80 sections
#             of `Maintown_WallSmall_BrickRedd_8M_NOLOD + BrickRed_EndPiece`
#             place 260 wall instances across diverse maintown chunks,
#             all at sensible terrain heights (Y~-3, 125, 140).
#
# Verified across 800+ u0=0 sections in the Colorado pool. cull=60
# sections (267 sections / 5360 instances) cover the 166-section
# sentinel-bbox group plus 101 unique-bbox impostors. The handful of
# pure-impostor handle tuples that leak through at cull=80 (e.g.
# Commercial_09_LOD01 + corner07_LOD00, 1 section) are reduced to a
# single render handle by the per-handle LOD filter.
#
# u0=1 sections render directly regardless of cull (cull there is just
# a per-section distance LOD band) — only apply this filter to u0=0.
_U0Z_CULL_THRESHOLD = 80.0


_LOD_TAIL_RE = __import__("re").compile(r"_LOD(\d{2})_*$")


def _is_lod_pair(tag_a: str, tag_b: str) -> bool:
    """True if ``tag_a`` and ``tag_b`` are LOD0/LOD1 (or similar) variants
    of the same base asset. Used to keep only the LOD0 leg of a section
    whose handle list contains both LODs of the same asset.
    """
    ma = _LOD_TAIL_RE.search(tag_a or "")
    mb = _LOD_TAIL_RE.search(tag_b or "")
    if not ma or not mb:
        return False
    if ma.group(1) == mb.group(1):
        return False  # same LOD level — not a pair
    base_a = tag_a[:ma.start()]
    base_b = tag_b[:mb.start()]
    return base_a == base_b


def _classify_tag(tag: str) -> str:
    """Return 'keep' or 'drop' for a freeroam policy decision.

    Drop-list wins; everything else is kept. The previous keep-list
    approach forced the output to ~48 unique handles (visible as the
    "ring of shadow casters" artifact); compound-section emission
    (slots 0+1+2) needs an open-by-default policy to surface the
    diverse asset coverage v42k7 actually carries.
    """
    if any(tag.startswith(p) for p in _DROP_TAG_PREFIXES):
        return "drop"
    return "keep"


def _normalise_rot_scale(
    rot: tuple,
) -> tuple[list[list[float]], float]:
    """Decompose the position-table rotation rows into (R, scale).

    The three rows are the instance's local +X, +Y, +Z axes expressed
    in world space, scaled by a per-instance model-radius factor (~0.3-
    0.5 for typical decal/road sections, ~1.0 for unscaled assets).
    Empirically the row magnitudes match across axes — so scale is
    recovered as the average row magnitude and orientation as the
    unit-length axes. The data is already right-handed: verified on
    Models_Ungrouped_1524 sec[18] (festival OBJ_FEST_BarrierMetal),
    ``r0 × r1`` matches ``r2`` to within float noise. The earlier code
    negated r2 under an "LH→RH" assumption that produced reflected
    rotations — visible as the wrong orientation on asymmetric assets
    while symmetric ones (most barriers) survived the bug.

    Returns ``(rotation_3x3_as_nested_lists, scale_float)``.
    """
    axes: list[tuple[float, float, float]] = []
    mags: list[float] = []
    for i, row in enumerate(rot):
        m2 = row[0] * row[0] + row[1] * row[1] + row[2] * row[2]
        if m2 > 1e-12:
            mag = m2 ** 0.5
            inv = 1.0 / mag
            axes.append((row[0] * inv, row[1] * inv, row[2] * inv))
            mags.append(mag)
        else:
            axes.append(_IDENTITY_ROT[i])
            mags.append(1.0)
    # axes[i] is local axis i in world space → columns of R.
    # Transpose so out[i][j] = axes[j][i] (row-major rotation matrix
    # where R @ local_v = world_v).
    rotation = [
        [axes[0][i], axes[1][i], axes[2][i]]
        for i in range(3)
    ]
    scale = sum(mags) / 3.0
    return rotation, scale


def _normalise_rot(rot: tuple) -> list[list[float]]:
    """Backward-compat wrapper: return rotation matrix only."""
    return _normalise_rot_scale(rot)[0]


ProgressFn = Optional[Callable[[int, int], None]]


_LOD_NOLOD_RE = __import__("re").compile(r"(_LOD\d+_*|_NOLOD)$")


def _strip_lod_suffix(tag: str) -> str:
    """Normalise a tag for cross-source dedup against rmb_world.

    Drops `_LOD\\d+` (with optional trailing underscore) and `_NOLOD`.
    Used to detect when a v42k7 handle's tag is the same conceptual
    asset as one already placed by rmb_world's wrapper-rmb path —
    those are streaming/library references, not render targets.
    """
    return _LOD_NOLOD_RE.sub("", tag or "")


def extract_v42k7_instances(
    zip_path: Path,
    out_dir: Path,
    *,
    include_all_lods: bool = False,
    policy: str = "freeroam",
    progress: ProgressFn = None,
    exclude_tag_keys: Optional[set] = None,
    family_min_lod: Optional[dict] = None,
) -> dict:
    """Extract v42k7 → rmb instance placements.

    ``exclude_tag_keys`` (optional): set of LOD/NOLOD-stripped tag
    strings. Blobs whose tag, after the same strip, lands in this set
    are dropped — a streaming/library cross-reference filter that
    avoids duplicating placements already handled by rmb_world. The
    canonical use is to pass ``{strip(tag) for tag in rmb_world_tags}``;
    that suppresses ~30k duplicate v42k7 instances (cables, smelter,
    pavements, road segments, MT_Area decals, Steelworks, Object NNN)
    that would otherwise render in radial "rings" around the chunks
    that load them as visibility hints.
    """
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

    # Tag-by-handle cache for the LOD-pair check during slot picking.
    # Without this, every section's pair check re-decodes the same rmb
    # blobs — devastating for runtime on chunks with high section counts.
    from fh1_mapdecomp.rmb import parse_blob as _parse_blob_for_tag
    _tag_cache: dict[int, str] = {}
    def _tag_of(handle: int) -> str:
        if handle in _tag_cache:
            return _tag_cache[handle]
        try:
            t = _parse_blob_for_tag(read_entry(zip_path, rmb_entries[handle])).tag
        except Exception:
            t = ""
        _tag_cache[handle] = t
        return t

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
                         "rot": [list(r) for r in _IDENTITY_ROT],
                         "scale": 1.0}
                        for p in positions_xyz
                    ]
                    proc_inline_positions += len(positions_xyz)
                else:
                    # Last-resort fallback: anchor at chunk-bbox centre
                    # (only ~7 chunks hit this on Colorado).
                    instances_meta = [{
                        "pos": [cx, cy, cz],
                        "rot": [list(r) for r in _IDENTITY_ROT],
                        "scale": 1.0,
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
            # u0=0 cull-box filter for streaming-impostor sections.
            # cull_box[0] in {0, 60} marks streaming-only entries (the
            # 166-section sentinel-bbox group plus ~100 unique-bbox
            # sections that pair unrelated handles like Sign+Commercial
            # at festival positions); cull_box[0] >= 100 marks real
            # placements (festival barriers, road decals, building
            # modules, cables). u0=1 sections render directly so cull
            # only sets the distance LOD band there — the filter does
            # not apply to them.
            for sec_idx, sec in enumerate(layout.sections):
                # The v42k7 within-section selector is the per-section
                # **slot skip mask**: trailing u32 at +0x54 of the 88-byte
                # Table B record. Bit N set = SKIP slot N (don't render).
                # Validated against ground-truth chunk 1463 sec[5]
                # (festival barrier): mask=0x05=0b101 → skip slots 0+2 →
                # render slot 1 = OBJ_FEST_BarrierMetal_LOD00.
                # See docs/world-architecture.md §5.2.
                #
                # For 1-slot sections the field is sometimes re-purposed
                # (occasionally a float ~1.0); mask only the bits within
                # the section's actual slot count to be safe.
                raw_handles = [h for h in sec.rmb_handles if h != 0]
                if not raw_handles:
                    continue

                rec = (records[sec_idx]
                       if records is not None and sec_idx < len(records)
                       else None)
                u0 = rec.u0 if rec is not None else 1
                if u0 == 0 and rec is not None and rec.cull_box[0] < _U0Z_CULL_THRESHOLD:
                    continue

                # HIGHEST-SET-BIT rule (2026-04-28): the +0x54 byte's
                # highest set bit identifies the slot to render. Verified
                # by transform-spacing measurement:
                #   G06788 sec[8] mask=0x4 (bit 2) spacing=9.80m,
                #     slot[2]=Modular_004 (10.01m wide) ✓
                #   G06848 sec[12] mask=0x6 (bits 1,2) → highest=2 ✓
                #   G06632 sec[8]  mask=0x5 (bits 0,2) → highest=2 ✓
                #   G07014 sec[4]  mask=0x3 (bits 0,1) spacing=9.18m,
                #     slot[1]=Modular_020 (8.68m wide) ✓
                # Multi-bit masks aren't "render every set bit" — they
                # encode (impostor_slot..high_LOD_slot), and the engine
                # renders the highest LOD index. Lower bits are streaming
                # impostor companions to load.
                #
                # LOD-PAIR EXCEPTION: when the slot list is a same-base
                # LOD pair (LOD00/LOD01 of the same asset), pick the
                # lowest-LOD-index slot (highest detail) instead of
                # highest-bit (which would pick the LOD01 impostor).
                slot_count = len(raw_handles)
                raw_mask = rec.slot_skip_mask if rec is not None else 0
                in_range_bits = (1 << slot_count) - 1
                if raw_mask & ~in_range_bits:
                    continue  # high-bit deactivation
                mask = raw_mask & in_range_bits
                if mask == 0:
                    continue  # no slots active → streaming-only section
                # LOD-pair detection: get tag of slot 0 and slot 1 if both
                # exist and look at base names. Uses cached tag lookup.
                pick_slot = mask.bit_length() - 1
                if slot_count >= 2:
                    ta = _tag_of(raw_handles[0])
                    tb = _tag_of(raw_handles[1])
                    if ta and tb and _is_lod_pair(ta, tb):
                        pick_slot = 0  # take LOD00 (highest detail)
                handles = [raw_handles[pick_slot]]

                # Real placements live in the position table. No
                # chunk-anchor fallback — sections without instances
                # are metadata only.
                if per_section_instances is None:
                    continue
                if sec_idx >= len(per_section_instances):
                    continue
                section_insts = per_section_instances[sec_idx]
                if not section_insts:
                    continue

                instances_meta = []
                for inst in section_insts:
                    rotation, scale = _normalise_rot_scale(inst.rotation)
                    instances_meta.append({
                        "pos": list(inst.position),
                        "rot": rotation,
                        "scale": scale,
                    })

                sections_meta.append({
                    "handles": handles,
                    "instances": instances_meta,
                    "u0": int(u0),
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
            np.savez(blob_dir / name, positions=sp, faces=sf)
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
                # Per-family-min-LOD filter (consistent with rmb_world).
                # Within each stripped-tag family, keep only the LOD
                # variant matching the family's LOD floor (smallest LOD
                # number present in the pool = highest detail). For
                # families with no numeric LOD (cables_NNN etc.) keep
                # all. The family_min_lod dict comes from rmb_world's
                # pre-pass; if not provided fall back to the older
                # global "LOD00 only" rule.
                if blob.lod:
                    try:
                        lod_num = int(blob.lod)
                    except ValueError:
                        lod_num = None
                    if family_min_lod is not None:
                        stripped = _strip_lod_suffix(blob.tag)
                        target = family_min_lod.get(stripped)
                        if target is not None and lod_num != target:
                            skipped_lod.add(handle)
                            continue
                    else:
                        if blob.lod != "00":
                            skipped_lod.add(handle)
                            continue
                if "MIDDIST" in blob.tag:
                    skipped_lod.add(handle)
                    continue
            if policy == "freeroam":
                if _classify_tag(blob.tag) == "drop":
                    skipped_filter.append({"handle": handle, "tag": blob.tag})
                    continue
            if exclude_tag_keys is not None:
                if _strip_lod_suffix(blob.tag) in exclude_tag_keys:
                    skipped_filter.append({
                        "handle": handle, "tag": blob.tag,
                        "reason": "duplicate_with_rmb_world",
                    })
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
            np.savez(
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

    # LOD-pair de-duplication. For ANY section (u0=0 or u0=1), if the
    # slot list contains a LOD pair of the same base asset, keep only
    # the LOD0 leg — emitting both LOD00 and LOD01 at the same world
    # position double-renders, and the LOD01 sibling often uses a
    # different mesh-pivot Y so it sinks under the terrain.
    #
    # Real-vs-impostor selection for u0=0 sections happens earlier via
    # the cull_box[0] >= 100 gate; this pass only de-dupes LOD legs.
    handle_tag = {b["handle"]: b["tag"] for b in blobs_meta}
    sections_dropped_lod_check = 0
    for c in chunks:
        kept_sections: list[dict] = []
        for s in c["sections"]:
            hs = list(s["handles"])
            for i in range(len(hs) - 1):
                ta = handle_tag.get(hs[i], "")
                for j in range(i + 1, len(hs)):
                    tb = handle_tag.get(hs[j], "")
                    if _is_lod_pair(ta, tb):
                        ma = _LOD_TAIL_RE.search(ta)
                        mb = _LOD_TAIL_RE.search(tb)
                        la = int(ma.group(1))
                        lb = int(mb.group(1))
                        drop_h = hs[j] if la <= lb else hs[i]
                        hs = [h for h in hs if h != drop_h]
                        break
                else:
                    continue
                break
            if not hs:
                sections_dropped_lod_check += 1
                continue
            s = dict(s, handles=hs)
            kept_sections.append(s)
        c["sections"] = kept_sections
    chunks[:] = [c for c in chunks if c["sections"]]

    # Drop skipped handles from chunks so the importer doesn't have to filter.
    filtered_handles = {d["handle"] for d in skipped_filter}
    drop = skipped_terrain | skipped_lod | filtered_handles
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
