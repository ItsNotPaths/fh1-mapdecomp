# PGEO — body-layout notes (post-header, byte 52+)

Status: **Phase B probe only**. We have empirical observations across all 7
variants from 14 small samples + one mid-sized sample per variant, but no
full parser yet. This document is the spec under construction — add to it
as each variant is decoded.

Source samples sit under `out/probe/<variant>/`:
- `*.raw`  — the decompressed PGEO bytes.
- `*.txt`  — automatic structural summary (`src/probe_bodies.py`).
- `LARGE_*` — one mid-sized sample per variant (`src/probe_large.py`).

All offsets below are **relative to the start of the file** (i.e. include
the 52-byte PGEO header). Body starts at `0x34`. All multi-byte ints and
floats are big-endian.

---

## 0. Variant ↔ engine class table

Confirmed against the xex RTTI dump
(`docs/xex-walk/04-rtti-classes.txt`, namespace
`proceduralGeometry::CProcedural*`). Use this table when classifying a
new variant — the engine class name nails down what the file *actually*
contains, independent of file-tag prefix.

| `(ver,kind)` | Variant label   | Engine class                    | Tag prefix                              | Content                                       |
|:-------------|:----------------|:--------------------------------|:----------------------------------------|:----------------------------------------------|
| (42, 2)      | `grass`         | `CProceduralVegetation`         | `Grass_Ungrouped_*`                     | Foliage scatter (grass tufts AND tree/shrub instances). The "real vegetation" class. |
| (42, 3)      | `crowd`         | `CProceduralCharacters`         | `crowd_*` / `CROWD_*`                   | Spectator placements + a few inanimate prop loops (barriers / stalls). |
| (42, 7)      | `v42k7`         | `CProceduralModels`             | `Models_Ungrouped_*` / `models_proc_clrd_*` | Per-section instance groups referencing rmb pool entries; or proc-inline placements. |
| (42, 8)      | `terrain`       | `CProceduralLightMaps`          | `LightMap_XX_YY`                        | Terrain tile + lightmap UVs (no per-tile heights — those live in rmb pool). |
| (43, 6)      | `light_glows`   | `CProceduralLightGlows`         | `Glow_*`                                | **Light entity data** (position + direction + cone + color + intensity). Not foliage despite the historical "vegetation" label. |
| (44, 4)      | `landmark_anim` | `CProceduralAnimatedObject`     | `Anim_ANIM_*`                           | Festival rides, autoshow rigs, fireworks emitters. |
| (44, 5)      | `v44k5`         | (`CProceduralPoints`?)          | `anim_proc_clrd_fx_*`                   | Procedural FX emitter placement (geyser etc.). RTTI class unconfirmed. |

Other RTTI classes that appear in `proceduralGeometry::` but are **not**
disk-serialized as a distinct PGEO `(ver,kind)` we've seen:
`CProceduralPoints`, `CProceduralBillboards`, `CProceduralCharacterManager`,
`CAnimatedReplayManager`. Some of these may be runtime-only; others may
be subvariants we haven't separated out.

> **Watch for the `light_glows` ↔ `grass` mixup.** The PGEO variant
> historically labeled `vegetation` is the engine's *light glow* class.
> The engine's actual `Vegetation` class is what we label `grass`. Treat
> any document or code that says "vegetation = trees" as stale and
> probably wrong about something.

---

## 1. Shared prolog (variant-agnostic)

All 7 variants share the same byte layout for the first ~0x2c bytes of
body. Every field is confirmed across ≥3 samples per variant.

| Off   | Size | Type    | Field                                         | Notes |
| ----: | ---: | ------- | --------------------------------------------- | ----- |
| 0x34  |   8  | 2×f32   | `local_scale` — second scale pair             | Often but not always equal to the header scale. Terrain: (-1,-1). v42k7: (-1, +350.0). Semantics TBD. |
| 0x3c  |   4  | u32 BE  | `n_major` — major item count                  | Values seen: 15, 22, 24, 27, 32, 33, 41, …  Correlates loosely with file complexity. |
| 0x40  |   4  | u32 BE  | `n_minor` — secondary count                   | Usually 0 or 1. v42k7 LARGE = 8. |
| 0x44  |   4  | u32 BE  | `0` — padding                                 | Always 0 in samples. |
| 0x48  |   4  | u32 BE  | **`total_size`** — **matches `len(file)`**     | **Universal anchor.** Verified on all 14 samples including the 19,476-byte landmark_anim. |
| 0x4c  |   4  | u32 BE  | `sec_offset` — offset into file, or 0         | Zero on terrain / v44k5 / grass / light_glows / crowd. Non-zero on v42k7 (0x2a58 = 10840) and landmark_anim (0x4764 = 18276). Strong "section table / texture block start" hint. |
| 0x50  |   4  | u32 BE  | `n_aux` — auxiliary count                     | Zero or small: seen 0, 14, 40, 83. v42k7 LARGE = 14 (`0x0000000e`). |
| 0x54  |   8  | bytes   | `0` ×8 — reserved                             | All zero in samples. |

### The "0x14 invariant"
`u32_BE(file[0x48])` equals `len(file)` for every sample we have. This is
the strongest single anchor and should be used as a sanity check at the
start of any variant decoder: a mismatched value means the file is
truncated or we're parsing the wrong offset.

---

## 2. Identifier string

Immediately after the shared prolog, every variant writes a **null-
terminated ASCII tag** that identifies the chunk's purpose. The tag start
position is not *quite* uniform across variants:

| Variant        | Tag start | Example                                       |
| -------------- | :-------: | --------------------------------------------- |
| terrain        |   0x60    | `LightMap_97_53`                              |
| grass          |   0x60    | `Grass_Ungrouped_15149_0`                     |
| light_glows    | 0x60/0x98 | `Glow_WorldStaticLightGlows_1309` (Format A) / `Glow_Gameplay_FR44_L_0` (Format B) |
| v42k7          |   0x60    | `Models_Ungrouped_1929`                       |
| v44k5          |   0x60    | `anim_proc_clrd_fx_geyser_0`                  |
| crowd          |   0x60*   | `crowd_proc_clrd_festival` (then a secondary `crowd_stages_136` and a numeric suffix) |
| landmark_anim  |   0x54    | `tga` (a short texture-type tag) followed at 0x60 by `Anim_ANIM_Firework_001_SmallSparkle13` |

