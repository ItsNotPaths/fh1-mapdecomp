# `rmb.bin` sub-blobs and v42k7 proc subvariant

This doc covers two previously-undecoded structures surfaced during the
2026-04-18 extraction pass:

1. **rmb.bin sub-blobs** — secondary geometry appended after a primary
   blob, invisible to the §1 parser in `docs/rmb-bin.md` until this
   pass. For 18.1 % of pool entries (24,650 sub-blobs across 102,012
   entries) the primary vertex buffer is tiny (e.g. `vc=4`) and the
   authored mesh lives entirely in the sub-blob.
2. **v42k7 proc subvariant** — `models_proc_clrd_*` PGEO chunks that
   `parse_layout` silently dropped (`return None`) because the tag name
   lives at a different offset and the section records use a
   different-sized bbox block.

Both are implemented in `src/fh1_mapdecomp/rmb.py` and
`src/fh1_mapdecomp/pgeo_body/v42k7.py`.

## 1. rmb.bin sub-blob layout

Each rmb.bin entry is **one primary blob followed by zero or more
sub-blobs**. The primary blob is the 0xb4-byte header variant
documented in `rmb-bin.md §1`. Sub-blobs sit sequentially after the
primary's section list and use a compact 0x40-byte header with no 4×4
transform and a single pair of centroid/bbox vectors.

### 1.1 Header

Offsets relative to the sub-blob start:

    0x00  u32 BE   marker        (=5)
    0x04  u32 BE   ?             (=1 in all observed samples)
    0x08  3×f32    centroid
    0x14  u32      pad 0
    0x18  3×f32    bbox_min
    0x24  u32      pad 0
    0x28  3×f32    bbox_max
    0x34  u32      pad 0
    0x38  u32      tag string count (=1)
    0x3C  u32      tag string length T
    0x40  bytes×T  tag (ASCII, no padding)
    --    u32      stream block count (=3)
    --    u32      vertex count
    --    u32      vertex stride (same valid set as primary: 16..36)
    --    u32      pad 0
    --    bytes    vertex data (vcount × stride)
    --    bytes    section block (same grammar as primary, see rmb-bin.md §1)

Everything after `tag` is structurally identical to the primary. The
only differences are the absence of the 16-float identity transform and
the single (not duplicated) centroid.

### 1.2 Discriminator vs section continuation

Sub-blobs always start with the byte sequence
`00 00 00 05 00 00 00 01`. **The same byte sequence also prefixes a
section continuation marker within a blob** (`[05]` between sections is
the continuation flag, followed by `[01][02]` = one stream, string
type). So the prefix alone is ambiguous.

The discriminator is the third u32:

- **Section continuation**: next u32 is `0x00000002` (the string stream
  type code).
- **Sub-blob**: next u32 is the first dword of the centroid float —
  never `0x00000002` for any plausible world-space value.

This is the `SECTION_CONT_TYPE = 2` check in `rmb.py`.

### 1.3 Scan + parse order

`_find_subblob_starts(buf, search_from)` scans for the 8-byte prefix
starting after the primary's vertex buffer and validates each hit by:

1. Reading a prospective 0x40 header.
2. Confirming `centroid`/`bbox_min`/`bbox_max` parse as float triples
   with a valid `tag_len` and the right stream header shape.
3. Parsing a trial `(vcount, stride)` pair and checking `stride ∈
   VALID_STRIDES`.

The list of validated starts bounds each region:

- Primary sections: `[post_primary_vbuf, first_subblob_start)`
- Sub-blob *i* sections: `[post_subblob_vbuf_i, subblob_start_{i+1})`
  (last one: to end of buffer).

Both `_scan_index_streams` and `_parse_sections` accept an optional
`end` parameter to honour these bounds.

### 1.4 Coordinate frame

Empirically verified on two samples spanning the observed range:

