# `rmb.bin` — shared geometry pool (partial RE)

`coloradoout.NNNNN.rmb.bin` blobs hold the real vertex data for terrain
and (we believe) other large meshes whose PGEOs only carry placement
metadata. This doc captures what is decoded today; see
`docs/bin-zip-layout.md §3` for the high-level inventory and
`docs/pgeo-body.md §3.1` for the PGEO side (terrain v0 flat-quads).

Surveyed 2026-04-17. Source: `src/fh1_mapdecomp/rmb.py`.

> **Update 2026-04-18:** each rmb entry can carry one or more
> **sub-blobs** after the primary. For many non-TERR assets the
> authored mesh lives entirely in a sub-blob (primary is a near-empty
> 4-vertex placeholder). See `docs/rmb-subblobs.md` for the format and
> discriminator.

## 1. Blob envelope

Every blob we've looked at starts with the same 32-byte preamble:

    0x00  u32 BE  version (always 6)
    0x04  3×f32   first centroid (world x, y, z)
    0x10  4 bytes 00 00 00 00
    0x14  3×f32   second centroid
    0x20  16×f32  4×4 identity transform (identity in every TERR sample)
    0x60  u32 BE  ?
    0x64  u32 BE  4
    0x68  u32 BE  4
    0x6c  u32 BE  0
    0x70  3×u32   1, 1, 1
    0x78  3×f32   blob "center" (world)
    0x88  3×f32   local bbox min
    0x98  3×f32   local bbox max
    0xa4  u32 BE  ?
    0xa8  u32 BE  tag string count (=1 in TERR samples)
    0xac  u32 BE  tag string length
    0xb0  bytes   tag string (length from previous field)

The tag is an ASCII classifier and always sits around `0xb0`. Tags
starting with `TERR_` classify the blob as a terrain mesh; the tag
encodes region/zone/area/LOD (e.g.
`TERR_CLRD_Redstone_Area05_LOD00_14`,
`TERR_CLRD_FEST_AREA_02_LOD00`).

## 2. Vertex block (TERR blobs)

Immediately after the tag there is zero padding up to 8 bytes, then
four u32 BE fields:

    …  u32 BE  ? (=3 in samples)
    …  u32 BE  vertex count
    …  u32 BE  vertex stride (28 or 32)
    …  u32 BE  0
    …  bytes   vertex data (count × stride)

`rmb.py::_parse_vertex_header` shift-searches the next 8 bytes past the
tag for a plausible (vcount, stride) tuple, requiring `stride ∈ {28,
32}` and `vcount ∈ [4, 200_000]`.

### Record layout (per vertex)

Bytes 0..11 of each record are the world-space position as three f32 BE
values. The remaining 16 (stride=28) or 20 (stride=32) bytes are not
decoded yet — presumed normal / UV / colour / tangent. Encoding is
identical to what appears in the PGEO v42k7 body's record block (see
`docs/pgeo-body.md §3.2`) but at a different stride and without 10-10-10-2
packing — these are full f32 positions, so elevations are exact.

Verified on LOD00 terrain: Y range across 5.06M vertices is
`-38.8 .. 838.3 m`, which matches Colorado's real relief.

## 3. Index block (unparsed)

Past the vertex data is an index + material section that this project
does **not** yet decode. Hex surveys show sequences like:

    00 00 00 01 00 02 FFFF 00 03 00 04 00 05 FFFF …
    … "Material__25" …
    00 06 00 07 00 08 FFFF …

Strong evidence:

- u16 BE indices with `0xFFFF` as the triangle-strip restart marker
  (standard Xbox 360 / D3D9 convention).
- ASCII `Material__NN` strings interleaved between strip runs, so each
  strip likely carries its own material reference.

Open questions:

- Where the strip count / material count is stored, and how strips map
  onto the ASCII table (contiguous runs? length-prefixed blocks?).
- Whether the index block reuses the u16-tuple-of-four layout seen in
  terrain PGEO bodies at `0xa0` (see `docs/pgeo-body.md §3.1`), which
  has the same 0xFFFF-prefix sentinel semantics but at a different
  granularity (per-tile border stitching, not per-mesh topology).

Until this is parsed, `terrain_hi` tiles are triangulated by the Blender
importer via `mathutils.geometry.delaunay_2d_cdt` on the XZ projection
(Y kept as height). Single-valued-in-Y is a safe assumption for
non-overhanging terrain; the output is a reasonable surface for visual
export but should be replaced with the authored topology once the index
block is decoded.

## 4. TERR blob inventory

From the full LOD pass (all tag matches, no LOD filter):

| LOD    | blob count |
| ------ | ---------: |
| LOD00  |      2,569 |
| LOD01+ |  thousands (unprocessed) |
| (none) |  thousands (no `LOD\d+` in tag — water patches etc.) |

LOD00 is the highest detail and alone is sufficient to cover the
drivable Colorado surface (confirmed visually against the in-game map).

Stride distribution at LOD00: **1,609 blobs at stride 32** and
**960 blobs at stride 28**. The extra 4 bytes per record in the
stride-32 variant are likely a second UV channel or a colour/weight —
not yet identified.

## 5. Extraction pipeline (today)

`fh1_mapdecomp.rmb`:

- `iter_terr_blobs(zip_path, lod="00")` — dedup iterator over all
  `rmb.bin` entries, yielding `(Entry, bytes, TerrBlob)` tuples for
  blobs whose tag starts with `TERR_` and whose LOD matches.
- `decode_positions(buf, blob)` — numpy Nx3 f32 world positions.

`fh1_mapdecomp.terrain_hi.extract_terrain_hi`:

- Writes `out/terrain_hi/NNNNNN.npz` per tile (positions only, game
  space, Y-up).
- Writes `out/terrain_hi/index.json` with tag/lod/filename/bbox per
  tile.

CLI: `fh1-mapdecomp terrain-hi --source <bin.zip|dir> --output out/`.
Runs in a few seconds over the 102k-entry pool and produces 5.06M
vertices across 2,569 LOD00 tiles (~46 MiB on disk).

`fh1_mapdecomp.blender_scripts.import_world`:

- Reads `out/terrain_hi/index.json` when `--terrain-hi <dir>` is passed.
- Per tile: dedup XZ, `delaunay_2d_cdt`, reattach Y, Y-up → Z-up.
- Links each tile into collection `fh1_terrain_hi`.

Wired into `fh1-mapdecomp blender` and `fh1-mapdecomp all` by default;
`--no-terrain-hi` opts out.

## 6. What's next

Priority is decoding §3 (index + material block). That unlocks:

- Authored triangle topology (replaces Delaunay).
- Per-strip material hashes → bin.zip `.bix` texture references →
  textured terrain.
- A pattern we can reuse for non-TERR rmb.bin blobs (the other
  12k entries whose tags are not `TERR_*`).

Secondary: LOD01/LOD02 passes (same decoder, different `--lod` value),
and identifying what the stride-32 extra 4 bytes encode.