\* Crowd has a 4-byte `-98d`-style field right before 0x60. We assume the
real name always starts at 0x60 and crowd's pre-string bytes are a
secondary count/hash; confirm when decoding crowd for real.

**Takeaway**: a decoder can read the tag as
`cstring_at(file, 0x60, max_len=128)` for every variant except
`landmark_anim` where the primary tag sits at 0x60 and 0x54 is a
type-prefix.

### Tag → content convention

| Prefix                        | Implies                                    |
| ----------------------------- | ------------------------------------------ |
| `LightMap_X_Y`                | terrain tile at lightmap grid cell (X,Y). |
| `Models_Ungrouped_N`          | generic model (v42k7).                    |
| `Grass_Ungrouped_N_M`         | scattered grass patch (grass).            |
| `Glow_WorldStaticLightGlows_N`| static light-glow entity (light_glows). The variant carries pure light data — position, direction, cone, color, intensity — not foliage. |
| `Glow_Gameplay_*_L_N`         | per-event gameplay light glow (race-checkpoint markers, dirigible/plane lights, festival event lights). Same `light_glows` variant, Format B. |
| `anim_proc_clrd_fx_*`         | procedural animated effect (v44k5).       |
| `crowd_proc_clrd_*` + `crowd_stages_N` | animated crowd mesh (crowd).     |
| `Anim_ANIM_*` + `ter.tga` etc | rigged/animated landmark with texture references (landmark_anim). |

---

## 3. Per-variant observations

### 3.1 terrain (ver=42, kind=8) — 2,288 files

Body size ranges from 116 bytes (tiles with barely any content) to several
KB. Structure after the shared prolog:

```
0x60   C-string  "LightMap_XX_YY\0"  (14 chars + pad to 4-byte bound)
0x70   2×f32 BE  lightmap origin world (x,z) — e.g. (-6080.0, -6224.0)
0x78   f32 BE    1.0                          (unit scale? tile size?)
0x7c   u32 BE    102                          (repeated across all terrain samples — constant?)
0x80   u32 BE    80                           (same — constant?)
0x84   u32 BE    128                          (same — likely lightmap resolution, 128×128)
0x88   2×f32 BE  bbox_min.x, bbox_min.z       (re-stated from header)
0x90   u32 BE    lightmap_col                 (the XX from the name)
0x94   u32 BE    lightmap_row                 (the YY from the name)
0x98   u32 BE    material/texture hash        (0x20477000c, 0x20477012, 0x305f9e32, 0x3051172e observed)
0x9c   u32 BE    n_strips                     (1 in tiniest file; 25 and 489 in larger)
0xa0 … end       index buffer (see below)
```

**No vertex buffer is present in terrain chunks.** This is the key
empirical finding. Terrain vertices must live elsewhere — either in a
shared heightmap referenced by the material hash, or in a global grid
indexed by `(col, row)`. This matches roadmap §3's "terrain is a regular
grid" assumption but implies the grid positions are not stored per-tile.

#### Index-buffer framing

Starting at ~0xa0, the index data is a sequence of **16-byte rows**:

```
00 01 00 02 00 17 00 18    four u16 BE triangle-strip indices
FF FF                       strip-restart sentinel
00 00 00 00 00 00           6 bytes of zero padding
```

Where all 16 bytes are `FF FF 00 00 00 00 00 00 00 00 …` the row is a null
strip (used to pad the array to a fixed stride). This implies the tile is
stored as a **fixed-size strip table** sized by `n_strips`, with unused
slots zeroed — not a packed stream.

Concretely for `__R00G05782.pgeo` (308 body bytes):
- 9 real strips, each `[k, k+1, k+23, k+24]` (regular lattice pair) —
  i.e. tiny quad strips for a 2×12 border between neighbours.
- Followed by many `FF FF 00 00 00 00 00 00 00 00 00 00 00 00 00 00` pad
  rows to reach the fixed body size.

So the concrete decoding plan for terrain is:
1. Read metadata + material hash.
2. Read the strip table until EOF; skip rows that are all-`FFFF`/zero.
3. Use `(col, row, lightmap_origin, pitch=128)` to compute world positions
   for the referenced indices, sampling **a heightmap yet to be
   identified**. Candidate sources: `.sh` files (which encode world
   coordinates in their filenames), a shared `terrain_heights.bix`, or
   the `material/texture hash` at 0x98 (likely a lightmap, not height).
4. Open question: where does the Y (elevation) data live? Candidates:
   `.fiz`, `.sh`, a vertex buffer in a "master" PGEO, or per-chunk
   elsewhere in bin.zip indexed by hash. Worth probing the hash at 0x98
   against bin.zip filenames next.

**Status**: strips decoded structurally but the geometry is useless
without the height source.

**Update 2026-04-17**: the terrain-height source is **not** referenced
by the PGEO body at all — it lives in the sibling `rmb.bin` geometry
pool as `TERR_*_LOD00_*` blobs. See `docs/rmb-bin.md`. A v0 flat-quad
decoder still ships in `pgeo_body/terrain.py` (y=0 two-triangle tile)
as a fallback; the real elevation mesh is emitted by the
`fh1-mapdecomp terrain-hi` pipeline and linked into Blender under a
separate `fh1_terrain_hi` collection. Open remaining work is the
`rmb.bin` index + material block (still unparsed; currently Delaunay-
triangulated at import time).

---

### 3.2 v42k7 (ver=42, kind=7) — 14,122 files

Models, buildings, road pieces, and procedural-instance placement.
Body sizes 336 bytes to hundreds of KB. Two distinct sub-variants
share the same `(version, kind)` — see §3.2.1 and §3.2.2 below.

**Status**: decoded to point-cloud (Phase 2 result). `pgeo_body/v42k7.py`
ships. No triangle indices exist in-file — see §3.2.3 for why and what
is ruled out.

> **Update 2026-04-17** — the "point cloud" framing is superseded. v42k7
> is an **instance-group file** that references `rmb.bin` geometry by
> direct index. The inline 40-byte vertex records are not a mesh; they
> are per-group placement/OBB data. See §3.2.5 for the Table A / Table B
> discovery and the path to real prop geometry.

Vbuf framing (confirmed across 12,087 / 14,122 samples):

```
u32 BE @ body 0x50 = vbuf_size (in bytes)
vbuf_start         = len(file) - vbuf_size - 4
vbuf               = 40-byte records, each with anchor e3 d0 at byte 32
4-byte trailer at end of file
```

