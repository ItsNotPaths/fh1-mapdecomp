# v42k7 section→mesh selection — what we know, what's heuristic, what's open

> **Read `world-architecture.md` §5 first** for the two-selector
> framing (section-level vs within-section). This doc focuses on the
> SECTION-level selector (solved: `cull_box[0]`) and the WITHIN-SECTION
> selector (open). Cross-source dedup against rmb_world is in
> `world-architecture.md` §6.

Captured 2026-04-28 after a long ground-truth session against a single
festival-area chunk in Colorado. The position-table side of v42k7 is
fully decoded; the *which mesh do we render at each instance position*
side is partially decoded.

**Update 2026-04-28 (later):** the missing selector for u0=0 sections
is **`Table B cull_box[0]`**, an f32 at +0x34 of each Table B record.
It cleanly partitions u0=0 sections into render bands:

  | `cull_box[0]` | role | example |
  |---|---|---|
  | 0   | metadata-only (`instance_count == 0`) | filler entries |
  | 60  | streaming impostor (do not render)    | sentinel-bbox group + 100 unique-bbox sections that pair Sign+Commercial / Maintown+Festival |
  | 80+ | real placement                         | maintown walls (BrickRedd_8M_NOLOD), BLDG_MainTown_Modular_*, cables, decals, festival barriers |

This subsumes both the earlier heuristics (sentinel-bbox and LOD-pair-
tail). Verified across 800+ u0=0 sections in the Colorado pool. Two
calibration rounds:

1. **Threshold = 100** recovered 688 unique-bbox non-LOD-pair sections
   (~20k instances of building modules, cables, road decals) that the
   LOD-pair filter was dropping; the 5 LOD-pair-tail sections at
   `cull_box[0] = 60` that the LOD-pair filter was keeping are impostors.
2. **Threshold = 80** (current) further added 4,761 instances and 8 new
   asset types (Maintown_WallSmall_BrickRedd_8M_NOLOD, BLDG_MainTown_
   Modular_001/004/015/018/021/024 etc.) — verified by spot-checking the
   `BrickRedd_8M_NOLOD + BrickRed_EndPiece_NOLOD` cluster (260 wall
   instances across 10 chunks, all in-chunk and at sensible terrain
   heights Y∈{-3, 125, 140}). The few pure-impostor handle tuples that
   remain at cull=80 (e.g. one section of `Commercial_09_LOD01 +
   corner07_LOD00`) reduce to a single render-handle after the
   per-handle LOD filter.

u0=1 sections render directly regardless of cull (cull there is just a
per-section distance LOD band: 60/70/75/100/130/.../220 are all valid
render distances for race barriers, cables, etc.) — the filter is
u0=0-only.

Implementation: `_U0Z_CULL_THRESHOLD = 80.0` in
`src/fh1_mapdecomp/v42k7_inst.py`. The earlier `_is_streaming_sentinel_bbox`
and "u0=0 must end in a LOD pair" gates were removed; the LOD0-leg-keep
de-dupe (drops LOD01 leg when both LODs sit in one section) is preserved.

**Update 2026-04-28 (later still): v42k7-vs-rmb_world cross-source dedup.**
Even after the cull-box gate, the visual output had radial "rings"
of duplicated assets (smelter, cables, decals, road segments, Object NNN).
Diagnosis: rmb_world's wrapper-rmb path *already places* these assets
via the named-section-refs mechanism (handle 6395 =
`PLA_SMELTER_BLDG_BlastFurnaceLow_001`); v42k7's references to the
sibling handle (61985 = `PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00`) are
streaming/visibility hints, *not* render targets — emitting them at
the v42k7 position-table positions produces concentric rings around
each chunk that loads the asset for streaming.

