# `.fiz` format — partial reverse-engineering (DEAD-ENDED 2026-04-29)

Status: **Investigation closed.** `.fiz` is not a placement source
for the missing decorative props this project is hunting for. The
file format is partially decoded but the *semantic* of Block B
(what the records actually represent) was never resolved, and
re-pursuing it isn't worth the effort.

> **2026-04-29 closing summary**:
>
> - Block A `(X_f32, Y_f32, Z_f32, packed_u32)` BE decode is correct.
>   All 25 records in 800.fiz are world-positioned within ~8 units
>   (≤1 cell) of the tile bbox.
> - Block A start = 0xb0 across every tile checked (consistent layout).
> - Block B was tentatively read as a triangle list (3 pointers + a
>   quaternion). **Falsified visually**: exporting the file as OBJ
>   and rendering it produces chaotic spike-fans following roads and
>   open ground, not coherent foliage meshes. The 3 pointers do NOT
>   form a triangle.
> - Block B trailer u32 is a **24-bit render-flag bitmask** (high byte
>   always 0x00; values cluster around `0xFFFFFF` = all-on with
>   single-bit-cleared variants like `0xFFFFFD`, `0xFDFFFF`). Not a
>   constant, not a model id.
> - Theory "no-collision decoratives scatter via .fiz" — REFUTED.
>   Differential analysis ruled out global pool handle / NNNNN
>   ordinal / FilenameMap index encodings. The visualization
>   contradicts the baked-mesh theory too. We don't know what `.fiz`
>   IS, but it's not the placement source.
>
> **What remains for a future investigator** (low priority):
> - True semantic of Block B's 3-pointer + quaternion structure.
> - The 13.2% of tiles with dY=100 (X,Z)-twin vertex pairs (billboard
>   marker? LOD anchor? cull plane?).
> - The packed_u32 low 24 bits.
>
> **Active leads elsewhere**: `project_disk_source_FOUND_2026-04-29.md`
> — bin.zip's central directory is the chunk master.

## Why we care

`.fiz` is the strongest remaining candidate for a per-tile static-decoration
scatter system. Theory (proposed by user 2026-04-29): the engine treats every
**no-collision** decorative model as "foliage" and scatters them via this
system. The named decoratives missing from CollObjs.xml/v42k7/rmb_world
(golf carts, haybales, wood/metal windmills, wind turbines, autoshow tents,
grandstand props) all have authored detail rmbs in the pool but no recovered
placement source — `.fiz` is the natural place for those positions.

Validation status: **theory not confirmed nor falsified yet.** Differential
analysis (probe `probe_fiz_differential.py`) tried matching unique-to-golf-tile
u32/u16 values against golf-cart/haybale/windmill/wind-turbine pool handles,
bin.zip NNNNN ordinals, and FilenameMap_00 indices. Zero matches in u32, one
weak u16 hit (15257 = wind turbine pool handle, almost certainly coincidence).
The asset-reference encoding in `.fiz` is NOT one of those three obvious
global ID schemes. It's likely indirect — see "Unresolved" below.

## Top-level inventory

- 16,934 `.fiz` entries in `bin.zip`, all under `media/tracks/colorado/bin.zip`
- 3,201 unique filenames (heavy bin.zip dedup, average 5–6 copies per name)
- Filenames: numeric (`1.fiz`, `1160.fiz`, `3201.fiz`); 1:1 per zone; not
  spatially-keyed in the filename itself (zone-id / arbitrary numbering)
- File sizes: 2,080 B (smallest) to ~1 MB (largest); `1160.fiz` (golf-cart
  tile) = 28,656 B
- Magic: `'fiz '` (4 bytes) at +0x00
- Version: 1 (u32 BE @+0x04)

## Phase 1: header layout (verified across 45 sampled tiles)