Per-record layout (partial):

```
bytes  0- 3  ?? padding
bytes  4- 7  u32 BE  packed 10-10-10-2 position (x,y,z fractions of header bbox)
bytes  8-11  u32 BE  second 10-10-10-2, usually ≈0x001ff800 — likely packed normal/tangent
bytes 20-27  ≈DEC3N  packed normal vectors
bytes 32-35  e3 d0 ?? ??   anchor + 2-byte per-file tag
bytes 36-39  ff 8X 8X 8X   DEC3N-packed (color?)
```

Packed-position decoding matches header bbox to <0.1% — verified on
the LARGE sample.

#### 3.2.1 "Real" sub-variant (vbuf_size > 0) — 12,087 files

Tag at body 0x60 reads `Models_Ungrouped_N`. Inline 40-byte record
block at the file end holds per-vertex data; no triangle indices
anywhere in the file (see §3.2.3).

Pre-vbuf region contains:

- The shared prolog (§1).
- Two f32 triplets at body 0x80..0x9c (bbox restated / centroid).
- A count+hash table starting at body 0xa4, pairs of
  `(count_u32, u32 "hash")`. These "hashes" are **not** asset hashes
  — see §3.2.4.
- For larger bodies, further zero-padding and a cluster of u32 BE
  values near body 0x1c0 whose meaning is still open — see §3.2.3.

#### 3.2.2 "Procedural-instance" sub-variant (vbuf_size == 0) — 2,035 files

1,977 / 2,035 have **no ASCII tag at 0x60**; the descriptor sits at
body ~0x98 and reads `models_proc_clrd_<category>_<name>_N` —
examples:

```
models_proc_clrd_trees_redstone_area03[freeroam]_0
models_proc_clrd_festival_fest_005_barriers_2
models_proc_clrd_festival_barriers_fest_008_20
```

These are **per-instance placement records**, not meshes:

```
0x34..0x5f  shared prolog (vbuf_size @0x50 is 0 for this sub-variant)
0x60        ff ff ff ff      (sentinel marking "no inline vbuf")
0x98        cstring           models_proc_clrd_…
0xc8..      3× f32 BE triplets (world position, scale, orientation?)
0xf8..      count+hash pairs  (same GPU-pointer shape as §3.2.1)
```

Geometry is provided by a shared template mesh referenced by name;
template mesh storage is not yet identified (candidates: the 429
`.bundle` files, or `rmb.bin` entries matched by the procedural name).

Currently falls back to a bbox cube in the Blender import. Decoding
this sub-variant needs the procedural-template lookup resolved.

#### 3.2.3 Where the triangle indices *aren't* (ruled out 2026-04-17)

After Phase A scouting (`out/probe/scout/REPORT.md`):

1. **No u16 BE / u16 LE / u8 triangle list or strip** survives a
   value-range sweep (`max < nvert`) anywhere in the pre-vbuf region
   `[0x34, vbuf_start)`, across 9 samples spanning nvert 6..856.
   0 substantial hits, any encoding.
2. **No sibling `.idx` / `.tri` / `.vb` / `.bix` / etc.** in bin.zip
   shares a stem with any `.pgeo`. PGEOs are self-contained against
   the filename namespace.
3. **The "count+hash" pointer table is not a bin.zip lookup key.** Of
   304 unique u32 values pulled from 2,035 empty-vbuf files, 0 match
   any `_0xHHHHHHHH_B.*` filename or any filename as a substring.
   Values form arithmetic sequences (`0xXX794404` pattern,
   `0xXXd46a37` pattern) with a constant low-24-bit base and a
   step of 4 in the high byte — a **GPU memory-pointer** signature
   (compile-time-baked VRAM offsets), not content hashes.

Open signal that is *not* yet explained: every decodable v42k7
sample has a contiguous run of u32 BE values near body offset 0x1c0
clustering in `0xf1c0..0xf200` (values 61900..61950), appearing as
**sequential pairs** (e.g. 61933, 61934, 61939, 61940 in
`__r00g07619.pgeo`). These read exactly like indices into an
external ~64 K-entry buffer, but the buffer is not identified —
cannot be an `rmb.bin` index (range only 0..14,559) and cannot be
local (max local nvert observed = 1,212).

Most plausible remaining home for the real triangles is the
`coloradoout.NNNNN.rmb.bin` geometry pool
(see `docs/bin-zip-layout.md §3`), keyed by something other than
filename hash. REing the `rmb.bin` format is a separate workstream.

> **Resolved 2026-04-17** — see §3.2.5. The "0xf1c0..0xf200 cluster"
> IS a rmb.bin index table. The reason 14,559 looked too small is that
> `rmb.bin` contains 102,012 entries across 14,291 distinct rmb.bin
> *files*, not one file. Handles index into the flattened list of all
> `*.rmb.bin` entries in the colorado bin.zip.

#### 3.2.4 Record-count irregularity

v42k7 vertex counts observed (6, 11, 14, 30, 124, 196, 218, 271,
318, 377, 856, 1212) are not systematically divisible by 3 (tri
list) or 4 (quad list), ruling out implicit stride-by-position
topology. The records are vertices in the graphics sense; the
connectivity is elsewhere.

#### 3.2.5 v42k7 is an rmb.bin instance-group file (breakthrough 2026-04-17)

Discovered while re-probing the LARGE sample
(`out/probe/v42k7/LARGE___R00G07486.raw`, 20,008 B, nvert=271).

##### Index resolution

Colorado's `bin.zip` contains **102,012** `*.rmb.bin` entries across
14,291 distinct `coloradoout.NNNNN.rmb.bin` filenames (confirmed via
`list_entries`). Flattened into order-of-appearance, each entry gets a
global index 0..102011. v42k7 handles are `u32 BE` values that index
directly into this flat list.

Sampled resolutions on LARGE:

| handle (hex) | handle (dec) | rmb file                      | tag prefix                        |
|:-------------|-------------:|:-------------------------------|:----------------------------------|
| `0x0000f1ce` | 61902        | coloradoout.00147.rmb.bin     | `S_Debris`                         |
| `0x0000f1cf` | 61903        | coloradoout.00148.rmb.bin     | `S_Debris`                         |
| `0x0000f21a` | 61978        | coloradoout.01572.rmb.bin     | `BLDG_MainTown_Modular_019_LOD00`  |
| `0x00004c86` | 19590        | coloradoout.10471.rmb.bin     | `MT_Area01_Terrain04_LOD01`        |
| `0x00004c83` | 19587        | coloradoout.06248.rmb.bin     | `FEST_MISC_Tollbooth_Marquee_LOD01`|