Fix: `extract_v42k7_instances` accepts `exclude_tag_keys` — the set of
LOD/NOLOD-stripped tags from rmb_world's blob list. Any v42k7 blob
whose stripped tag matches drops out. `cmd_v42k7_inst` reads
`<output>/rmb_world/index.json` if present and passes its tag set;
`cmd_all` reorders so rmb_world runs *before* v42k7_inst. Effect on
Colorado: 47 of 112 v42k7 blobs (~21k instances) suppressed — all
cables_NNN, smelter, Steelworks, Pavementcap, MT_Area decals, road
segments, Object NNN. The remaining 65 blobs are race-event dressing
(OBJ_BarrierBrand, S_Debris, GrandstandStraight, RV_Dawning,
OBJ_FEST_CanopyClosed, Armco) and v42k7-owned freeroam buildings
(BLDG_MainTown_Modular_003/004/019/024, Maintown_WallSmall_*).

---

## TL;DR

For each `Models_Ungrouped_*.pgeo` chunk we resolve every section to a
real `rmb` handle via well-understood pair-pointer indexing. **u0=0
sections additionally carry a `cull_box[0]` byte that gates rendering**
— `>= 100` renders, `< 100` is streaming/metadata only. Combined with
the per-handle LOD/tag filter (drop LOD01/LOD02/MIDDIST and race-event
dressing) this captures the engine's actual emission set for u0=0
sections.

u0=1 sections render directly without the cull gate; their `cull_box`
encodes a per-section distance LOD band (60/70/75/100/130/.../220).

Open: inner-circle festival assets (stages, signage, hub geometry)
appear to live in a placement source we haven't decoded yet. The
v42k7 pool simply doesn't carry them.

---

## What's solid (no guesswork)

### File envelope
- bin.zip / Turn 10 LZX framing
- rmb.bin pool indexing, blob/sub-blob headers, triangle strips
- v42k7 PGEO header at file offset 0x88: `section_count`,
  `table_a_count`, `table_b_count`, `vbuf_size`, `total_size`

### Section record (64 bytes per section, starting at file 0x88)
```
0x00..0x18   6 BE floats     local bbox (xmin, ymin, xmax, ymax, zmax, zmin)
0x18..0x1c   u32 BE          padding (always 0)
0x1c..0x34   3 × (cnt, ptr)  pair pointers (cnt is BE u32, ptr is LE u32)
0x34..0x40   12 bytes        residual data (overlaps section_handles array
                             for the last record; otherwise leftover floats)
```

Each pair has `cnt == 1` for active slots and `cnt == 0, ptr == 0` for
unused slots. Most sections use 2 slots; some use 3.

### Pair pointer → section_handles indexing
- `base_ptr = min` of all non-zero pair pointers across the chunk
- `n_handles = (max_ptr - base_ptr) / 4 + 1`
- `idx = (ptr - base_ptr) / 4`
- `section_handles[idx]` is a real BE u32 rmb pool handle

The `section_handles` array is located at `records_end + δ` for a small
δ ∈ {−16…+16}; we scan for the offset where every entry decodes as a
valid handle (1 ≤ value < 0x40000 or zero). Confirmed: the resolved
handles always have valid rmb tags.

### Table A (the "outer" handles)
`table_a_count` × 8B records of `(handle BE u32, pad BE u32 = 0)`. In
every chunk inspected these were exclusively LOD01 / MIDDIST landscape
impostors — Plains_Area*_MIDDIST, MT_Area01_Terrain*_LOD01. **Best
hypothesis:** Table A is a region-wide preload list for distant
streaming, not a per-section render selector.

### Table B (per-section attributes)
`section_count` × 88-byte records, separated by `0xFFFFFFFF` markers.
Layout (verified):
```
0x00..0x10   4 BE f32        attr (per-section LOD/cull weights, 0..1 floats)
0x10..0x20   4 BE f32        extra (uniform 7.78, 1.10, 0.75, ε)
0x20..0x34   5 BE u32        seq = (u0, section_idx, instance_count, u3, sep_mult)
0x34..0x44   4 BE f32        cull_box
0x44..0x48   u32 BE          pad (always 0)
0x48..0x54   3 LE u32        runtime_ptrs
0x54..0x58   u32             trailing
```