```
+0x00  bytes[4]   magic 'fiz '
+0x04  u32 BE     version (=1)
+0x08  u32 BE     section-end byte offset (corr +1.000 with body size)
+0x0c  u32 BE     ? (corr +0.932)
+0x10  u32 BE     ? (corr +0.574)
+0x14  u32 BE     0
+0x18  u32 BE     0
+0x1c  u32 BE     0
+0x20  u32 BE     Block A record count   (corr +0.998, range 2..4661)
+0x24  u32 BE     Block B record count   (corr +0.998, range 2..4645)
+0x28  u32 BE     CONSTANT 144            (always — likely a class-table size)
+0x2c  u32 BE     Block C byte length    (corr +0.981)
+0x30  u32 BE     CONSTANT 12
+0x34  u32 BE     CONSTANT 12
+0x38  4×f32 BE   bbox = (xmin, zmin, xmax, zmax) — verified by truth coord
+0x48  f32 BE     CONSTANT 10.0           (cell size?)
+0x4c  f32 BE     CONSTANT 10.0           (mirror of 0x48)
+0x50  f32 BE     CONSTANT 0.1            (quant step?)
+0x54  f32 BE     CONSTANT 0.1            (mirror of 0x50)
+0x58  u32 BE     ? (corr +0.999, scales similarly to +0x08)
+0x5c  u32 BE     ? (corr +0.484, occasional non-zero garbage value)
```

Probe: `probes/probe_fiz_header_audit.py` — sample 45 tiles, compute
correlation between each field and body size; classify constants vs
scaling.

## Phase 2: block layout (verified on 800.fiz)

`800.fiz` chosen because it is the smallest non-trivial body (2,080 B).
Counts: `A=25`, `B=20`, `C_byte_len=544`, `main_end (@+0x08) =
0x800 = 2048`. Body bytes = 2,080 − 96 (header) = 1,984.

```
+0x000..+0x060   header (96 B)
+0x060..+0x0b0   pre-block padding (80 B, mostly zeros; one flag word
                  0x80000000 at +0x9c — purpose unknown)
+0x0b0..+0x240   Block A: 25 × 16-byte records (= +0x190 bytes)
+0x240..+0x4c0   Block B: 20 × 32-byte records (= +0x280 bytes)
+0x4c0..+0x6e0   Block C: 544 bytes sparse pointer table
+0x6e0..+0x800   Section D: u16 index buffers + inner pointers
+0x800..+0x820   trailer (32 B zeros, 0x800 = main_end)
```

Tested across multiple tile sizes; the (A_count × 16 + B_count × 32 +
C_byte_len) = body-bytes-minus-prefix-padding relationship holds, with
the pre-block padding region varying.

### Block A: 16-byte vertex pool — DECODED (2026-04-29)

Layout per slot:
```
+0x00  f32 BE   X (world)
+0x04  f32 BE   Y (world height)
+0x08  f32 BE   Z (world)
+0x0c  u32 BE   packed: high byte = class/material, low 24 bits = TBD
                (likely packed normal / vertex-color / UV / instance-data)
```

First record at 0x0b0 in 800.fiz:
```
bytes:    c5 04 a6 f6 3f 9a c5 a8 c5 3e 20 0a f8 9f e0 31
decoded:  X=-2122.435  Y=1.209  Z=-3042.002  packed=0xf89fe031
```

That X falls inside the tile X bbox `[-2216.86, -2100.86]`. **The earlier
"-528.61" reading was a transcription/conversion error.**

Tile-bbox fit (all 25 records of 800.fiz):
- 17/25 X strictly inside `[xmin, xmax]`
- 14/25 Z strictly inside `[zmin, zmax]`
- 25/25 within Colorado world bounds
- Maximum overshoot: 7.76 units in either axis — less than the 10-unit
  cell-size constant from the header. The tile bbox represents the
  "core" extent; vertices spill into the neighboring cell band.

**Probe**: `probes/probe_fiz_block_a.py` — `(X,Y,Z,packed)` decode,
tile-fit summary, packed_u32 byte-0 distribution.

#### packed_u32 high-byte clusters by shape class

In 800.fiz the high byte of `packed_u32` clusters by triangle type:
- low foliage (Block B type `0x0e01`): high byte ∈ `{0xf5, 0xf6, 0xf7, 0xf8}`
- mid bushes (Block B type `0x0e02`): high byte ∈ `{0xed..0xfe}`
- tall impostors (Block B type `0x0803`): high byte ∈ `{0x01, 0x53}`

So `packed[31:24]` is a per-vertex material/class tag and is
*consistent* with the triangle-type byte that uses the vertex.