- **Terrain blob** (handle 4, world-space): sub-blob positions land in
  the same world-space frame as primary positions (kilometre-scale Y
  values matching the site's topography).
- **Grandstand blob** (handle 61909, local-space): sub-blob positions
  are small (10..100 m) and centred on origin, matching the primary's
  local-space convention.

**Conclusion**: sub-blobs share the primary's coordinate frame. They
can be concatenated vertex-wise and their triangle indices offset by
the running vertex count — no transform needed.

### 1.5 Pool-wide statistics (Colorado, 102,012 entries)

- Entries with ≥1 sub-blob: **18.1 %** (~18,474 entries).
- Total sub-blobs unlocked: **24,650**.
- Per-entry distribution: 1 sub-blob is typical; up to 4+ observed on
  heavy-asset buildings.
- Example: handle 61909 `GrandstandStraight_LOD00_`
  primary `vc=4` (effectively empty) vs sub-blob `vc=1601, tri=801` —
  the real grandstand mesh is entirely in the sub-blob.

### 1.6 `RmbSubBlob` dataclass + merge convention

```python
@dataclass
class RmbSubBlob:
    tag: str
    lod: str
    centroid: tuple[float, float, float]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    vcount: int
    stride: int
    voff: int
    positions: np.ndarray        # (vcount, 3) f32
    sections: list[RmbSection]   # same type as primary sections
```

`RmbBlob.sub_blobs: list[RmbSubBlob]` is the attach point.
`v42k7_inst.py` merges them for export:

```python
pos_list = [blob.positions]
tris_list = [s.triangles for s in blob.sections if s.triangles.size]
vert_offset = blob.vcount
for sub in blob.sub_blobs:
    pos_list.append(sub.positions)
    for s in sub.sections:
        if s.triangles.size:
            tris_list.append(s.triangles + vert_offset)
    vert_offset += sub.vcount
```

Triangle indices inside each `RmbSection` are local to that blob's
vertex buffer, so the running `vert_offset` is critical. The merged
positions/faces go into a single `blobs/HHHHHH.npz` per handle.

## 2. v42k7 proc subvariant (`models_proc_clrd_*`)

PGEOs whose descriptor name matches `models_proc_clrd_*` (and some
`models_proc_fest_*`) follow the v42k7 body family but differ from
regular `Models_Ungrouped_*` chunks in three respects: name location,
record stride, and pair slot offsets.

### 2.1 Detection

    buf[0x60 : 0x64]  ==  b'\xff\xff\xff\xff'    # sentinel
    name lives at     0x98                       # null-terminated ASCII

The regular subvariant has a printable name starting at `0x60` and no
sentinel. `_is_proc_subvariant(buf)` does the 4-byte compare.

### 2.2 Variable records offset

After the name null terminator, the records table is 16-byte aligned:

    records_offset = (null_pos + 16) & ~15

Observed `records_offset` values: `0xd0`, `0xe0`. Using a fixed
`0xd0` produced 355 / 2,114 parse failures until the dynamic formula
was introduced; it now parses **0 / 2,114** chunks with failures on
Colorado.

### 2.3 Record layout

Each section record is a fixed-size struct. The regular subvariant
packs its bbox as six f32 (24 bytes, no alignment pad); the proc
subvariant packs it as two 4-aligned f32 triplets (16 + 16 = 32 bytes):

    regular (from offset 0 of record)          proc (from offset 0 of record)
    0x00  3×f32   bbox min (packed)            0x00  3×f32   bbox min
    0x0c  3×f32   bbox max (packed)            0x0c  pad 4
                                                0x10  3×f32   bbox max
                                                0x1c  pad 4

Because the bbox block is 8 bytes larger, every later slot shifts by
+8:

| slot              | regular offset | proc offset |
| ----------------- | -------------: | ----------: |
| pair_offsets[0]   |         `0x1c` |      `0x24` |
| pair_offsets[1]   |         `0x24` |      `0x2c` |
| pair_offsets[2]   |         `0x2c` |      `0x34` |

The pair slots carry LE pointers into the runtime base_ptr; handle
resolution reuses the existing arithmetic in `parse_layout`.

### 2.4 What proc chunks actually reference

The section handle stream produced by `parse_layout` for proc chunks
is a small cluster of indices in `[61882, 61931]` mapping almost
entirely to `LOD01`/`LOD02` tags + `TERR_CUBE_*` patches — none of
which are the geometry the descriptor names imply (trees, street
barriers, redrock fencing). **The handle stream is not the geometry
source.** Real placements live in the trailing inline vertex buffer
(when present) or at the chunk anchor.

#### Inline 40B packed-position vbuf

When `body_header.vbuf_size != 0` (offset `0x50`), the file ends with
`vbuf_size` bytes of inline records followed by a 4-byte trailer:

    [ N × 40B records ][ 4B trailer ]
    N = vbuf_size / 40

Each 40B record:

    +0x00  4B    pre-position field (record-local; not decoded)
    +0x04  u32 BE    packed position (10:10:10:2)
    +0x08  ...   per-record attributes (normal? colour? not decoded)
    +0x20  e3 d0    fixed anchor — validated on first/middle/last record
    +0x22  ...

The packed u32 at +4 splits into three 10-bit fractions:

    x10 = (u >>  0) & 0x3FF
    y10 = (u >> 10) & 0x3FF
    z10 = (u >> 20) & 0x3FF
    pos = bbox_min + (xyz / 1023) × (bbox_max - bbox_min)

(The 2-bit MSB at bit 30..31 is presumed `w` and ignored.)

This is the **same packing** used by the regular subvariant's per-vertex
point-cloud preview in `decode()` — the only difference is interpretation:
in proc chunks, each record is one **world-space placement** (e.g. one
tree), not a vertex. There is no rotation table; orientation is
identity (or runtime-randomised, not encoded).

**Coverage on Colorado:** 26 / 469 proc chunks carry an inline vbuf —
all `models_proc_clrd_trees_*` chunks with multi-tree areas — yielding
167 placements. 100% fall inside their chunk bbox (sanity verified).
The remaining 443 proc chunks have `vbuf_size == 0`, of which 375 are
drop-list (`festival`, `barriers_`, `fest_`, `multiplayer`, `showcase`,
`prhub`, `pr_`, `foot_`, `track_`) and 68 are keep-list (no-vbuf
`trees_*`, `street_*`, `redrock_*`/`redstone_*`).

#### Proc 96B position table

For keep-list chunks the real per-position data lives in the same
**96B-stride position table** the regular subvariant uses, but with
the rotation rows holding cull/fade-distance constants instead of a
real rotation. The standard `_rot_is_placement` filter (used by
regular chunks to drop the trailing per-section metadata record)
incorrectly rejects every entry. The proc path therefore calls
`parse_position_table(..., keep_all_rotations=True)` and substitutes
identity rotation, then guards each entry with an in-bbox check to
reject any stray sentinel slots that slip past the count.

**Coverage on Colorado:** 87 of 94 keep-list proc chunks contribute
position-table entries — `street_*` 1337, `trees_*` 334, `redrock_*`
51 — for **1722** placements. Combined with the 167 inline-vbuf
positions and 7 chunk-anchor fallbacks, the proc path emits **1805
placements across 94 chunks** (`{trees, street, redrock}`), up from
235 with chunk-anchor-only fallback (~7.7×).

#### Synthetic placeholder blobs

Because the inline vbuf encodes only positions, the v42k7_inst
extractor emits each placement against a **synthetic blob handle**
keyed by descriptor class:

| class    | synthetic handle | placeholder mesh                     |
| -------- | ---------------: | ------------------------------------ |
| trees    | `0x100000`       | upright trunk + crown (~6.5m tall)   |
| street   | `0x100001`       | low 3×1m horizontal barrier slab     |
| redrock  | `0x100002`       | small 2.5m boulder pyramid           |

Handles sit well above the rmb pool size (`<0x40000`) so they cannot
collide with real handles. The placeholder meshes are written to
`out/v42k7_inst/blobs/` and consumed unchanged by the existing Blender
import path; nothing in the importer changes.

The placeholder shapes are not the authored meshes — locating those
(likely a small set of asset library entries indexed by the descriptor
suffix) is the next step.

## 3. Impact on the Colorado export

- rmb.parse_blob now returns merged primary + sub-blob geometry for
  every referenced handle. Blob meta in `out/v42k7_inst/index.json`
  gains a `sub_blobs` count field and `vcount` reflects the merged
  total (primary + sub-blobs).
- **Measured on Colorado v42k7_inst output:**
  - `--policy all` — 70 blobs total, **7 carry sub-blobs**:
    `GrandstandStraight_LOD00_` (×2 handles, primary vc=4 →
    merged vc=1605, tri=803), `Countdown_Left`,
    `MT_Area01_Blend07_LOD00`, `MT_Area03_Pavements08_LOD00`,
    `OBJ_CLRD_SignB_NOLOD_001` (×2 handles).
  - `--policy freeroam` (default) — 45 blobs, **only 2 retain
    sub-blobs** (`MT_Area01_Blend07_LOD00`,
    `MT_Area03_Pavements08_LOD00`). The other 5 are race dressing
    dropped by the filter.
  - **Takeaway:** sub-blobs are the primary geometry source for
    race-dressing assets (Grandstand, Countdown, sign billboards),
    not for freeroam content. The pool-wide 18.1% / 24,650 figure is
    dominated by non-referenced or race-only handles. Freeroam
    `.blend` output gains 2 tiles' worth of authored topology — a
    correctness fix, not a large visible diff.
- Proc subvariant chunks parse cleanly end-to-end but contribute
  near-zero new geometry until the inline vbuf is decoded.

## 4. Still open

- **Proc placeholder → real meshes.** The inline vbuf decode
  (§2.4) gives positions; per-class placeholder boxes stand in for
  the authored trees / barriers / boulders. Mapping descriptor name
  to the actual asset (and its rmb handle) is the obvious follow-up.
- ~~**Proc no-vbuf trees with sec≥2.**~~ Resolved by the 96B
  position-table path documented above (`keep_all_rotations=True`).
- **rmb primary index buffer** for non-TERR primary vbufs with u16
  indices and interleaved `Material__NN` strings is still a point
  cloud; see `rmb-bin.md §3`. Sub-blobs solved the vertex-count
  problem for many assets but several primaries still have authored
  topology we don't read.
- **Sub-blob vertex attributes** (bytes past position per record) are
  not decoded — same TODO as primary records.
