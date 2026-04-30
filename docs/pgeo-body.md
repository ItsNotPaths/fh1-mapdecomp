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
| 0x4c  |   4  | u32 BE  | `sec_offset` — offset into file, or 0         | Zero on terrain / v44k5 / grass / vegetation / crowd. Non-zero on v42k7 (0x2a58 = 10840) and landmark_anim (0x4764 = 18276). Strong "section table / texture block start" hint. |
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
| vegetation     |   0x60    | `Glow_WorldStaticLightGlows_1309`             |
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
| `Glow_WorldStaticLightGlows_N`| static light-glow billboard (vegetation). Note: "vegetation" variant is not exclusively trees. |
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

### 3.5 vegetation (ver=43, kind=6) — 3,316 files

Similar prolog to grass, but **with f32 triplets not just packed bytes**:

```
0x60  C-string  "Glow_WorldStaticLightGlows_1309\0"   (also "ANIM_TREE_*", "TREE_*" in other samples)
0x80  4 bytes   0x98cfd109                           (hash? pointer?)
0x84  4 bytes   0xaccfd109                           (hash)
0x88  u32 BE    1                                    instance count
…
0xac  3×f32 BE  instance world position  = (7592.81, 442.34, -338.57)   ← matches header bbox_min + half_extent
0xb8  2×f32 BE  (0x80000000, 1.0)                   orientation quaternion component?
0xc4  u32 BE    0
0xc8  3×f32 BE  orientation vectors…
```

The sample carries ONE instance; other samples have more. The scale pair
`(1000, 1000)` on the header is probably the packed-coord range used by
a subset of vegetation variants (trees with packed positions); the sample
we have uses unpacked floats.

---

### 3.6 crowd (ver=42, kind=3) — 8,544 files

Header scale pair `(60, -1)`; bbox is a 2×6×2 m box.

```
0x5c  4 bytes   `-98d` — secondary count/flags
0x60  C-string  "crowd_proc_clrd_festival\0"
0x78  C-string  "crowd_stages_136\0"                 (double-string!)
0x88  4 bytes   "27\0\0"                              (numeric suffix as text)
0x90  3×f32 BE  bbox_min re-stated
0xa0  3×f32 BE  bbox_max re-stated
0xb0  u32 BE    1                                    count
0xb4  u32 BE    0x4078f62f — hash
0xbc  u32 BE    0x4c78f62f — hash (sibling)
0xd0  bytes     0x7fff2492, 0x7fffd101 — DEC3N-packed normals (sentinel-like)
```

Crowd carries material hashes and small per-instance data but no
in-body vertex payload — animated crowds likely share a small mesh pool
referenced by the hashes.

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