`runtime_ptrs[0]` and `runtime_ptrs[2]` are runtime memory addresses
for the section's slice of the position table — `[0]` is the slice
start, `[2]` is the slice end; consecutive sections line up with the
32B separator gap. They confirm the position-table layout but don't
encode handle indices. `runtime_ptrs[1]` reinterpreted as little-endian
f32 is a per-section cull/LOD constant (0..1 range).

`u0`, `u3` are the meaningful integer fields:
- `u0 ∈ {0, 1}` — see below
- `u3` varies per section, possibly an index into a per-zone array

### Position table (96B per instance)
```
0x00..0x0c   3 BE f32   world position
0x0c..0x10   f32 = 1.0  w
0x10..0x20   4 BE f32   section_constant (uniform across a section's instances —
                        looks like a 4-float color or UV value, not a handle)
0x20..0x2c   3 BE f32   up vector
0x2c..0x30   pad
0x30..0x40   row 0 of rotation matrix (3 BE f32 + 4-byte gap)
0x40..0x50   row 1 of rotation matrix
0x50..0x60   row 2 of rotation matrix
```

The rotation matrix's row magnitudes carry per-instance scale. The
4-byte gaps (+0x3c, +0x4c, +0x5c) are zero — verified on festival
BarrierMetal section.

The matrix is **right-handed already** — `r0 × r1 ≈ r2`. The earlier
code negated `r2` for an LH→RH conversion that doesn't apply, which
turned valid rotations into reflections (visible as wrong orientation
on asymmetric assets).

---

## What's heuristic but probably right

### `u0 = 1` sections render "directly"
Pair-pointer slots resolve to handles the engine renders without
indirection. Race barriers (`OBJ_BarrierBrand_LOD00`), cables, debris,
festival canopies (`OBJ_FEST_CanopyClosed`) all match where they
should be in-game. ~95% confidence.

### `u0 = 0` sections render iff `cull_box[0] >= 80`
The mesh "selector" we'd been hunting is not a per-section index — it's
a per-section render gate. Table B's `cull_box[0]` (the f32 at offset
+0x34 of each 88B Table B record) takes a small number of distinct
values; for u0=0 those map to engine roles:

- `cull_box[0] == 0`: metadata-only (always `instance_count == 0`)
- `cull_box[0] == 60`: streaming impostor — does not render
- `cull_box[0] >= 80`: real placement — render the slot list at the
  position-table positions, after the LOD0-leg de-dupe

This subsumes the older sentinel-bbox and LOD-pair-tail heuristics:
the 166-section sentinel-bbox group all live at `cull_box[0] = 60`,
and the LOD-pair-tail signal was just a partial proxy that missed the
real placements at `cull_box[0] >= 80` (~25k instances of building
modules, cables, decals, maintown walls, modular pieces).

For u0=1 sections cull_box just sets a render-distance LOD band
(60/70/75/100/130/.../220 are all valid); the gate does NOT apply.

### LOD0-leg de-dupe
For ANY section (regardless of u0), if the slot list contains both LODs
of the same base asset (e.g. `OBJ_FEST_BarrierMetal_LOD00 + _LOD01`),
keep only the LOD0 leg — emitting both at the same world position
double-renders, and the LOD01 sibling's mesh pivot Y often differs from
the LOD0 (probably centered vs. base-anchored), so it sinks under the
terrain.

### Right-handed rotation, no LH→RH negation
The rotation row triplet is already a valid right-handed basis;
negating `r2` produced reflections.

---

## What's open

### The festival inner circle is empty
v42k7 carries no plausible asset for the inner-circle barriers,
buildings, or stages. Best guess: they live in a placement source we
haven't decoded — possibly a separate XML manifest, a chunk type
filtered out by `_is_proc_subvariant` checks, or `Festival_Area5/6/Road_*`
rmb_world entries that exist but aren't connecting visually.

### The festival inner circle is empty
v42k7 carries no plausible asset for the inner-circle barriers,
buildings, or stages. Best guess: they live in a placement source we
haven't decoded — possibly a separate XML manifest, a chunk type
filtered out by `_is_proc_subvariant` checks, or `Festival_Area5/6/Road_*`
rmb_world entries that exist but aren't connecting visually.