The tags form a coherent group (a town-festival junction with debris,
modular buildings, terrain patches and a marquee) — the handles are
real, not a coincidental overlap with f32 decoding.

##### Prolog table layout (corrected 2026-04-17)

The earlier "92-byte records at 0x4de" reading was off in two ways:
the records actually live at file offset **0x88** with **64-byte
stride**, and the 26 + 8 handle counts swap — the 26 is the section-
handle array (one slot per section pointer) and the 8 is Table A
(file[0x40]).

For LARGE (`out/probe/v42k7/LARGE___R00G07486.raw`, 14 sections, 8 Table-A
handles, vbuf_size=10840):

```
file offset  content                                            header field
-----------  -------------------------------------------------  ----------------
0x00..0x33   PGEO header (parsed by pgeo.parse_header)
0x34..0x5f   body header — key u32 BE fields:
               0x3c  table_b_count   (= "n_minor", LARGE = 21)
               0x40  table_a_count   (LARGE = 8)
               0x48  total_size = file_len
               0x4c  sec_offset     (= 0 in v42k7)
               0x50  vbuf_size
               0x54  section_count  (LARGE = 14)
0x60..0x87   name field (40-byte slot): C-string, often
             "Models_Ungrouped_NNNN", trailed by an ~12-19B preamble
             with a couple of unexplained floats.
0x88..0x407  section_count × 64-byte records (see "Section record
             layout" below).
0x408..0x467 Section-handle array — N u32 BE rmb-pool handles, where
             N is the spread of distinct pointer values across all
             section records (LARGE: 26, but only 24 fit before
             Table A starts; the array overlaps Table A).
0x468..0x4a7 Table A: 8 × (u32 BE handle, u32 BE 0-pad).
0x4a8..vbuf  table_b_count × ~380B records (semantics TBD; likely
             material/instance metadata).
vbuf_start   vertex buffer (vbuf_size bytes, 40-byte records with
             10-10-10-2 packed positions, anchor e3 d0 at rec[32]).
len-4        4-byte trailer.
```

`vbuf_start = total_size - vbuf_size` (no separate trailer subtraction
in the body — the legacy `decode()` subtracts 4 to skip the trailer).

##### Section record layout (64 B)

Within each record at file `0x88 + i × 64`:

| Off    | Size | Type           | Field                                         |
| -----: | ---: | -------------- | --------------------------------------------- |
| 0x00   |  24  | 6 × f32 BE     | local bbox: `(min_x, min_y, max_x, max_y, max_z, min_z)`. min_y/min_z are always 0 in samples — sections sit on the ground in the chunk-local frame. |
| 0x18   |   4  | u32 BE         | padding (always 0).                           |
| 0x1c   |   8  | (u32 BE, u32 LE) | `(count_a, ptr_a)`. `count==1` ⇒ slot used; `count==0` ⇒ slot empty (and ptr=0). The pointer is **little-endian** even though every other u32 in the file is BE — these fields are dumped as raw runtime memory. |
| 0x24   |   8  | (u32 BE, u32 LE) | `(count_b, ptr_b)` — same rule.            |
| 0x2c   |   8  | (u32 BE, u32 LE) | `(count_c, ptr_c)` — only seen non-zero on a couple of records (LARGE rec[12] uses all three). |
| 0x34   |  12  | bytes          | padding (always 0).                           |

##### Pointer → handle resolution

The pointers are baked runtime VAs to consecutive 4-byte slots in the
section-handle array. Resolution is base-relative:

```
base_ptr = min(non_zero pointers across all records)
idx       = (ptr - base_ptr) // 4
handle    = u32_BE(file[handles_offset + idx*4])
```

`handles_offset` is the file offset of the section-handle array (start
of post-records region — `0x408` for LARGE, slightly different for
single-section samples where the records pack tighter).

A 0-valued handle (LARGE rec[13]) is the sentinel for "no rmb
geometry" and corresponds to indexing into the pad of a Table-A entry.

Verified mappings (LARGE):

| section pointer (LE) | idx | handle | rmb tag                          |
|:---------------------|----:|-------:|:---------------------------------|
| 0x376AD450           |   0 | 0xf1d0 | (in `0xf1cc..0xf21a` block)      |
| 0x376AD454           |   1 | 0xf1d1 | "                                |
| 0x376AD478           |  10 | 0xf1ee | "                                |
| 0x376AD4B0           |  24 | 0x4c86 | first Table-A entry (overlap)    |
| 0x376AD4B4           |  25 | 0      | sentinel (Table-A pad)           |

##### Vertex buffer interpretation

For LARGE, 271 vertices across 26+8=34 instances ≠ n×8 (OBB corners)
and ≠ n×1 (centroids). The 40-byte vertex record's packed 10-10-10-2
position decodes to coherent world-adjacent points (PCA ev3/ev1=0.054,
strongly planar), so these are *some* kind of spatial data — most
likely **per-splat placement points** for scattering referenced rmb
blobs, analogous to Unreal's foliage painting. The 14 × 92-byte
records plausibly describe per-section transforms that scale/rotate
the referenced rmb template onto each splat.

This is unverified; needs testing by rendering the referenced rmb
blobs at the splat positions and checking visual coherence against
the game.

##### Path to real geometry

Status (2026-04-17):

1. ✅ `rmb.py` now parses all blob types (TERR / BLDG / OBJ / S / Tree
   / Festival / MOUN / Reservoir / Mountains / Cables / …) into
   `RmbBlob` with vertex positions and section-level triangle lists.
2. ✅ Post-vbuf index streams decoded — u16 BE strips with 0xFFFF
   restart, converted to triangle lists with even/odd winding.
3. ✅ `pgeo_body/v42k7.py::parse_layout()` returns a `V42k7Layout`
   exposing `sections[i].rmb_handles` (1..3 handles per section) and
   `table_a` (8 globally-shared handles).
4. ⏳ Wire it up: per v42k7 chunk, instantiate each section's first
   rmb handle (geometry stream) at the chunk's world transform —
   higher LOD slots (the second/third handles) and Table-A entries
   are deferred until the geometry side is verified visually.

---

### 3.3 landmark_anim (ver=44, kind=4) — 447 files

