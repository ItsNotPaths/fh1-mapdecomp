# FH1 texturing investigation — opening notes (2026-05-03)

This is the kickoff document for the texture/material extraction work.
Geometry (positions+faces) ships in `out/meshes/*.npz`; UVs, materials,
and textures are all unaddressed. The plan is to use Doliman100 et
al.'s `forza-x360-io` (FM2 / FM3 era) as a Rosetta stone — same engine
family, same Xbox 360 GPU formats, similar container layouts — and
adapt where the FH1 disk format diverges.

Status: phase 1 (audit) only. Implementation starts with the next doc.

---

## TL;DR

The reference repo gives us **everything we need to decode the bytes
on disk**: CAFF (texture container), BIX (paired texture file), Xbox
360 GPU detiling/deswizzling, RMB material/shader/sampler decoding,
and shader→texture binding through the PVS file. The FM3 version of
each format is documented down to the field; FH1 will deviate in
specific places (CAFF version, RMB layout offsets, PVS schema), but
the data model (mesh → material → sampler index → PVS texture entry →
`_0x{HEX}.bin`) almost certainly survives.

The five things to port, in order:

1. **Vertex format** (`forza_vertex.py`): position is 3×f32 BE, UVs
   are 2×u16 BE normalized to `/65535`. Already matches our observed
   FH1 strides (16/20/24/28/32/36).
2. **RMB material set** (`read_rmbbin.py`): per-section
   `texture_sampler_indices[]` + `fx_filename_index`. We currently
   discard everything past positions+faces.
3. **PVS texture table** (`read_pvs.py`): the lookup that turns
   `texture_sampler_indices[i]` into the actual `_0x{HEX}.bin`
   filename. Each entry also has per-sampler U/V scale and translate.
4. **CAFF reader** (`read_bin.py` + `CAFF.bt`): pulls the texture
   header (format, width, height, mip count, base data ptr) and the
   raw GPU-tiled pixel data out of `_0xHHHH.bin`.
5. **Detile + DDS wrap** (`deswizzle.py` + `read_bix.py`):
   `XGUntileSurfaceToLinearTexture` → linear blocks → DDS file.

Each step has a known FH1 deviation — see "FH1 deltas" at the end of
each section.

---

## Where every file in the reference fits

```
forza_blender/forza/
├── pvs/
│   ├── read_pvs.py        ← shader+texture+model tables, instance transforms
│   └── pvs_util.py        ← BinaryStream (f16/f32/string helpers)
├── models/
│   ├── read_rmbbin.py     ← RMB header → MaterialSet → Material[]
│   ├── forza_track_section.py   ← VertexBuffer + subsections
│   ├── forza_track_subsection.py← per-submesh: material_index, uv xform, IB
│   ├── forza_vertex.py    ← stride parser — POSITION0 + TEXCOORD0/1/2
│   └── index_type.py      ← TriList=4, TriStrip=6
├── shaders/
│   ├── read_shader.py     ← FXLShader → VertexDeclaration → VertexElement
│   ├── shaders.py         ← per-shader-name graph builders (Blender nodes)
│   └── shaders_util.py    ← generate_image_texture_nodes_for_material()
├── textures/
│   ├── read_bin.py        ← CAFF header → TextureAsset → DDS
│   ├── read_bix.py        ← _B.bix + .bix → DDS
│   ├── texture_util.py    ← integer index → _0x{HEX}.bin filename
│   └── CAFF.bt            ← 010-Editor binary template — formal CAFF spec
└── utils/
    ├── deswizzle.py       ← XG tiling math (32-block macro/micro tiles)
    └── mesh_util.py       ← TriStrip → TriList
```

The `.bt` binary templates are the cleanest source — they document
every byte. `CAFF.bt` is 1336 lines and covers the whole asset
container including pixel/vertex shaders, vertex declarations,
texture headers, command stream, and symbol table.

---

## The data model (FM3 confirmed; FH1 to verify)

