# PVS / PVSZ — FH1 placement format

`Ribbon_NN/<Track>_NN.pvs` (magic `FPVS`) is the engine's authored placement
table for one ribbon. It carries the model directory and the placement
slots; per-instance world transforms are streamed in via per-zone
`__R{ribbon:02d}Z{zone:05d}.pvsz` files (which live inside `bin.zip` for
retail FH1).

This format has been kept opaque in the FH community for years.
Doliman100 and austinbaccus reverse-engineered it for their FH1 fork of
the Forza-X360-IO Blender addon (`/run/media/paths/SSS-Core/misc-repos/Forza-X360-IO GPLv3/`).
Our `src/fh1_mapdecomp/pvs.py` is a Python port of that reader, trimmed
to the game_series=2 (Horizon) branch and verified against retail FH1
v50 — Colorado: **62,173 model_instances, 14,560 models, 1,434 streamed
zones, 59,599 instances hydrated (96.8 %)**.

The `pvs.bt` 010-template in the upstream repo describes a richer
in-game-data structure (cubemap probes, materials, lighting tables);
the production reader skips most of those and so do we. None of the
extra fields are needed for placement extraction.

## Versions

| Version | Game | Notes |
|--:|---|---|
| 45  | FH1 dev (June 2012 build) | Earliest decode-able variant. |
| 50  | FH1 retail (Xbox 360) | This implementation's primary target. |
| 51  | FH2 (Xbox 360)  | Differs in `routes_length` byte width. |

All FH versions are big-endian on Xbox 360.

## Master `.pvs` body — game_series=2

Pseudo-Python (see `src/fh1_mapdecomp/pvs.py` for the actual reader):

```text
header                 = u32 magic 'FPVS' + u32 version + u32 cpu_data_size + skip 4
ribbon_index           = u16
                         skip 5
tools_based            = u8 if version >= 47 else 0
                         skip 4
                         skip 4 if version >= 46
v48 unk                = u32 n + skip 4*n   if version >= 48
                         skip 1 + 2

zones_length           = u32
for _ in range(zones_length):
    _read_zone_body(...)            # same shape as a PVSZ file

textures_length        = u32
textures               = [_read_texture] * textures_length
shaders_length         = u32
shaders                = [u32-prefixed string] * shaders_length

models_instances_length = u32
models_instances       = [_read_model_instance(version)] * models_instances_length

models_length          = u32
models                 = [_read_model] * models_length

skip 3 length-prefixed arrays (post-models, sky-related in older builds)

lone_models_instances  = same as models_instances
prefix                 = u32-prefixed string         # e.g. 'coloradoout'

skip 2
objects_id_length      = u32; skip each as u32-prefixed string
zone_visibility_length = u32 (must be 0 for our variant)
skip 1
streamed_zones_length  = u32                          # = number of __R##Z#####.pvsz files
```

### `_read_zone_body` (used in BOTH master PVS zones AND `.pvsz` files)

```text
n_idx                  = u32
indexes                = [u32] * n_idx                # 0x80000000 bit may be set
if tools_based:
    skip 4 length-prefixed arrays (2/16/4/1 byte stride)
unk2                   = u32 n + skip 2*n
textures_references    = u32 n + skip 4*n
textures_use           = u32 n + skip n               # u8 list
extra_a                = u32 n + skip 16*n            # game_series=2 only
extra_b                = u32 n + skip n               # game_series=2 only
n_det                  = u32
details                = [_read_model_instance_details(version)] * n_det
```

In a master-PVS zone, `n_det` is always 0 — the master only declares the
slot list; transforms live in the per-zone `.pvsz` files.

### `_read_model_instance_details` (the placement transform)

```text
skip 6
translate              = (f32, f32, f32)              # world XYZ (Y-up)
rotation               = 3x3 of f16                   # column-major in source
material_data          = f32
skip 12
use_dynamic_3d_data    = u8
if use_dynamic_3d_data:
    skip 4
    routes_length      = u8
    skip 2*routes_length if version >= 51 else routes_length
    skip 32
```

The matrix the addon assembles is column-major; we transpose into a 4×4
row-major during read.

### `_read_model_instance` (placement slot, no transform)

```text
model_index            = u16                          # NB: u16 even for v50, per addon
parent_lod_offset      = s16 if version >= 49 else None
flags                  = u32
texture                = u32                          # ref into PVSTexture[]
skip 4
skip 2                                                 # game_series=2 trailer
```

Transform is left empty; filled in by the PVSZ pass.

### `_read_model` (mesh handle + material binding)

```text
n_textures = u32; textures = [u32] * n_textures        # ref into PVSTexture[]
n_shaders  = u32; shaders  = [u32] * n_shaders         # ref into shader_paths[]
skip 12*4 + 12                                         # game_series=2 trailer
```

### `_read_texture` (28 bytes, game_series=2)

```text
texture_file_name (u32)        # u32 hash matching _0xHHHHHHHH.bin or .stx index
index_in_stx_bin  (u32)
u_scale, v_scale, u_translate, v_translate (4× f32)
skip 4
```

The 010-template describes additional v41/v46 fields, but the production
addon doesn't read them and the layout works without them on FH1 v50.

## Model → mesh resolution

Each `model_index` resolves to a file in `bin.zip`:

    {prefix}.{model_index:05d}.rmb.bin

For Colorado, `prefix = "coloradoout"`. Of 14,560 models, 14,291 have a
matching `.rmb.bin` in our extract — 269 are referenced but absent
(probably streamed from a sibling archive or used cross-ribbon).

## Hydrating transforms

```python
from fh1_mapdecomp.pvs import parse_pvs, hydrate_transforms, make_binzip_pvsz_reader

pvs, tbzu = parse_pvs(pvs_path.read_bytes())
reader    = make_binzip_pvsz_reader(zip_path, pvs.ribbon_index)
hydrate_transforms(pvs, tools_based_zone_unioning=tbzu, pvsz_reader=reader)

for inst in pvs.models_instances:
    if inst.material_data is None:
        continue                                       # never referenced by a zone
    pos = (inst.transform[0][3], inst.transform[1][3], inst.transform[2][3])
    rot3x3 = [[inst.transform[r][c] for c in range(3)] for r in range(3)]
```

First-write-wins: an index that appears in multiple PVSZ files keeps the
first transform (matches the addon's `model_data is None` gate).

## Why this matters

Before this format was decoded, fh1-mapdecomp inferred placements from
runtime artifacts: `v42k7` PGEO chunks, the `cull_box[0] >= 80` selector,
the within-section RENDER mask, rmb_world wrapper-rmb labelling, and
LOD-pair leg-keep heuristics. Those were reverse-engineering a stream
that the engine builds at runtime FROM the PVS data described above.

PVS gives us 54,429 placed instances directly with no heuristic
filtering. The `v42k7_inst`, `rmb_world`, and `collobjs_inst` pipelines
remain useful for the assets PVS doesn't enumerate (some streamed
decorative classes, race-event dressing, etc.) — but PVS is now the
primary placement source.