Richest format. Small samples are already 19 KB. Strings include
**Art Tool metadata** (`FromFileName`, `TextureType`, `UnitsPerMeter`,
`UpVector`, ...), **particle emitter XML fragments**, and a
**texture-type tag "tga"** at the beginning:

```
0x54  C-string  "tga\0"                             (texture format)
0x60  C-string  "Anim_ANIM_Firework_001_SmallSparkle13\0"
0x88  u32 BE    0x10240000                          (unknown — maybe size-related)
0x90  u32 BE    0x00001688 = 5768                   → offset into file, string table start
0xb0  u32 BE    0x00004764 = 18276                  → offset, texture section
0xc0  u32 BE    0x00003594 = 13716                  → offset, another section
…
0x263 bytes …   `Particles emitterType=AMB_FireworkSparkleSmall maxDist=4000.0`
0xe4 bytes …    "ter.tga"   (texture filename)
```

This is the first variant where the "offsets" in the first 256 body bytes
genuinely resolve to interior offsets. `sec_offset = 0x4764` at 0x4c
**matches** one of the in-body offsets exactly — strong evidence that the
prolog's `sec_offset` field is a principal section pointer.

Plan when decoding landmark_anim:
1. Read all u32 BE at 4-byte alignment between 0x88 and 0x130 that
   resolve into the file — that's the section table.
2. Each section is likely `{name_hash, size, payload_offset}`.
3. Texture references name .tga files by short name; probably resolved
   against `.bix` hashes elsewhere in bin.zip.

---

### 3.4 grass (ver=42, kind=2) — 11,292 files

**Engine class:** `proceduralGeometry::CProceduralVegetation` (xex
RTTI, `docs/xex-walk/04-rtti-classes.txt:317`). The file naming
prefix `Grass_*` is misleading — the engine class covers all
foliage scatter (grass tufts AND tree/shrub instances). Real
foliage placements live here, not in the misnamed `light_glows`
variant.

**Status (2026-05-03):** Body decoded. 11,292 / 11,292 files
parse cleanly into per-instance positions; 1,052,983 placement
positions recovered (287,917 unique after chunk-pool dedup —
the 765k duplicates are byte-identical chunk-pool replication of
the same scatter cell across multiple ribbons). Position decoding
verified at 100% in-bbox across all samples. Per-instance
orientation field undecoded (low-impact for placement
visualisation). Extractor: `src/fh1_mapdecomp/grass_inst.py`.

**Visual validation (2026-05-03 yellow-cylinder placeholder):**
roadside grass / Festival-edge / Plains-area scatter renders in the
right place. Trees and hillside foliage do **not** appear — the
`grass` PGEO variant covers the per-blade roadside ground scatter
only, not standalone tree/shrub placements. Trees almost certainly
live in the `v42k7` *proc-inline subvariant* (descriptor prefix
`models_proc_clrd_trees_*`, see §3.2.2). Position decoding for that
subvariant already exists in `pgeo_body/v42k7.parse_proc_inline_positions`
but isn't wired as a placement extractor yet — that's the obvious
follow-up after grass.

**Per-template handle table:** the per-instance template-index u16
array is decoded (one u16 per record at file offset `rec_end`), but
the index→rmb-pool-handle lookup is **not** in the grass PGEO body.
Scanning all 11k trailing blocks for plain u32 BE values resolving
to a `Grass_*`-tagged rmb showed the top hit
(`Foothills_Grass_Area_2a_LOD01`) only matches in 0.4 % of files;
the table lives externally (likely a runtime/zone registry built
from `.bundle` material data, or a per-zone master we haven't
found). Until then the extractor ships a synthetic bright-yellow
cylinder placeholder so positions are visually verifiable.

#### 3.4.1 Body layout

```
file off  size  field
--------  ----  -----
0x34..0x5b      shared prolog (n_minor @ 0x40, total_size @ 0x48 = len(file))
0x60..        cstring descriptor — "Grass_Ungrouped_NNNN_0\0"
              (NNNN is a chunk index, not an asset key)
[null+4 → align 4]
              n_minor × 8B records — (u32 BE handle, u32 BE 0)
              The handles point to the **underlying terrain patch** the
              grass scatter sits on (`TERR_*_LOD01`, `Plains_*_LOD01`,
              `Foothills_Grass_Area_*_LOD01`). They are NOT the grass-
              blade mesh template — that template is unidentified;
              `grass_inst.py` uses a single representative `Grass_LOD01`
              rmb as a placeholder for every instance.
              Engine probably uses these handles as paint-target hints
              (which terrain to drape this scatter over).
+0x00..0x0b   3×f32 BE  bbox_min restated (12B + 4B pad)
+0x0c..0x17   3×f32 BE  bbox_max restated (12B + 4B pad)
+0x18..0x1b   u32 BE    count_a — primary instance count (= records that
                         decode cleanly with the 0xffff sentinel; matches
                         visible scatter density per chunk).
+0x1c..0x1f   u32 BE    count_b — secondary count (always > count_a;
                         appears to be a per-instance metadata table size
                         in some unit; not yet decoded structurally).
[zero pad to 12B-aligned record start]
              count_a × 12B per-instance records (§3.4.2)
              [trailing per-handle / per-LOD metadata to EOF — LOD
               distance thresholds (e.g. 26, 75, 150, 20 m), GPU
               pointers, material parameters; not needed for placement]
end-4..end    (no separate trailer)
```

**0x14 invariant** (§1.2): `u32 BE @ 0x48 == len(file)` holds on
all 11,292 samples.

#### 3.4.2 Per-instance record (12 bytes)

```
+0x00  u16 BE   X fraction of bbox X-extent  (0..65535)
+0x02  u16 BE   Y fraction of bbox Y-extent  (0..65535)
+0x04  u16 BE   Z fraction of bbox Z-extent  (0..65535)
+0x06  u16 BE   packed extra (orientation? scale-modifier? semantics TBD)
+0x08  u16 BE   small flag (values seen: 0, 1, 2, 3 — could be LOD
                level, blade-template index, or quadrant rotation;
                not currently used)
+0x0a  u16 BE   0xffff sentinel — strongest single anchor; never
                varies. The position-table scanner locks onto it.
```

Position decoding (per axis):
```
world_x = bbox_min_x + (sx / 65535.0) * (bbox_max_x - bbox_min_x)
```

#### 3.4.3 Asset reference