```
RMB.bin (a single mesh)
 ├── track_sections[N]                  ← what we already parse as RmbSection
 │    ├── vertex_buffer (length, stride, raw bytes)
 │    └── subsections[M]                ← submeshes within a section
 │         ├── material_index           ← into material_sets[0].materials
 │         ├── uv_offset[2], uv_scale[2]← per-submesh UV transform
 │         └── index_buffer (TriList | TriStrip)
 ├── material_sets[K]                   ← typically K=1
 │    └── materials[M]
 │         ├── fx_filename_index        ← into shader_filenames[]
 │         ├── pixel_shader_constants[] ← f32 inputs to shader
 │         └── texture_sampler_indices[]← s32 per-sampler — see below
 └── shader_filenames[]                 ← strings like "shaders\concrete.fx"

PVS file (the scene-level table that joins mesh refs to texture files)
 ├── textures[T]                        ← global texture pool
 │    └── PVSTexture
 │         ├── texture_file_name (u32)  ← THIS IS THE INTEGER FILENAME
 │         ├── u_scale, v_scale
 │         └── u_translate, v_translate
 ├── shaders[S]                         ← shader path strings (.fxobj basenames)
 ├── models[]                           ← per-mesh metadata
 │    └── PVSModel
 │         ├── textures[]               ← list of indices into pvs.textures[]
 │         └── shaders[]                ← list of indices into pvs.shaders[]
 └── models_instances[]                 ← per-placement: model_index, transform, texture override
```

**The join (FM3):**

```
mesh subsection
  → subsection.material_index
  → material_set[0].materials[material_index].texture_sampler_indices[i]
                          (int N: index into pvs_model.textures[])
  → pvs_model.textures[N]   (int K: index into pvs.textures[])
  → pvs.textures[K].texture_file_name   (int H: the actual filename)
  → bin/textures/_0x{H:08X}.bin   (CAFF container with the DDS data)
```

Special sentinels in `texture_sampler_indices`:
- `-2` → no texture in this slot
- `-1` → "inherited" texture, comes from instancer attribute (FM3 has
  per-instance texture overrides — see `PVSModelInstance.texture`)

---

## CAFF taxonomy (`textures/CAFF.bt`)

Single container format used for `.rmb.bin`, `.stx.bin`, and
`_0x*.bin`. Identified by the 4-char magic `CAFF`, then a 16-char
version string `DD.MM.YY.BBBB` (e.g. `21.11.05.0034`).

**Version split** (line 33 of CAFF.bt):

| version range | header layout | endianness byte |
|---|---|---|
| < 21.11.05.0034 | unsupported by reference | — |
| 21.11.05.0034 → 07.08.06.0036 | "old" (FM2/FM3 → here) | offset 76 |
| ≥ 07.08.06.0036 | "new" | offset 72 |

FH1 is **CAFF v21.11.05.0034** per memory (the
`hashbin_caff_textures` find). That means the **old** layout — same
as the reference's `read_bin.py`. So the Python port should drop in
nearly verbatim.

**Header structure (old layout, 400 bytes total):**

```
0x00  char[4]   magic "CAFF"
0x04  char[16]  version_string "21.11.05.0034\0\0\0"
0x14  u32       hash (PJW32 of header with hash=0)
0x18  u32       assets_length         ← number of asset names
0x1C  u32       sections_length       ← number of section descriptors
0x20  HeaderUnknown1 unk_1            (16 bytes; a/b length pairs)
0x30  HeaderUnknown1 unk_2            (16 bytes)
0x40  u32       info_unk1_length
0x44  u32       data_allocation_block_size  ← table_0+table_1 live inside .data block at this offset
0x48  u32       header_size           ← absolute offset where allocation blocks start
0x4C  u8        endianness            (0=LE, 1=BE)
0x4D  u8        allocation_blocks_length  (typically 4: .data, .gpu, .gpucached, .stream)
0x4E  u8        compression           (0=none, 1=zlib)
0x4F  u8        info_unk0_length
0x50  AllocationBlockInfo[N] (40 bytes each):
        char[11] name (e.g. ".data", ".gpu")
        u8       alignment (1<<n)
        u32      base_section_index
        u32      uncompressed_size
        u32      data_ptr            (filled by process at runtime)
        u32      data_offset
        u32      data_overlap
        u32      data_size
        u32      compressed_size
0x190 u8[…]     padding to 0x190 (400 - 80 - 40*N)
```

