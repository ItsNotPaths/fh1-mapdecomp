# Forza Horizon 1 — asset format notes

Reverse-engineering notes for extracting and reassembling the Colorado world
map from the Xbox 360 release of Forza Horizon 1 (Turn 10 / Playground Games,
2012). Everything here was derived empirically from a legitimately extracted
copy of the game, verified against Xenia-Canary.

All file offsets are zero-based. All multi-byte integers and floats in on-disk
game data are **big-endian** (Xenon PPC native). ZIP central-directory fields
remain little-endian per the PKZIP spec.

---

## 1. Game packaging

The commercial disc is a XGD3 ISO. We do not document ripping here — the
workflow assumed below is:

1. `iso2god` splits the ISO into a Games-on-Demand folder tree under
   `4D5309C9/00007000/2DC7007B/`.
2. Xenia-Canary verifies the content runs.
3. Our tooling operates on the extracted tree read-only.

Tree (only the parts relevant to a world-map export):

```
4D5309C9/00007000/2DC7007B/
├── default.xex               # main executable (PPC, encrypted)
└── media/
    ├── db/
    │   └── gamedb.slt        # SQLite — car/part metadata
    └── tracks/
        └── colorado/
            ├── bin.zip                 # 2.9 GB asset container (230,057 entries)
            ├── colorado.nav            # navmesh
            ├── PhysicsDefinitions.bin  # physics
            ├── co_cone.bin             # cones
            └── Ribbon_00/              # placement + metadata for the Colorado map
                ├── Colorado_00.hex     # HEXY — hex-grid index
                ├── Colorado_00.pvs     # FPVS — potentially visible set
                ├── PVSZLookup_00.dat   # zone-hash → slot
                ├── FilenameMap_00.dat  # flat filename table
                ├── GameObjs.xml        # Barn-Find placements
                ├── CollObjs.xml        # trackside collision props
                ├── ParticleEmitters.xml
                ├── PostProcessingZones_Safe.xml
                ├── PreRaceCams.xml
                ├── SmashableEffects.xml
                ├── TimeOfDay*.xml
                └── TrackRoute0NN.xml   # race routes
```

Colorado is the only released world map, so `Ribbon_00/` is the whole world.

Other `media/tracks/<name>/` dirs exist for UI screens (uipaintshop,
uiautoshow, …) — they ship their own small bin.zips. Out of scope here.

---

## 2. `bin.zip` — the container

`bin.zip` is a pseudo-ZIP: the central directory is a standard PKZIP stream at
the end of the file and parses cleanly with Python's `zipfile`, but the **local
file headers are absent or garbage**. Data lives directly at each central-
directory `header_offset` for `compress_size` bytes.

```python
import zipfile
with zipfile.ZipFile("bin.zip") as z:
    for info in z.infolist():
        # info.header_offset points at raw compressed data, NOT a local header
        # info.compress_type: 0=stored, 21=LZX (see §3)
        ...
```

### Entry extensions and counts (Colorado bin.zip)

| Ext          | Count     | Purpose                                                              |
| ------------ | --------: | -------------------------------------------------------------------- |
| `.bin`       |   115,200 | `.rmb.bin` collision meshes, other binary blobs                      |
| `.bix`       |    42,657 | Binary index-by-hash (`_0x1234ABCD_B.bix`). Candidate texture blobs. |
| `.pgeo`      |    41,587 | **Geometry chunks** (see §4). Named `__R00GNNNNN.pgeo`.              |
| `.fiz`       |    16,934 | `fiz ` magic, world-coord header. Per-tile foliage density (probably). |
| `.sh`        |     4,768 | Spherical-harmonics lighting tiles. Filename encodes world coords (`Colorado__shdata__n2550x_n1645z.sh`; n=−, p=+). |
| `.soundscape`|     4,675 |                                                                      |
| `.pvsz`      |     3,502 | Per-zone authored placement transforms. Decoded — see `pvs-format.md`. |
| `.bundle`    |       429 | Compound bundles keyed by `0x1NNNNNNN`.                              |
| `.fxobj`     |       173 | Shaders (`shaders/track/ADD_DIFF_OPAC_RGBA.fxobj`).                  |
| `.fsb`       |        66 | FMOD sound banks.                                                    |
| `.fev`       |        66 | FMOD events.                                                         |

Duplicate names are common: the archive often stores both `__R00G00475.pgeo`
and `__r00g00475.pgeo`. Our extractor dedups with a `__dupN` suffix when paths
collide.

---

## 3. Compression — LZX with Turn 10 chunk framing

`compress_type == 0` entries are raw stored bytes — slice
`[header_offset : header_offset + compress_size]` and you're done.

`compress_type == 21` entries are Xbox-360-flavour LZX. Decoder parameters:

```
window_bits    = 17
reset_interval = 0
is_delta       = 0
```

(Microsoft's LZX, not LZMA2. The `21` value collides with 7-Zip's method numbering
and misled us for about an hour.)

### Per-entry framing

Each compressed entry is split into one or more chunks. Each chunk decodes to
exactly **32,768** uncompressed bytes, except the final chunk which may be
shorter. The on-disk format of a chunk depends on whether it is the final one:

- **Non-final chunk** — bare `u16 BE csize`, followed by `csize` bytes of LZX.
- **Final chunk** — `FF [u16 BE uncomp] [u16 BE comp] <comp bytes LZX> <5-byte trailer>`.

Entries whose total uncompressed size is ≤ 32,768 bytes are therefore entirely
"one final chunk" and begin with `FF` on disk. Multi-chunk entries begin with
the first bare `csize` and have the `FF` prelude only on the last chunk.

### Why strip the headers

The LZX decoder state (window, prev match tables) must persist across chunks —
reinitialising per chunk produces garbage on chunks ≥ 2. We strip all chunk
framing in one pass and feed the decoder a single concatenated LZX bitstream of
length `sum(csize_i)`.

### `tools/lzxd_helper`

libmspack on Devuan/Debian does not export the public LZX API from its shared
library (only the CAB frontend). We build a local helper from upstream
libmspack source (cloned to `tools/libmspack_src/`) that links against
`lzxd.c` + `system.c` and uses the internal
`lzxd_init` / `lzxd_decompress` / `lzxd_free` directly.

Build:
```
cd tools
gcc -O2 -I libmspack_src/libmspack/mspack -I libmspack_src/libmspack \
    lzxd_helper.c \
    libmspack_src/libmspack/mspack/lzxd.c \
    libmspack_src/libmspack/mspack/system.c \
    -o lzxd_helper
```

Helper CLI (stdin → stdout):
```
lzxd_helper single     WBITS RESET OUTLEN          < stream.bin > out.bin
lzxd_helper chunked_be WBITS RESET OUTLEN CHUNKSZ  < framed.bin > out.bin
```

`single` is the production path: Python (`tools/fh1_binzip.py`) strips the
chunk framing and hands the helper a continuous stream. `chunked_be` was used
during RE to verify our framing hypothesis against the helper's own stripper.

---

## 4. `.pgeo` — geometry chunks

Magic: the first four bytes on disk are `O E G P` (i.e. `"PGEO"` big-endian
reversed). Every PGEO begins with a fixed 52-byte header:

| Off  | Size | Type    | Field                                           |
| ---: | ---: | ------- | ----------------------------------------------- |
| 0x00 |   4  | bytes   | magic `"OEGP"`                                  |
| 0x04 |   4  | u32 BE  | version (42, 43, 44 observed)                   |
| 0x08 |   8  | 2×f32   | scale pair (meaning depends on variant)         |
| 0x10 |  12  | 3×f32   | `bbox_min` in world space                       |
| 0x1C |   4  | u32     | padding (always 0)                              |
| 0x20 |  12  | 3×f32   | `bbox_max` in world space                       |
| 0x2C |   4  | u32     | padding (always 0)                              |
| 0x30 |   4  | u32 BE  | `kind` — variant selector (2,3,4,5,6,7,8)       |

`bbox_min` and `bbox_max` are in absolute Colorado world coordinates (metres,
Y-up). **This IS the chunk's world placement** — for map reassembly we do not
need a separate placement file. Terrain tiles use `Y = ±100000` as a
"don't care, terrain is height-varying" placeholder; downstream code must
clamp.

### Variants

The `(version, kind)` pair combined with the scale pair selects a different
internal layout after byte 52. Full-archive survey across 41,587 chunks:

| Variant tag     | `ver` | `kind` | Scale                  | Count  | Engine class                  | Contents                                      |
| --------------- | :---: | :----: | ---------------------- | -----: | ----------------------------- | --------------------------------------------- |
| `terrain`       |  42   |   8    | (-1, -1)               |  2,288 | `CProceduralLightMaps`        | Terrain tiles, reference a `LightMap_X_Y` sub-block. Y-bbox is the ±100000 placeholder. |
| `grass`         |  42   |   2    | (-1, -1)               | 11,292 | `CProceduralVegetation`       | Foliage scatter (grass tufts AND tree/shrub instances), string `Grass_Ungrouped_NNNNN_N`. The engine's actual "vegetation" class. |
| `crowd`         |  42   |   3    | (60, -1)               |  8,544 | `CProceduralCharacters`       | Animated crowd + a few inanimate prop loops, strings like `crowd_proc_clrd_festival` / `crowd_stages_117`. |
| `light_glows`   |  43   |   6    | (1000, 1000)           |  3,316 | `CProceduralLightGlows`       | **Light entity data** — position + direction + cone + color + intensity. Was misnamed `vegetation` until 2026-05-03. Strings like `Glow_WorldStaticLightGlows_NNNN`. |
| `v42k7`         |  42   |   7    | (100,100)/(150,150)/(300,150)/(300,2500) | 14,122 | `CProceduralModels` | Per-section instance groups referencing rmb pool entries; or proc-inline placements. |
| `v44k5`         |  44   |   5    | (-1, -1)               |  1,578 | (likely `CProceduralPoints`)  | Procedural FX emitters (`anim_proc_clrd_fx_*`). |
| `landmark_anim` |  44   |   4    | (-1, -1)               |    447 | `CProceduralAnimatedObject`   | Festival rides + autoshow rigs (Ferris wheel, etc). Largest files (up to 540 KB uncompressed). |