There is none we've identified for the per-instance blade mesh.
The `n_minor` handles after the descriptor point at the underlying
terrain patches (paint-target hints), not at a blade-template rmb.
Searching the rmb pool for `Grass_*` tags surfaces candidates
(`Grass_LOD01`, `Foothills_Grass_Area_*_LOD0X`,
`MT_Area06_Road_Grass_*`) but no field in the body cleanly
selects which one is the blade mesh for a given chunk.

The current extractor uses a single representative
`Grass_LOD01` (rmb handle 4223) as a placeholder for every
instance. Visual review can swap in better placeholders later
without changing the position pipeline.

#### 3.4.4 Layout legacy notes

The earlier scout summary documented:

```
0x60  C-string  "Grass_Ungrouped_15149_0\0"
0x7a  u32 BE    0x00004c7a                     (unknown; size pattern)
0x80  3×f32 BE  ≈ bbox_min  (= 1243.35, 75.32, 296.5 — matches header bbox_min w/ small delta)
0x8c  3×f32 BE  ≈ bbox_max
0x98  u32 BE    0                               padding
0x9c  u32 BE    3                               count?
0xa0  u32 BE    3                               (same) — 3 LoDs? 3 clusters?
…                                               zero padding
0xc4  …         something starting `ff ff 1f 13 00 00 ff ff …`
```

The trailing data block contains recurring byte patterns typical of
**packed per-blade data**: `ff ff 1f 13 00 00`, `f0 4b 9a 00`, `10 4c 9a 00`.
These look like 4-byte records, possibly `(packed_pos_u32,
flags_u8×4)` for each grass blade.

Grass is least urgent: per roadmap §6.2 we'll render it via Blender
Geometry Nodes from a point cloud, not per-blade meshes.

---

### 3.5 light_glows (ver=43, kind=6) — 3,316 files

**Engine class:** `proceduralGeometry::CProceduralLightGlows` (xex RTTI
table, `docs/xex-walk/04-rtti-classes.txt:314`). Earlier sessions
labeled this variant `vegetation` based on the `(1000, 1000)` scale
pair and `ver=43` header looking foliage-shaped; that was a misnomer —
the file content is **light entity data, not foliage**. All 3,316
Colorado entries have `Glow_*` descriptors. Real foliage is the
`grass` variant (engine class `CProceduralVegetation`).

**Status (2026-05-03):** Body shape decoded against samples; per-record
field semantics inferred but not yet wired into an extractor.

#### 3.5.1 Two descriptor formats

Same A/B split as the crowd variant (§3.6.1 / §3.6.2):

| Format | Descriptor offset | Count | Tag prefix                            |
|:-------|:-----------------:|------:|:--------------------------------------|
| A      | 0x60              | 2,435 | `Glow_WorldStaticLightGlows_NNNN`     |
| B      | 0x98              |   881 | `Glow_Gameplay_<EVENT>_L_NN`          |

Format B reserves `0x60..0x97` for runtime section state (zero-padded
on disk, with a short tag like `FR44_L` mid-block) and pushes the full
descriptor to 0x98.

#### 3.5.2 Per-record layout (60 bytes, both formats)

Records start at file offset `0xb0` (Format A — single-instance file
also fits this) or `0x100` (Format B). Stride is 60 bytes; record count
is at body+0x70 (file 0xa4) preceded by a `0xffffffff` sentinel at
0xa0:

```
+0x00  3×f32 BE   world position  (x, y, z)
+0x0c  3×f32 BE   direction vector
                   Format A samples (omnidirectional streetlights):
                     near-zero on all 3 axes
                   Format B samples (race-checkpoint cones):
                     normalized vector pointing along the cone axis
+0x18  2×f32 BE   (radius, cone_angle)
                   Format A: (6.108, 6.2832 = 2π) — 6 m radius, full sphere
                   Format B: (0.524, 1.5708 = π/2) — 0.5 m radius, π/2 cone
+0x20  3×f32 BE   color RGB
                   Format A streetlights: (0.1, 0.1, 1.2) — blue-tint
                   Format B race lights: variable per-record
+0x2c  2×f32 BE   intensity / falloff pair (0.5, 0.5 default)
+0x34  4 bytes    RGBA8 alpha-mask byte + 3-byte per-record terminator
                   (`ca e8 ad XX` Format A, `0d 0f 14 XX` Format B)
```

The "constant" fields in Format A (radius, cone, color) reflect that
all `Glow_WorldStaticLightGlows_*` entries within one file share a
material — the file groups N copies of the same light prefab placed at
different positions. Format B varies per-record because each gameplay
checkpoint has its own beam direction.

#### 3.5.3 Asset reference

There is none. Light glows are not meshes — the engine renders them as
additive billboards / volumetric glow primitives using the per-record
material parameters in-place. No rmb pool handle, no descriptor-to-mesh
map.

For Blender export the natural representation is one Blender point /
empty per record (positions only). If lighting is ever wired in, the
direction + color + intensity fields are the source of truth. Until
then, this variant is low-priority compared to actual foliage.

---

### 3.6 crowd (ver=42, kind=3) — 8,525 files (Colorado bin.zip)

**Status (2026-05-03):** Decoded. 8,514 / 8,525 files parse with the
unified Format A/B layout below; the 11 remaining are Format C
(large-bbox, f32-explicit) and decode separately. 712,174 placement
positions recovered across 1,778 unique descriptors. Inventory dumps
live at `probes/out/crowd_families.tsv` (58 families) and
`probes/out/crowd_descriptors.tsv` (1,778 rows).

A "crowd" PGEO is a **procedural-instance container** very similar in
shape to v42k7's `proc_inline` subvariant (§3.2.2): a single ASCII
descriptor identifying the asset, a restated bbox, an instance count,
two LE pointers to a runtime-baked record array, and a packed per-
instance record stream. There is no inline vertex buffer; the asset
geometry is referenced by descriptor name and looked up against the
`rmb.bin` pool / a sibling crowd-mesh pool at runtime.

Header pattern: scale `(60, -1)`; kind=3; bbox is whatever the chunk's
spatial extent is — small (~2×6×2 m for a single-instance file like
`__R00G00537`), large (~100×16×100 m for a packed festival-area-wide
file). The `0x14 invariant` (§1.2) holds on every sample.

#### 3.6.1 Format A — descriptor at body 0x60 (924 files)

Used when the descriptor zone fits within the standard prolog tail.
Recognised by an ASCII `crowd_*` byte at file offset `0x60`. Layout:

```
file off  size  field
--------  ----  -----
0x34..0x5b      shared prolog (n_major @ 0x3c, total_size @ 0x48 = len(file))
0x60..        cstring descriptor — e.g. "crowd_proc_clrd_festivalcrowd_stalls_12\0"
              (often a doubled name with no internal null between the parts —
              "festivalcrowd_stalls_12" is one concatenated tag, not two)
              Trailed in some samples by a numeric-as-text suffix like "27\0".
0x90..0x9b    3 × f32 BE  bbox_min restated
0x9c..0x9f    pad (zero)
0xa0..0xab    3 × f32 BE  bbox_max restated
0xac..0xaf    pad
0xb0..0xb3    u32 BE      count N (number of placement records)
0xb4..0xb7    u32 LE      ptr_a  (runtime-baked VA, low byte tracks position)
0xb8..0xbb    pad
0xbc..0xbf    u32 LE      ptr_b  (ptr_b - ptr_a == N × 12 — STRONG anchor)
0xc0..0xc3    pad
0xc4..0xc7    u32 BE      0x0000000f marker (= 15; meaning unconfirmed,
                          stable across all standard files)
0xc8..0xcf    pad (8 bytes zero)
0xd0..        records: N × 12 bytes (see §3.6.3)
end-4..end    4-byte trailer (zero in samples)
```

For a single-instance file the records live inline after the marker
and the file ends a few bytes later. There is no second sub-record
in normal samples; `__R00G00537` (a 220 B single-stall file) is the
canonical example.

#### 3.6.2 Format B — descriptor at body 0x98 (7,574 files)

Identical to Format A except the descriptor sits 0x38 bytes later
because the prolog tail at `0x60..0x97` is reserved for runtime
section state (mostly zero on disk; carries varying small u32 fields
like `01 06 00 00` or `01 f3 00 00` at body 0x84). Recognised by an
ASCII `crowd_*` byte at file offset `0x98`. The post-descriptor
layout (bbox restated, count, pointers, marker, records) is identical
to Format A; the parser doesn't care which of the two it is once it
has located the descriptor's terminating null.

The split between Format A and Format B is correlated with descriptor
content but isn't 1-to-1 — `crowd_proc_clrd_*` shows up in both. The
likeliest explanation is build-toolchain era differences (an older
authoring tool packing the name early vs a newer one keeping the
prolog layout reserved). For decoding purposes both are handled by:

1. Scan `body[0x5c..0xb0]` for the first `crowd|CROWD|FESTI` anchor.
2. Read a null-terminated cstring from there.
3. From `(null_offset + 4) & ~3`, scan u32-aligned offsets for a 3×f32
   triplet that matches `header.bbox_min` within ±1 m on X/Z and within
   `bbox_height + 5` m on Y (the restated copy is bit-imprecise — last
   bit of the f32 mantissa drifts by 1).
4. From the matched bbox offset, fixed +32 lands on the count, +32+32
   lands on the first record (matches `ptr_b - ptr_a == count × 12`).

#### 3.6.3 Per-instance record (12 bytes)

```
+0x00  u16 BE   X fraction of bbox X-extent  (0..65535)
+0x02  u16 BE   Y fraction of bbox Y-extent  (0..65535)
+0x04  u16 BE   Z fraction of bbox Z-extent  (0..65535)
+0x06  u16 BE   packed orientation — semantics TBD
                Low byte = constant 0x02 on observed barrier samples;
                high byte clusters by triples of records. Plausibly a
                quantized yaw plus rope-segment group ID. NOT a clean
                DEC3N normal — magnitudes 0.27..1.42 if decoded that way.
+0x08  u32      flag — always 0x00000000 on inanimate-prop (barrier)
                samples. Some animated `crowd_*` records flip byte +0x08
                to 0x01 (possibly is-animated / material-pool index).
                Verification deferred to the animated-crowd workstream.

**History (don't repeat).** The initial reading copied v42k7's
`parse_proc_inline_positions` — `u32 BE` at +0x00 decoded as packed
10:10:10:2. That gave coherent positions inside the chunk bbox but
spread Y across the FULL bbox Y-extent (~6 m on a 6.4 m bbox), which
is physically implausible for stanchion posts on flat festival ground.
The straight-X-line artefact in the resulting Blender scene came from
Y-noise drowning out the X+Z signal. Fixed 2026-05-03 by treating
each axis as its own u16: per-chunk Y-spread collapses to 0.32..2.42 m
(real terrain undulation), X+Z resolve into recognizable barrier
curves.
```

#### 3.6.4 Format C — large-bbox / f32-explicit (11 files)

The 11 files that don't fit Format A/B are wide-area scatters
(`crowd_proc_clrd_festivalcrowd_garagerear_17` and
`crowd_proc_clrd_festivalcrowd_secampsite_91`, with 5 / 6 chunk-pool
copies each). Body 0x90..0xab is filled with `0x7fffffff` sentinels
(bbox_min = bbox_max = +FLT_MAX), signalling "no bbox restated;
positions stored explicitly". After the sentinel block the body
carries a small count-prefixed pre-header (a `(ptr, count)` pair at
0xb4..0xbb) followed by 16-byte records of 3 × f32 BE world-space
positions plus 4 bytes of trailing data. Used because the parent
chunk's bbox spans hundreds of metres (374×11×335 m for `garagerear`)
and the 10-bit packed format would degrade to ~0.4 m precision per
axis.

Decoding Format C is shelved for a follow-up pass — the gap is real
(the festival garage-rear and SE campsite areas) but small (0.13% of
files) and doesn't include the festival-circle barriers.

#### 3.6.5 Descriptor families

The 58 first-5-token descriptor families fall into rough buckets
(full table in `probes/out/crowd_families.tsv`). Selected highlights:

| family                                       | files | unique descs | inst count | inanimate? |
|----------------------------------------------|------:|-------------:|-----------:|-----------:|
| `crowd_proc_clrd_festival_crowd`             |  2173 |          423 |    160 151 | ❌ animate spectator scatter |
| `crowd_proc_clrd_festival_fest`              |   730 |          130 |     83 898 | ❌ animate spectator |
| `crowd_proc_track_main_p2p`                  |   715 |           67 |     34 044 | ❌ race-track crowd (race-only) |
| `crowd_proc_clrd_multiplayer_warehouse(s)`   |   307 |           41 |     43 226 | ❌ multiplayer-only spectator |
| `crowd_proc_clrd_festivalcrowd_stalls`       |    78 |           10 |     24 122 | ✅ festival booth structures |
| `crowd_proc_clrd_festivalcrowd_stages`       |   298 |           46 |     23 226 | ✅ festival stage structures |
| `crowd_proc_clrd_festivalcrowd_grandstands`  |    10 |            2 |      4 050 | ✅ grandstand structures |
| `crowd_proc_clrd_fest_area3` (`barriers_*`)  |    36 |            6 |        489 | ✅ **festival metal stanchion barriers** |