After the header come the actual allocation blocks (uncompressed for
us — we never see compressed here on disk). Inside the `.data` block,
at offset `data_allocation_block_size`, lives Table 0:

- `assets_names_size` u32
- `assets_names_offsets[assets_length]` (offsets into the name pool)
- `assets_names[assets_names_size]` (ASCII null-separated)
- `unk_names_size` + optional `unk_names`
- `sections_info[sections_length]` (14 bytes each):
  `asset_index, asset_offset, asset_size, allocation_block_index (1-based), alignment`

Then Table 1 (pointer fixup tables — `unk_1_b`, `unk_2_b` etc.).
After applying fixups, the section data lives in the appropriate
allocation block. For texture `_0x*.bin` files the relevant asset is
a `TextureAsset`:

```
0x00  u8[24]   gap
0x18  u32      format        ← D3DFORMAT bitfield (see below)
0x1C  u32      type          ← TEX_DIRECT=0, TEX_CUBE=2, TEX_ARRAY=4, TEX_HARDWARE_ARRAY=5
0x20  u8[4]    gap
0x24  u16      width
0x26  u16      height
0x28  u32      base_offset_ptr  ← Pointer to .gpu block where pixel data lives
0x2C  u32      mip_offset       ← 0xFFFFFFFF = contiguous mips
0x30  u8       levels           ← mip count
0x31  u8[3]    gap
0x34  u32      pTexture_ptr     ← runtime, ignore
0x38  u32      depth            ← or array length for arrays
0x3C  u8[8]    gap
0x44  u32      dword44
0x48  u32      dword48
0x4C  u32      dword4C
```

**The `format` field is a bitfield** (line 1227 of CAFF.bt):
- bits 0-5: `TextureFormat` (GPU format, see table below)
- bits 6-7: `Endian` (GPUENDIAN_NONE=0, _8IN16=1, _8IN32=2)
- bit 8: `Tiled` (0=linear, 1=XG-tiled)
- bits 9-10: `TextureSignX` (UNSIGNED/SIGNED/BIAS/GAMMA)
- bits 11-22: SignY/Z/W
- bit 23: `NumFormat` (FRACTION=0, INTEGER=1)
- bits 24-31: 4× swizzle masks

| GPU format value | D3DFORMAT name | DXGI equivalent | block | bytes/block |
|---|---|---|---|---|
| 6 | 8_8_8_8 | B8G8R8A8 / B8G8R8X8 | 1×1 | 4 |
| 18 | DXT1 | BC1_UNORM (71) | 4×4 | 8 |
| 19 | DXT3 | BC2_UNORM (74) | 4×4 | 16 |
| 20 | DXT5 | BC3_UNORM (77) | 4×4 | 16 |
| 49 | DXN | BC5_UNORM (83) — normal maps | 4×4 | 16 |
| 59 | DXT5A | BC4_UNORM (80) — single channel | 4×4 | 8 |

Plus L8 (671088898) and X8R8G8B8 (673710470) as raw 32-bit forms.

**FH1 deltas to verify:**
- Memory says all 13,187 hash-named .bin files in colorado bin.zip
  are CAFF v21.11.05.0034 — matches the old-layout assumption.
- Endianness: FH1 is Xbox 360 only, expect BE everywhere.
- The `.bix` may not be CAFF at all — see BIX section below.

---

## BIX taxonomy (`textures/read_bix.py`)

A second texture container, paired into two files:
- `name.bix` — a 28-byte header (no payload)
- `name_B.bix` — the raw GPU-tiled pixel data

Header is plain BE u32 fields:

```
0x00  u32  magic (1112102960 = 0x42495830 'BIX0', or 1112102961 'BIX1')
0x04  u32  width
0x08  u32  height
0x0C  u32  levels
0x10  u32  format       ← same D3DFORMAT bitfield as CAFF
0x14  u32  total_size
0x18  u32  main_size
```

The pixel data is in `_B.bix` and gets the same detile +
DDS-wrap treatment as CAFF.