Engine class names confirmed from xex RTTI dump
(`docs/xex-walk/04-rtti-classes.txt`, namespace
`proceduralGeometry::CProcedural*`). Use them — the literal file
`(ver, kind)` and tag-prefix can mislead (e.g. `(43, 6)` was historically
labeled `vegetation` based on tag-prefix guessing; it's actually light
data per RTTI).

Per-variant vertex/index/material decode is **in progress** — currently only
the shared header is parsed (`tools/pgeo.py`). Planned approach:

- Xbox 360 vertex decls are typically `D3DDECLTYPE_DEC3N` (10-10-10-2 packed)
  for normals, `FLOAT16_2` for UVs, `UBYTE4N` for colors, all big-endian.
- Triangle lists use `u16 BE` indices; index buffers terminate on `0xFFFF`
  strip restarts in many engines of this era.
- The `scale` pair likely rescales packed vertex coords into world units; e.g.
  the `light_glows` variant's `(1000, 1000)` pair is the [0, 1000]³
  packed-coord cube used by some sub-format.

Start with `landmark_anim` (few files, lots of data) or `terrain` (well-bounded
grid + visible LightMap header) — both have clearer structure than the denser
variants.

---

## 5. Placement indices (`Ribbon_00/`)

These files orchestrate the Colorado map. Every multi-byte int/float is
big-endian unless stated.

### 5.1 `Colorado_00.hex` — HEXY hex-grid

Header (32 bytes), then a sparse grid, then per-tile records.

| Off  | Size | Field                                                     |
| ---: | ---: | --------------------------------------------------------- |
| 0x00 |   4  | magic `"HEXY"`                                             |
| 0x04 |   4  | u32 version (101)                                         |
| 0x08 |   4  | f32 `pitch` (metres between cell centres; 100.0)          |
| 0x0C |   4  | f32 `origin_x` (-6700.0)                                  |
| 0x10 |   4  | f32 `origin_z` (-6495.1904…)                              |
| 0x14 |   4  | u32 `count` (populated tiles; 1434)                       |
| 0x18 |   4  | u32 `width` (columns; 91)                                 |
| 0x1C |   4  | u32 `height` (rows; 60)                                   |

After the header:

- **Sparse grid**: `width × height` `u32 BE` (21,840 bytes). Each cell holds a
  zero-based tile index into the tile-records array, or `0xFFFFFFFF` for empty.
- **Tile records**: `count` × (`u32 col`, `u32 row`) = 1434 × 8 bytes.
- **Tile flags**: `count` × `u8`. Mostly `0x3F` (= `0b111111`, all six hex
  neighbours present); lower values indicate missing neighbours.

Cell `(col, row)` lies at world position
`(origin_x + col·pitch, origin_z + row·pitch)`.

### 5.2 `PVSZLookup_00.dat`

1434 entries of `(u32 BE hash, u32 BE zone_index)`. `zone_index` is simply the
array index (0…1433 in ascending order). The hashes are short (all ≤ 0xFFFF in
the observed data) and likely match the zone id referenced by `.pvsz` files in
bin.zip.

### 5.3 `FilenameMap_00.dat`

A flat string table:
- `u32 BE` offsets start at file offset 0; the first offset tells you where the
  string region begins (and therefore how many offsets there are).
- Each offset points at a NUL-terminated ASCII string back in the same file.
- Colorado has 43,546 entries — the full list of bin.zip filenames referenced
  by the Colorado ribbon. Extension breakdown matches the bin.zip subset used
  for this track (see `tools/index_parse.py` output).

No structure beyond the flat list — this is a file-name index, not a
placement map. Placement comes from PGEO headers (§4) and the XML files
(§5.4).

### 5.4 XML placement files

`GameObjs.xml`, `CollObjs.xml`, `ParticleEmitters.xml`, `TrackRoute0NN.xml`,
etc. are all cleartext XML. Shape:

```xml
<Obj0 GameplayID="BF_CUDA_426BF_CLOSEDC">
  <Pos x="-1291.224976" y="46.967731" z="-3502.273926"/>
  <Orientation>
    <XAxis x="-0.888851" y="-0.028361" z="-0.457318"/>
    <YAxis x="-0.016422"  y="0.999413" z="-0.030062"/>
    <ZAxis x="0.457902" y="-0.019211" z="-0.888795"/>
  </Orientation>
</Obj0>
```

- `GameObjs.xml` — Barn-Find car placements (cosmetic).
- `CollObjs.xml` — trackside collidable props (armco, cones…). `GraphicsName`
  is a `#N`-form numeric index, not a filename.
- `TrackRoute0NN.xml` — race routes as waypoint lists.

---

## 6. Tool inventory

Everything lives in `tools/`. Outputs go to `out/`.

| Tool                               | What it does                                                    |
| ---------------------------------- | --------------------------------------------------------------- |
| `lzxd_helper.c` → `lzxd_helper`    | Native helper that decodes a single LZX stream via libmspack's internal API. |
| `fh1_binzip.py`                    | Library + CLI to list/extract `bin.zip` entries (methods 0+21). |
| `pgeo.py`                          | Parses the 52-byte PGEO header, tags variants.                  |
| `index_parse.py`                   | Parses HEXY, PVSZLookup, FilenameMap.                           |
| `world_assemble.py`                | Reads all PGEOs, emits `out/world/colorado/ribbon_00.json`.     |
| `plot_pgeo_bboxes.py`              | Matplotlib scatter of all chunk centres (colour per variant).   |
| `blender_import.py`                | `blender --background --python` builds a .blend scene from `ribbon_00.json`. |
| `blender_render_top.py`            | Orthographic top-down PNG of a .blend.                          |
| `survey_pgeo.py`                   | Classifies every PGEO and tallies variants.                     |
| `test_extract.py`                  | Phase 1 smoke test — extracts one example per variant.          |
| `probe_lzx*.py`, `probe_nx_single.py` | RE-era probes for LZX framing. Kept for history.             |

### Common commands

List every foliage PGEO:
```
python3 tools/fh1_binzip.py \
    4D5309C9/00007000/2DC7007B/media/tracks/colorado/bin.zip list \
    --glob '*.pgeo'
```

Extract every terrain PGEO:
```
python3 tools/fh1_binzip.py \
    4D5309C9/00007000/2DC7007B/media/tracks/colorado/bin.zip extract \
    --out out/bin --glob '__R00G*.pgeo'
```

Build the world placement JSON (re-run if bin.zip changes):
```
python3 tools/world_assemble.py
```

Build the placement-cube Blender scene:
```
/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender \
    --background --python tools/blender_import.py -- \
    --json out/world/colorado/ribbon_00.json \
    --out  out/blender/colorado.blend
```

Render a top-down PNG of the scene:
```
/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender \
    --background --python tools/blender_render_top.py -- \
    out/blender/colorado.blend out/blender/colorado_topdown.png
```

---

## 7. Coordinate systems

- **Forza world**: right-handed, **Y-up**. X/Z horizontal, Y vertical.
  World-space units are metres.
- **Blender**: right-handed, **Z-up**.
- Import conversion used throughout `blender_import.py`:
  `(x, y, z)_game → (x, -z, y)_blender`.
- The `.sh` spherical-harmonics filenames also encode world coords directly:
  `Colorado__shdata__n2550x_n1645z.sh` means tile at X=−2550, Z=−1645 (n/p
  prefixes are minus/plus).

---

## 8. What's known vs. still unknown

**Known / implemented**
- Full bin.zip entry extraction, any method.
- LZX framing fully reversed.
- PGEO 52-byte header and variant taxonomy.
- HEXY, PVSZLookup, FilenameMap parsers.
- World bounding-box placement for all 41,587 PGEO chunks (the PGEO header
  itself carries this).
- Blender scene of placement cubes showing the recognisable Colorado outline.

**Not yet reversed**
- PGEO per-variant vertex / index / material decoding.
- `.fiz` interior structure (header looks like a per-tile foliage/field
  descriptor, not a texture).
- `.bix` format — probable texture blobs but no confirmed magic or layout.
- `.bundle` — compound asset bundles; structure unknown.
- ~~`.pvs` / `.pvsz` internals — magic `FPVS`; byte layout unknown.~~ Decoded 2026-05-03 (port of Doliman100 / austinbaccus FH1 fork). See `pvs-format.md`.
- Spherical-harmonics payload inside `.sh` files.

**Explicitly out of scope**
- Car models, liveries, UI, FMOD audio playback, navmesh/physics —
  extractable via the same tools but not needed for a visual Blender map export.
- `default.xex` reverse engineering — only considered if a format cannot be
  reversed any other way.