The 6 `barriers_*` descriptors are the user-confirmed gap from the
2026-05-03 handoff:

```
crowd_proc_clrd_fest_area3_barriers_northeast_1   24 inst (avg)  bbox 21×6×25  centre (-860,-7.9,-139)
crowd_proc_clrd_fest_area3_barriers_northeast_2    3 inst         bbox 3.5×6×3 centre (-777,-6.5, 45)
crowd_proc_clrd_fest_area3_barriers_northeast_3    6 inst         bbox 3.3×6×6 centre (-702,-7.4,-88)
crowd_proc_clrd_fest_area3_barriers_northwest_1    9 inst         bbox 13×6×11 centre (-1049,-9.4,-137)
crowd_proc_clrd_fest_area3_barriers_southeast_1   24 inst         bbox 94×8×82 centre (-915,-9.6,-346)
crowd_proc_clrd_fest_area3_barriers_southwest_1   21 inst         bbox 38×7×56 centre (-1082,-12.1,-314)
```

Cardinal-direction barrier loops around the festival circle at
~(-1000, -10, -240), with one each at NE/NW/SE/SW corners plus two
small NE stubs.

#### 3.6.6 Asset reference (TODO)

The descriptor name is the only obvious asset-ref in the file. There
is no `_LOD\d+` suffix, no rmb-pool handle, no content-hash. The
runtime presumably resolves `crowd_proc_clrd_X_Y` to a template mesh
via a sibling registry (analogous to v42k7 `models_proc_clrd_*`,
§3.2.2 — also unresolved). For inanimate descriptors the template is
likely a single rmb-pool tag matching the descriptor's category
(`OBJ_FEST_BarrierMetal_*`, `OBJ_FEST_Stall_*`, etc.) — testing this
is the immediate next step (task #6 in the handoff).

---

### 3.7 v44k5 (ver=44, kind=5) — 1,578 files (unclassified)

Sample: `anim_proc_clrd_fx_geyser_0`. Body includes:

```
0x60  C-string  "anim_proc_clrd_fx_geyser_0\0"
0x7c  u32 BE    0x00000013 = 19
0x88  u32 BE    0x00005394                        offset into file? = 21396 > file len? (file is 288). No — out of range. Could be an external hash.
0x90  2×f32 BE  (0x507ddb32, 0xd07ddb32) — huge floats, look like hashes
0xa4  u32 BE    0x343bbd4d — hash
0xb0  u32 BE    0xb43bbd4d — hash (bit-flipped sibling of the one above)
0xc8  f32 BE    1.0
0xd0  3×f32 BE  world position (1715.32, 196.41, -5340.6)   — centroid of bbox
```

v44k5 looks like **animated procedural effects** (geyser, etc.) — similar
shape to v44k4 (landmark_anim) but simpler. Low priority; not visible
geometry per se.

---

## 4. Next steps

Ordered by bang-for-buck, after Phase A (2026-04-17) ruled out
in-file / sibling-file index buffers for v42k7:

1. **Ship a terrain decoder.** Topology (strips + lightmap grid) is
   self-contained in the `.pgeo` per §3.1. Y-elevation is still open;
   v0 can ship flat tiles, better than the current bbox cubes.
2. **RE the `rmb.bin` pool.** 102,012 entries / 14,291 unique indices
   / 4.4 GiB — see `docs/bin-zip-layout.md §3`. This is the most
   likely home of the real v42k7 triangles *and* a plausible Y
   source for terrain *and* the template storage for procedural
   v42k7 instances (§3.2.2). Biggest lever in the project.
3. **Find the terrain height source.** Candidates in rough order:
   (a) an `rmb.bin` blob referenced per tile, (b) the `.sh` tiles
   (filenames already encode world coords), (c) the material hash
   at terrain body 0x98. Item (2) above subsumes (a).
4. **Decode the landmark_anim section table.** Fewest files but
   richest in-file structure — section offsets already visible in
   the prolog. Low risk, standalone RE win.
5. **Resolve procedural-instance templates** (§3.2.2). Once
   `models_proc_clrd_<name>` → template-mesh lookup is known, the
   2,035 empty-vbuf v42k7 chunks can emit real placements instead
   of bbox cubes. Likely depends on (2) and/or on the `.bundle`
   files.

---

## 5. Open questions

- What are `body[0x7c] = 102` and `body[0x80] = 80` for terrain? They are
  constant across every terrain sample. Lightmap tile size in texels?
  Compression stride? The `0x80` smells like the lightmap row stride
  (128 bytes with some overhead) but needs proof.
- What does `body[0x3c] (n_major)` actually count?
- Is `body[0x4c] (sec_offset)` always populated when there's a section
  table? And if zero, does that mean there are no sections, or that the
  data is inline right after the prolog?
- What encodes the `rmb.bin` lookup key used by a v42k7 PGEO? The
  GPU-pointer "hashes" at body 0xa4+ can't be it (§3.2.4 /
  `docs/bin-zip-layout.md §4`). Numeric `NNNNN` does not appear
  directly in the samples we've dumped, so the key is either
  embedded further in the body or computed at runtime from some
  other field.
- What's the external buffer indexed by the `0xf1c0..0xf200`
  cluster (§3.2.3)? Needs a search of `.rmb.bin` / `.bundle` /
  `.bix` contents for a ~62 K-entry array matching shape.

Answered (2026-04-17):
- **Which variant owns `.bix` textures?** `.bix` files are
  self-contained containers (`BIX1` magic) keyed by their own
  filename hash; PGEOs don't reference them by stem. The `_B` suffix
  denotes GPU-packed 8 KiB / 128 KiB texture tiers.
- **Do v42k7 files have sibling index-buffer entries?** No. No
  `.pgeo` stem matches any entry of any other extension. Indices
  are not in bin.zip under a paired name.

Answers land here as we go.