**FH1 deltas:**
- Memory confirms: FH1 .bix files have NO embedded ASCII names. The
  reference assumes the file is named after its referenced asset
  (`{name}.bix` + `{name}_B.bix`). FH1 file names appear to use the
  `_0x{HEX}.bix` hash convention (need to confirm by listing
  `out/extracted/`).
- Memory says BIX is `.bix` + `_B.bix`, magic `BIX1`. So the same
  pair scheme applies; we just lose the ASCII-name fallback for human
  labels.
- Disjoint hash space from `_0x*.bin`: BIX hashes never collide with
  CAFF hashes per the 2026-04-29 investigation.

---

## XG tiling / deswizzling (`utils/deswizzle.py`)

Xbox 360 GPU textures are laid out in a tiled pattern (32×32 macro
tiles, 8×8 micro tiles inside, with per-block byte interleaving) for
efficient texture cache use. To get a linear block stream you have to
"untile" with `XGAddress2DTiledOffset` — pure index math, ~7 lines.

There's also an endian step:
- `(format >> 6) & 3` → 0 = no swap; 1 = swap each u16; 2 = swap each
  u32. Required because the GPU expects local-endian per-block, and
  the BE Xbox stores them swapped.

After untile + endian flip, the linear blocks are wrapped in a DDS
header. The reference has wrappers for both legacy DXT FOURCC
(`DXT5`) and modern DX10 extension (`BC1`/`BC4`/`BC5` etc.). DX10
form is preferred (BC4/BC5 don't have legacy FOURCCs).

**FH1 deltas:** Pure GPU math — should be identical. No FH1 deviation
expected.

---

## RMB material decoding (`models/read_rmbbin.py`)

In FM3, the `.rmb.bin` is a CAFF asset with a custom payload. After
the standard CAFF unwrap, the payload is:

```
u32  version
u8[112] header
u32  track_sections_count
ForzaTrackSection[track_sections_count]:
  asserts (validates fixed-form header)
  string  name
  VertexBuffer (version, length, stride, version>=3 → +4, raw data)
  u32 = 1
  u32  sub_count
  ForzaTrackSubSection[sub_count]:
    asserts
    string  name
    u32     index_type   (4=TriList, 6=TriStrip)
    u32     material_index
    asserts (1, 0, 0, 0, 0, 1.0, 1.0, 1.0, 1.0)
    f32[2]  uv_offset
    f32[2]  uv_scale
    IndexBuffer (version, version>=4 → +4, length, stride, raw)
    skip 4
u32  version
u32  material_sets_length
MaterialSet[material_sets_length]:
  skip 8
  u32  materials_length
  Material[materials_length]:
    skip 4 (version)
    u32  fx_filename_index           ← shader filename (index into shader_filenames[])
    skip 4 (technique_index)
    skip 4 (vertex shader constants version)
    u32  vsc_length; skip 16*vsc_length
    skip 4 (pixel shader constants version)
    u32  psc_length; f32[4*psc_length] pixel_shader_constants
    skip 4 (texture sampler version)
    u32  tsi_length
    s32[tsi_length] texture_sampler_indices   ← THE KEY ARRAY
u32  fx_filenames_count
string[] shader_filenames                       ← e.g. "shaders\\concrete_diff_5.fx"
```

**Where it joins to PVS:** `texture_sampler_indices[i] = N` means
"sampler i of this material reads pvs.textures[pvs_model.textures[N]]".

**FH1 RESULT (2026-05-03 — task #3 complete):**

The FM3 material set grammar is **embedded verbatim in our existing
`RmbTrailer.raw`**, just at the start. Decoded via
`probes/probe_rmb_material_set.py` and validated on 1588 RMBs.

Trailer header (FH1):

    u32 trailer_version            (5 or 6)
    u32 ?                          (always 1)
    u32 ?                          (always 1)
    u32 ?                          (always 1)
    u32 ?                          (always 1)
    u32 materials_length           (== n_sections)

Per material (identical to FM3 `Material.from_stream`):

    u32 material_version           (3)
    u32 fx_filename_index          ← into trailer.shader_paths
    u32 technique_index            (0)
    u32 vsc_version                (1)
    u32 vsc_length
    [16 * vsc_length] bytes        (VSC vec4s)
    u32 psc_version                (1)
    u32 psc_length
    [16 * psc_length] bytes        (PSC vec4s)
    u32 tsi_version                (1)
    u32 tsi_length
    [4 * tsi_length] s32           ← THE KEY ARRAY

Materials are 1:1 with sections in section_idx order. Sentinels
match FM3: -2 = unbound sampler, -1 = instancer override.

Validation:
- 1588/1613 RMBs with trailers parsed materials cleanly (98.5%)
- 100% of those have `len(materials) == len(sections)`
- TSI value range [-2, 26], with -2 and -1 present as expected
- `fx_filename_index` bounded to `len(shader_paths) - 1`

Wired into `rmb.py`:
- `RmbMaterial(fx_filename_index, pixel_shader_constants, texture_sampler_indices)` dataclass
- `RmbSection.material: RmbMaterial | None` field
- `_parse_material_set()` does the parse, with strict bounds checking
- `_attribute_materials()` replaces `_attribute_shader_paths()` when
  the material set parses; falls back to the older 1:1 path
  attribution otherwise

The complete data join (FH1, confirmed):

    RmbSection.material.texture_sampler_indices[i]   (small int N)
        → PvsModel.textures[N]                         (int K)
            → Pvs.textures[K].texture_file_name          (int H)
                → bin/textures/_0x{H:08X}.bin              (CAFF container)

---

## Shader binding (`shaders/read_shader.py`, `shaders/shaders.py`)

The `.fxobj` files are FXL shader programs. The reference parses just
enough to extract the **vertex declaration** (`VertexDeclaration` →
`VertexElement[]`) — that's how the vertex stride is interpreted. A
VertexElement has:

```
u16 stream
u16 offset
u32 type        ← D3DDECLTYPE (e.g. FLOAT3=2761657, USHORT2N=2891865, DEC4N=1712519, D3DCOLOR=1583238)
u8  method
u8  usage       ← D3DDECLUSAGE (POSITION=0, NORMAL=3, TEXCOORD=5, TANGENT=6, BINORMAL=7, COLOR=10)
u8  usage_index ← e.g. usage=5, usage_index=2 → TEXCOORD2
u8  pad
```

So **the shader tells you how to read the vertex buffer**. In FM3,
`forza_vertex.py` walks the declaration and builds a numpy structured
dtype on the fly.

`shaders.py` is a 1300-line library of per-shader-name Blender node
graphs (`diff_1`, `diff_opac_2`, `wood_glossy_4`, etc.) that map
texture sampler slots to BSDF inputs. This is the **rendering recipe**
— fully optional for our purposes (a generic Principled BSDF that
takes Diffuse from sampler 0 and Normal from sampler 1 covers 90% of
the look).

**FH1 deltas:**
- We don't have `.fxobj` files extracted yet. Need to check bin.zip
  for `*.fxobj`.
- If FH1 ships compiled shaders only, we may need to **infer** the
  vertex layout from the stride alone. Our observed strides
  (16/20/24/28/32/36) suggest:
  - 16 = pos(12) + uv(4) — DXT5N or just diffuse
  - 20 = pos(12) + uv(4) + color/dec4n(4)
  - 24 = pos(12) + uv(4) + uv2(4) + dec4n(4)
  - 28-36 = additional UV sets, normals, tangents
- The reference observes `D3DDECLTYPE_FLOAT3` for position and
  `D3DDECLTYPE_USHORT2N` for UVs — same bit-cost as our stride
  arithmetic. **Strong hypothesis**: our UVs are 2×u16 BE just past
  the position, exactly like FM3.

---

## How FM3 PVS files compare to ours

FM3 PVS (`read_pvs.py`):

```
'FPVS' magic  (0x46505653, big-endian)
u32 version (24-27 supported, 27+ has +4 byte header tail)
skip 6
u32 zones_length
PVSZone[zones_length] (variable — has sub-arrays we skip)
u32 textures_length
PVSTexture[textures_length]:           ← THE TEXTURE TABLE
  u32 texture_file_name                ← the integer filename
  u32 index_in_stx_bin
  f32 u_scale, v_scale
  f32 u_translate, v_translate
  skip 4
u32 shaders_length
string[shaders_length] shaders         ← shader filename strings
u32 models_instances_length
PVSModelInstance[models_instances_length]:
  u16 model_index
  u32 flags
  u32 texture                          ← per-instance texture override
  skip 4
  f32 model_data
  if version >= 25: skip 20 else skip 8
  f32×3 translate
  f16×9 rotation                       ← row-major 3×3
u32 models_length
PVSModel[models_length]:
  u32 textures_length
  u32[textures_length] textures        ← indices into pvs.textures[]
  u32 shaders_length
  u32[shaders_length] shaders          ← indices into pvs.shaders[]
  skip 80
... sky_model + lone_models_instances + prefix
```

**Our FH1 PVS** (per `docs/pvs-format.md` and `pvs.py`): we already
parse the headers and the visibility/zone arrays. Memory says the
**tail** carries `model_instance_details` with translate+rotation
matching the FM3 `PVSModelInstance` shape — that's the breakthrough
that solved placements.

**What we have NOT parsed yet, but FM3 says is there:**
- The `textures[]` table (PVSTexture entries with the integer
  filenames + UV transforms)
- The `shaders[]` string table
- The `models[].textures[]` and `models[].shaders[]` index arrays

These three are the missing keys. Need to extend `pvs.py` to walk
past where we currently stop.

---

## What this gives us (and what's still unknown)

**Confidence level: HIGH for**
- CAFF reader port — formal spec, FH1 is the same version.
- Detile + DDS wrap — pure math, GPU-determined.
- BIX header parse — 28 bytes, we know the magic.
- UV bit-layout in vertex buffers — strong analogy, easy to verify.

**Confidence level: MEDIUM for**
- FH1 PVS texture table location — analogy says it's there, our
  current decoder stops short of it. May be in a different order or
  with new fields between us and FM3.
- FH1 RMB material set location — we know shader paths exist in the
  trailer; the material indices / sampler indices may be there too,
  or they may live in a sibling file we haven't identified.

**Confidence level: LOW for**
- FH1 shader (`.fxobj`?) availability and layout. If they're
  stripped or compiled-only, we work from stride heuristics.
- Per-instance texture overrides (`PVSModelInstance.texture` in FM3).
  Our pvsz parser may need to capture this when we do the texture
  pass.

**Off the table for now:**
- Lighting (spherical harmonics, lightmaps)
- Animated textures (the `texanim` field in CAFF rendergraph)
- Normal/tangent space reconstruction beyond what shaders need
- The full `shaders.py` per-shader recipe library — out of scope;
  generic Principled BSDF is enough for visual validation.

---

## Phase results (2026-05-03)

All seven phases of the original plan complete:

| # | task | status | result |
|---|---|---|---|
| 1 | Audit forza-x360-io | ✓ | spec doc + repo cloned |
| 2 | Decode RMB UVs | ✓ | 8159/16322 meshes carry UV0 (37M loops) |
| 3 | Identify material set | ✓ | 1588/1613 RMB trailers parse materials (98.5%) |
| 4 | CAFF v21.11 reader | ✓ | 13157/13187 hash-bins decode (99.8%) |
| 5 | BIX reader | ✓ | 5230/5234 BIX pairs decode (99.9%) |
| 6 | Material → texture binding | ✓ | per-section sampler→hash join in `pvs_inst.npz` |
| 7 | Wire textures into Blender | ✓ | 1317 unique images packed into colorado.blend, 12988 mesh slots use them |

**Tools built:**
- `src/fh1_mapdecomp/caff.py` — CAFF v21.11 reader, XG detile, DDS DX10 wrap
- `src/fh1_mapdecomp/bix.py` — BIX reader (shares deswizzle with caff)
- `src/fh1_mapdecomp/textures.py` — `TextureBank` for in-memory hash → DDS lookup
- `src/fh1_mapdecomp/rmb.py::_parse_material_set` — FH1 material-set decoder
- `RmbMaterial` dataclass + per-section attribution

**.npz schema additions (pvs_inst):**

    positions:    (V, 3) float32
    faces:        (F, 3) uint32
    uvs:          (V, 2) float32
    mat_per_face: (F,) uint16          ← which material each face uses
    mat_textures: (M, 8) int32         ← per material, 8 sampler hashes;
                                         positive = texture filename hash,
                                         -2 = unbound, -1 = instancer override

**Texture pipeline runtime (Blender side):**

The Blender import script (`blender_scripts/import_world.py`) now:
1. Reads `source_zip` from each pvs_inst index.json
2. Lazily constructs a `TextureBank` against bin.zip
3. For each unique texture hash referenced by any mesh, decodes the
   CAFF/BIX blob in memory, writes DDS to a temp file, loads via
   `bpy.data.images.load`, packs into the .blend, deletes the temp file
4. Builds one Principled BSDF material per unique texture-tuple key,
   assigns per-face material indices from `mat_per_face`

No PNG/DDS files written to disk — all textures live inside the .blend.
Blend file size: 257 MB (untextured) → 322 MB (UVs only) → 378 MB
(UVs + 1317 packed textures).

**Known limitations:**
- Only sampler 0 is wired to base color. Normal maps (BC5), specular,
  AO, light maps live at higher sampler slots — not yet hooked up.
- 30 cube maps (TEX_CUBE) skipped (skybox/reflection probes; rare).
- 4 BIX pairs error on size mismatch — edge case, ignored.
- The shader-recipe library (`shaders.py` from forza-x360-io with its
  per-shader-name node graphs) is NOT ported. We use a generic
  Principled BSDF for everything. Shader-accurate rendering is future
  work.

---

## Recommended port order

1. **Vertex stride parser (FH1 RMB)** — write a probe that reads the
   first 4 bytes past position for several known strides and sees
   whether they look like 2×u16 normalized UVs. Quick win.
2. **Extend our PVS parser** to read the texture table and string
   tables. Cross-check by counting unique `texture_file_name` values
   and comparing to the count of `_0x*.bin` files in bin.zip.
3. **Port CAFF reader** — start with `_0x00000001.bin` (smallest by
   index) and dump it as DDS. Verify width/height look sane for a
   game texture.
4. **Port BIX reader** — same drill.
5. **Find the FH1 material set in RMB** — grep our `RmbTrailer.raw`
   bytes for the `1.0,1.0,1.0,1.0` followed by 4×f32 pattern that
   FM3 has between the asserts and the UV xform.
6. **Wire to Blender** — copy `uv_util.py` + `texture_util.py` +
   `shaders_util.py` (the generic image-texture-node builder) and
   skip the per-shader recipe library.

Each step is independently shippable. Even step 1 alone (UVs into
the .npz with no textures) makes our exported meshes substantially
more useful for downstream work.

---

## Reference repo location

- Cloned: `/run/media/paths/SSS-Core/misc-repos/forza-x360-io`
- Upstream: <https://github.com/austinbaccus/Forza-X360-IO>
- Last commit at clone time: `789c464` (Combine Generate Textures
  buttons), 2026-05-03.

Files most-cited in this doc:
- `src/forza_blender/forza/textures/CAFF.bt` — formal CAFF spec
- `src/forza_blender/forza/textures/read_bin.py` — Python CAFF reader
- `src/forza_blender/forza/textures/read_bix.py` — Python BIX reader
- `src/forza_blender/forza/utils/deswizzle.py` — XG detile math
- `src/forza_blender/forza/models/read_rmbbin.py` — RMB material set
- `src/forza_blender/forza/models/forza_track_section.py` — vbuf+IB
- `src/forza_blender/forza/models/forza_track_subsection.py` — UV xform
- `src/forza_blender/forza/models/forza_vertex.py` — stride parser
- `src/forza_blender/forza/pvs/read_pvs.py` — PVS textures+shaders+models
- `src/forza_blender/forza/shaders/read_shader.py` — vertex declaration
- `src/forza_blender/forza/shaders/shaders_util.py` — Blender hook