The low 24 bits don't yet have a clean structure. Differential analysis
across tiles (`probe_fiz_differential.py`) ruled out global pool handle,
NNNNN ordinal, and FilenameMap index. Possibilities for the low 24 bits:
quantized normal, packed UV, per-vertex tint, or per-instance index.

#### Sentinel slots at 0x90 and 0xa0

The "pre-block padding region" 0x60..0xb0 contains 5 zero-slots, but
two of them (0x90 and 0xa0) ARE referenced by Block B record 0 as
vertex pointers. These are sentinel "null vertex" markers:
- 0x90: all zeros except `packed = 0x80000000` (flag bit only)
- 0xa0: all zeros

When a Block B triangle has only 1 or 2 real vertices (e.g., a single
billboard sprite or a line), the unused pointer slots are filled with
0x90/0xa0 to keep the 3-pointer record shape uniform. So the **effective
Block A start is 0x90, with the first 2 slots reserved as sentinels and
23 real vertices following at 0xb0..0x220** (matching the original 25
count).

### Block B: 32-byte triangle records — DECODED

Each record at `+0x240 + N × 32` has:
```
+0x00  u16 BE   triangle type / material class (observed: 0x0e01, 0x0e02, 0x0803)
+0x02  u16 BE   flag (always 0x8000)
+0x04  u32 BE   vertex pointer #1   → file offset into Block A vertex pool
+0x08  u32 BE   vertex pointer #2   → file offset into Block A vertex pool
+0x0c  u32 BE   vertex pointer #3   → file offset into Block A vertex pool
+0x10  3×f32 BE quaternion (qx, qy, qz)  — qw implicit, orients the triangle
+0x1c  u32 BE   trailer = 0x00FFFFFF (constant)
```

**Each Block B record is a TRIANGLE that references three vertices in
the Block A pool.** The whole triangle is rotated by the quaternion.
Verified on 800.fiz:
- Type 0x0e01 (5 triangles) = ground foliage patches; edges 10–20 units
- Type 0x0e02 (11 triangles) = mid-height bushes; mostly fan-style
  topology with 2 short edges + 1 long shared edge (hub vertex)
- Type 0x0803 (4 triangles) = tall impostor billboards; ~5–10 unit
  short edges + ~95–100 unit "vertical" edges (top-of-billboard pairs)

#### Impostor billboard quads

In 800.fiz, six vertices form three exact Y-pairs (same X,Z, dY = 100.00):
```
@0x1e0 (Y=104.37) ↔ @0x210 (Y=4.37)
@0x1f0 (Y=104.26) ↔ @0x200 (Y=4.26)
@0x220 (Y=104.48) ↔ @0x230 (Y=4.48)
```
These 6 vertices are the corners of 3 vertical billboard quads
(rendered as 4 triangles by the type=0x0803 records). The 100-unit
height = the impostor's billboard height in world space.

**Probe**: `probes/probe_fiz_block_a_triangles.py` — verifies edge
lengths, type↔class correlation, Y-pair detection.

#### Why pointers reference 0x90 / 0xa0

Earlier the doc treated these as a structural anomaly. Resolved:
0x90 and 0xa0 are sentinel "null vertex" slots (see Block A section).
They appear in triangle records that effectively have <3 real vertices
(point sprites or lines), allowing Block B to keep a uniform 3-pointer
shape. So the effective Block A range is 0x90..0x220 (25 slots), with
the first 2 reserved as null sentinels.

### Block C: 544-byte sparse pointer table — PARTIAL

In 800.fiz, Block C runs 0x4c0..0x6e0 and is mostly zeros, with three
u32-aligned values:
```
+0x4ee  0x000006e0
+0x4f2  0x000006e4
+0x4f6  0x000006f0
+0x6cc  0x000006f8
```
All four are file offsets pointing INTO Section D (which starts at
0x6e0). So Block C is a sparse "lookup vector → Section D offsets"
table. Stride and indexing scheme TBD.

### Section D: u16 index buffers — PARTIAL

In 800.fiz from 0x6e0 onwards there's structured data:
- 0x6f0..0x700: two u32 pointers (0x70c, 0x718)
- 0x700..0x758: u16 BE values, mostly in range 0x00..0x14 (= 0..20)
- 0x758..0x800: zeros (with a few zero-padding lines) leading to trailer