### LODs sometimes render under the ground
LOD01 sibling has a different mesh-Y pivot than the LOD0 (probably
centered vs. base-anchored). Currently mitigated by dropping
LOD01/LOD02 globally — works visually, costs detail at distance.

### The proc subvariant inline 40B vbuf still emits placeholders
`models_proc_clrd_*` chunks contain real per-position 10:10:10:2
packed vertex data; we render synthetic pyramid placeholders for
trees / street / redrock classes. Decoding the inline mesh is its own
workstream.

---

## Concrete next-session probes

The cull_box[0] gate (above) closed the byte-diff probe. Remaining
work is on missing assets, not missing selectors:

1. **Search for the inner-festival assets in non-v42k7 PGEO variants.**
   `Models_Ungrouped_*` is one PGEO variant. The pool also has v44k5,
   crowd, animatedobjects variants — at least one of those may carry
   the festival hub's stages and signage.
2. **Disassemble the v42k7 chunk-load function in `default.bin`.**
   Confirm the cull_box[0] gate at the source level (the byte-level
   evidence is strong; an instruction-trace would be definitive). The
   same trace would identify any *secondary* per-instance selector for
   sections whose slot list still resolves to multiple alternative
   handles in the engine.
3. **Look for any single-cull u0=0 placement source that's still
   missed.** With threshold=80 the festival is broadly correct and
   the maintown walls/modular sections are populated. Inner-circle
   festival assets (stages, signage, hub geometry) are still empty —
   those almost certainly live in a non-v42k7 placement source.

---

## Reproducible probes

```bash
cd /run/media/paths/SSS-Core/python\ projects/fh1-mapdecomp

# Festival barrier chunk inspection
PYTHONPATH=src python3 -c "
from pathlib import Path
import json
from fh1_mapdecomp.binzip import list_entries, read_entry
from fh1_mapdecomp.pgeo import parse_header
from fh1_mapdecomp.pgeo_body.v42k7 import parse_layout, parse_table_b
zp = Path('/run/media/paths/SSS-Games/fh1-xex/4D5309C9/00007000/2DC7007B/media/tracks/colorado/bin.zip')
tags = {int(h): t for h, t in json.loads(Path('/tmp/rmb_tags.json').read_text()).items()}
for e in list_entries(zp):
    if e.filename != '__R00G06793.pgeo': continue
    d = read_entry(zp, e); h = parse_header(d)
    layout = parse_layout(d, h); records = parse_table_b(d, layout)
    for i, sec in enumerate(layout.sections):
        ts = [tags.get(hh, '?') for hh in sec.rmb_handles]
        rec = records[i] if records else None
        print(f'sec[{i:2d}] u0={rec.u0 if rec else \"?\"} inst={rec.instance_count if rec else 0:3d} {sec.floats}  {sec.rmb_handles} {ts}')
    break
"
```

Expected: chunk 1463 sec[5] is the real festival-barrier u0=0 section
(unique bbox `(-1.36, 0, 0.32, 1.11, 1.31, 0)`, handles
`[MT_Decals393, BarrierMetal_LOD00, BarrierMetal_LOD01]`); sec[7..9]
are sentinel-bbox impostors.

---

## Code pointers

- `src/fh1_mapdecomp/pgeo_body/v42k7.py` — `parse_layout`,
  `parse_table_b`, `parse_position_table`. Section-handle resolution
  happens here.
- `src/fh1_mapdecomp/v42k7_inst.py` — `extract_v42k7_instances`. The
  sentinel-bbox filter, LOD-pair filter, and per-handle LOD/tag drops
  live here. `_normalise_rot_scale` decodes per-instance rotation +
  scale.
- `src/fh1_mapdecomp/blender_scripts/import_world.py` —
  `_make_v42k7_instance_gn`. Geometry Nodes group that instances
  meshes with `rot_euler` and `inst_scale` per-point attributes.