The u16 max value 0x14 = 20 = Block B count. So these are PROBABLY
indices into Block B — a per-class index list of "which Block B
records belong to which class."

## Re-probe findings (2026-04-29) — what `.fiz` actually IS

Combining the Block A/B decodings: a `.fiz` tile is a **baked
foliage mesh** — a small static mesh of 25-ish vertices and 20-ish
triangles in world coordinates, classified by material/class
(`0x0e01` low foliage, `0x0e02` mid bushes, `0x0803` tall impostors).
The 144 constant @+0x28 is plausibly the global material-class table
size; per-tile class set is much smaller.

This **REFUTES the original theory** that `.fiz` is a per-instance
scatter system holding hidden golf-cart / haybale / windmill
placements. Those are NOT here. `.fiz` carries authored low-poly
ground vegetation geometry pre-baked at world position; there are no
"asset references" to resolve because the geometry is inline.

That said — Phase 3 is not closed yet. Two angles still worth probing:

1. **Block C + Section D semantic**: still partially decoded. Block C
   is a sparse pointer table into Section D; Section D contains u16
   indices into Block B. These are likely per-class triangle index
   lists used by a runtime culler / shader binder. Useful to confirm
   the "no per-instance asset reference" conclusion.

2. **packed_u32 low 24 bits**: structure not yet identified. Plausibly
   a quantized normal + UV pair, or vertex color. Decode would help
   accurate re-rendering but doesn't unblock the placement search.

## Unresolved (Phase 3 remaining)

1. **packed_u32 low 24 bits**: high byte = class (decoded). Low 24
   bits structure TBD. Try standard packed-normal encodings (10/10/10,
   x18y6, etc.) and 16-bit UV.

2. **Block C indexing scheme**: stride and key encoding. The Block C
   non-zero entries in 800.fiz are at 0x4ee, 0x4f2, 0x4f6, 0x6cc —
   irregular spacing, so it's a sparse table not a fixed-stride
   array. Likely a hash-bucket or class-id-keyed lookup.

3. **Section D class index lists**: what classes of triangles each
   list selects. Probably per-render-pass or per-LOD groupings.

4. **Cross-tile verification**: confirm Block A vertex-pool model on
   1160.fiz and a few other sizes. Edge-length signatures should be
   stable across tiles for the same triangle types. (Doc claim that
   1160.fiz is a "golf-cart tile" is now suspect — golf carts likely
   don't appear in `.fiz` at all.)

## Next steps (resumable)

A. ~~Re-probe Block A~~ — **done 2026-04-29**.

B. ~~Check 0x60..0xb0 pre-block region~~ — **done 2026-04-29**.
   Resolved as sentinel slots.

C. **Decode packed_u32 low 24 bits**: small effort, see Unresolved #1.
   Probably yields normals + UVs and unlocks accurate mesh export.

D. **Cross-tile triangle-mesh verification**: run the
   `probe_fiz_block_a_triangles.py` probe on 1160.fiz, 1815.fiz,
   1026.fiz. Confirm the 3 vertex classes (`0xe01`/`0xe02`/`0x803`)
   appear consistently and that pointers stay in-bounds.

E. **Pivot the placement search**: since `.fiz` is mesh data and not
   prop placements, the missing golf-cart / haybale / windmill
   placements must live elsewhere. Closest remaining candidates
   (per memory log): the bin.zip "central directory IS the master"
   note — an unparsed bin.zip stream that references those props.

## Files

- `probes/probe_fiz_header_audit.py` — Phase 1 probe (header field
  audit across 45 tiles).
- `probes/probe_fiz_structure.py` — Phase 2 probe (full hex dump of a
  tile with anchored count fields).
- `probes/probe_fiz_differential.py` — earlier differential analysis
  that ruled out three obvious global ID encodings.
- `probes/probe_fiz_block_a.py` — Phase 3a Block A `(X,Y,Z,packed)`
  decode and tile-fit check.
- `probes/probe_fiz_block_a_triangles.py` — Phase 3a Block B triangle
  verification (edge lengths, type↔class correlation, Y-pair
  detection for impostor billboards).

## Memory pointers

- `project_fh1_fiz_format_2026-04-29.md` (in `~/.claude/projects/.../memory/`)
  — concise summary for cross-conversation recall.
