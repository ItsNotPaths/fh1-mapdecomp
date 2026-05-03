# Handoff — decode the remaining PGEO body classes

## Mission

Decode the per-variant PGEO body layouts that we haven't cracked yet, so
the Blender export gets crowd, foliage, and the missing festival
metal barriers. Start with **`crowd`** (suspected host of the festival
metal stanchion barriers, since those are crowd-control props), then
**`grass`** (the engine's `CProceduralVegetation` class — actual
foliage scatter), then everything else.

> **Naming gotcha:** the PGEO variant historically called `vegetation`
> (`(43, 6)`) is the engine's `CProceduralLightGlows` class — light
> entity data, not foliage. It has been renamed `light_glows`. The
> engine's `CProceduralVegetation` class is what we label `grass`. See
> `docs/pgeo-body.md §0` for the full variant ↔ engine class table.

When done: one `_extract_*_instances` extractor per variant emitting
the standard `index.json + blobs/*.npz` schema, wired into `cmd_all`
the same way `pvs_inst` and `collobjs_inst` are.

## Why this matters now

The previous session got the placement extraction to a stable point:

- **`pvs.py` + `pvs_inst.py`** parse `Ribbon_NN/<Track>_NN.pvs` and
  the in-bin.zip `__R##Z#####.pvsz` files. ~32k instances at correct
  world transforms (with a Z-negate on PVS pos and a Y-flip mirror to
  match the user's mental model).
- **`collobjs_inst.py`** parses `Ribbon_00/CollObjs.xml` for
  static-collision-prop placements that PVS leaves at pos=(0,0,0).
- **`terrain_hi.py`** rebuilds the rmb-pool TERR meshes.

After that work the Blender scene has buildings, signs, road meshes,
festival tents, paint shops, smelter, dam — but no people, no grass,
no foliage, and **no festival metal stanchion barriers** (the
crowd-control fences around the festival circle). The user verified
those barriers are NOT in PVS, NOT in CollObjs, NOT in any
`FEST_Grp_*` composite, NOT in any `Festival_Area5/6_*` terrain patch.

Process of elimination — they probably live in **`crowd` PGEO chunks**,
the same body class that holds spectator placements. The variant name
"crowd" was inferred from spectator-ish patterns; the body is
undecoded so we can't actually confirm without writing the parser.

## PGEO variant inventory (Colorado bin.zip)

Run `probes/probe_bodies.py` to regenerate. Last counts:

| variant         | count | sample size | sample bbox extents         | decoded? |
|-----------------|------:|------------:|-----------------------------|---------:|
| `v42k7`         | 14122 |       4268B | 230×72×186                  | ✅ |
| `grass`         | 11292 |       3100B | 14×5×21                     | ❌ |
| `crowd`         |  8544 |        296B | 6×8×17                      | ❌ ← start here |
| `light_glows`   |  3316 |        496B | 19×11×19                    | 🟡 (body shape decoded — see pgeo-body.md §3.5; not yet wired) |
| `terrain`       |  2288 |       5992B | 128×200000×128 (Y is sentinel) | ✅ |
| `v44k5`         |  1578 |       1152B | 640×38×214                  | ❌ |
| `landmark_anim` |   447 |     270172B | 47×52×12                    | ❌ (see notes) |

`crowd`, `light_glows`, `grass` all have small bbox extents (≤ 21m
per axis) and small uncompressed sizes, suggesting per-cell scatter
with a few-dozen instance positions each. `v44k5` has larger
horizontal bbox but small Y, suggesting a streaming-roads-style
record. `landmark_anim` is much bigger and embeds `Anim_ANIM_*` rig
identifiers (see memory entry).

Variant classifier lives in `src/fh1_mapdecomp/pgeo.py:51` —
`(version, kind)` → name. Adding new variants only needs that table
update plus a body decoder under `src/fh1_mapdecomp/pgeo_body/`.

## What's already decoded (use as templates)

- **`src/fh1_mapdecomp/pgeo_body/terrain.py`** (73 lines) — simplest
  reference. Decodes terrain heightfield tiles. Read it first to see
  the expected shape of a body decoder.
- **`src/fh1_mapdecomp/pgeo_body/v42k7.py`** (706 lines) — the
  ambitious reverse-engineered one. Has section parsing, position
  tables, RENDER mask logic, proc-inline subvariant. Read selectively;
  most of its hard-won heuristics (cull_box[0], LOD-pair-leg-keep,
  RENDER mask) became less critical once PVS-based placement
  superseded v42k7-derived placement.
- **`src/fh1_mapdecomp/pgeo_body/types.py`** — the `MeshData`
  dataclass + `save`/`load`. Every new decoder returns one of these.

## Existing format work to reuse

- **`docs/pgeo-body.md`** — Phase-B probe notes covering all 7
  variants. Documents the **shared 0x34..0x5b prolog** and the
  **"0x14 invariant"** (`u32_BE(file[0x48]) == len(file)`) — your
  parser's first sanity check. Per-variant subsections start there;
  add to them as you decode.
- **`docs/format.md`**, **`docs/world-architecture.md`** — broader
  context on bin.zip / rmb / PVS layout.
- **`docs/parsed-inventory.md`** — current decoded-vs-not inventory
  including the PGEO variants. Update as you go.

## Existing tooling

- **`probes/probe_bodies.py`** — dumps `(filename → variant, header,
  body summary)` for every PGEO. Re-run to refresh
  `out/probe/<variant>/*.txt`.
- **`probes/probe_large.py`** — extracts one mid-sized sample per
  variant for deeper inspection.
- **`scripts/scout_undecoded.py`** — scans bin.zip for files we don't
  decode and reports counts.
- **`scripts/decode_chunk_vbuf.py`** + **`scripts/vbuf_to_blender.py`**
  + **`scripts/test_vbuf_is_mesh.py`** + **`scripts/vbuf_topology_gallery.py`**
  — the v42k7 vertex-buffer reverse-engineering toolkit. Reusable
  for guessing vertex-buffer layouts in `crowd` / `light_glows` /
  `grass` bodies. Tried 7 topologies on v42k7 chunk vbufs — all noise
  (chunk vbufs aren't meshes); see `project_investigation_closer_2026-04-28`.

## Crowd quick-look (2026-05-03)

End-of-session probe on 5 crowd PGEOs found:

- 0x48 size invariant holds.
- `n_major` at body+0x08 is the instance count (samples: 41, 41, 41, 55, 43).
- Body contains an **ASCII descriptor string** before the position
  array — exactly the same pattern as v42k7's `proc_inline`
  subvariant. Examples:
  - `crowd_proc_clrd_festival` + `crowd_stages_103`
  - `crowd_proc_clrd_festival` + `crowd_stages_117`
  - `crowd_proc_clrd_festival` + `crowd_secampsite_5`
  - `FESTIVAL_SECOND_15` (different shape)
- World-coord f32 positions follow the descriptor (verified one
  sample: `c4 a6 22 3d` = `-1329.07`, matches bbox X).

This means **`v42k7.parse_proc_inline_positions` is most of the
decoder you need for crowd**. Open
`src/fh1_mapdecomp/pgeo_body/v42k7.py`, look at `parse_proc_inline_positions`
and `_proc_descriptor_class` (the latter classifies descriptors like
`models_proc_clrd_trees_*` → tree-mesh placeholder). Crowd needs an
analogous descriptor classifier:

| descriptor prefix                  | placeholder mesh class   |
|------------------------------------|--------------------------|
| `crowd_stages_*`                   | festival-stage spectator |
| `crowd_secampsite_*`               | campsite spectator       |
| `crowd_proc_clrd_festival_*` (other) | generic festival crowd |
| `FESTIVAL_SECOND_*`                | second-stage spectator   |

Until the actual spectator mesh is identified, emit each instance as
a 1m-tall locator quad per position (same trick v42k7's proc-inline
uses with `_proc_synth_mesh`). User can swap in a real spectator
rmb later. Whether the festival metal stanchion barriers live in a
specific descriptor (likely `crowd_secampsite_*` or similar) is the
verification step.

## Suggested concrete approach for `crowd`

1. **Pick anchor samples.** Pull 3 crowd PGEOs from very different
   spatial regions: one near the festival hub (Forza X≈-1100, Z≈200),
   one along a race track, one at the smelter. Use bbox center from
   `pgeo.parse_header` to filter. Save raw bytes via
   `binzip.read_entry`.
2. **Verify the 0x48 size invariant** on each. Confirms our header
   parse is right for this variant.
3. **Diff the samples.** Look for shared header fields (numbers that
   match across all three) vs per-instance fields (counts, position
   arrays). Document in `docs/pgeo-body.md` under the `crowd` section.
4. **Find the position table.** Crowd is people-placement; expect a
   contiguous run of 3×f32 BE world coords (or quantized 3×s16
   relative to chunk origin). Sample bbox is tiny (~6×8×17m), so
   coords should cluster within that. Compare to v42k7's
   `parse_position_table` (offset 0x80-ish, stride 96 bytes — but
   crowd will likely be much simpler since it's a single-asset
   per-chunk record vs v42k7's section-list).
5. **Identify the asset reference.** Crowd PGEOs probably reference
   a single rmb pool handle (the spectator/barrier rmb to render at
   each position). Look for a u32 that resolves to a sensible
   `coloradoout.NNNNN.rmb.bin` index. Or possibly a content-hash
   matching a `_0xHHHHHHHH.bin` texture/asset.
6. **Cross-check with the rmb pool.** Once you have a handle,
   `parse_blob` it and verify the tag is something spectator-like
   (`Spectator_*`, `Crowd_*`, `Person_*`) or barrier-like
   (`Barrier_*`, `Crowd_Control_*`, `OBJ_FEST_BarrierMetal_*`).
7. **Emit a `MeshData`-style extractor.** `extract_crowd_instances`
   under `src/fh1_mapdecomp/crowd_inst.py` (or alongside `pgeo_body/`
   — pick the convention you want, both `pvs_inst.py` and
   `collobjs_inst.py` live at the package root and that pattern
   works). Schema: identical to `pvs_inst` (one section per handle
   with all its instance positions/rotations).
8. **Wire into `cmd_all`** alongside `cmd_pvs_inst` and
   `cmd_collobjs_inst`. The `import_world.py` Blender helper already
   has a generic `_import_inst_dir` — reuse it.

After `crowd`, the same pattern applies to `grass` (decoded
2026-05-03 — engine class `CProceduralVegetation`; per-blade
positions decoded into 1.05M placements (287k unique); covers
roadside/Festival/Plains scatter only — trees live in the `v42k7`
proc-inline subvariant `models_proc_clrd_trees_*`, see §3.2.2 —
ships a synthetic bright-yellow cylinder placeholder until the
external per-template→rmb-handle table is found),
`light_glows` (point-cloud of light entities; no mesh — render as
empties or as a GN-instanced beacon glyph), and `landmark_anim`
(rigs with embedded `.max` paths — see memory entry, the rig names
are already extracted to `probes/out/landmark_anim_names.tsv`).

**Open foliage gap:** standalone trees / shrubs / hillside foliage
are NOT covered by the `grass` extractor. The `v42k7` proc-inline
subvariant carries them: `pgeo_body/v42k7.parse_proc_inline_positions`
already decodes the per-tree positions, but no extractor wires them
into `out/<variant>_inst/`. Mirror `grass_inst.py` to ship those
placements next.

`v44k5` is lower priority: it shows up in 1578 chunks but the
horizontal bbox is much larger (640×214m), suggesting a streaming
roads variant. Likely overlaps with what PVS already places. Do this
last.

## Output schema (match the existing pipelines)

For each new extractor, emit `out/<variant>_inst/index.json` with:

```json
{
  "source_zip": "...",
  "policy": "freeroam",
  "blob_count": N,
  "chunk_count": 1,
  "chunks": [{
    "filename": "<source>.pgeo (or aggregated)",
    "name": "Crowd",
    "origin": [0, 0, 0],
    "sections": [{
      "handles": [mi],
      "instances": [{"pos": [x, y, z], "rot": [[..],[..],[..]]}, ...]
    }]
  }],
  "blobs": [{
    "handle": mi, "npz": "blobs/m{mi:05d}.npz",
    "tag": "...", "vcount": N, "tri_count": M, "sub_blobs": K
  }]
}
```

Then `import_world.py` adds `--<variant>-inst <dir>` and calls
`_import_inst_dir(...)` with new `label` + `collection` strings.
The basis swap, GN-instance node group, and per-instance euler
conversion are all already in place.

## Coordinate convention (don't reinvent)

The previous session settled on:

- Vertex / source-mesh basis: `(x, y, z)_forza → (x, z, y)_blender`
- PVS / placement-position basis: `(x, y, z)_forza → (x, -z, y)_blender`
  (engine quirk — PVS pos.Z is sign-flipped relative to vertex Z)
- Whole scene then mirrored across Blender X axis (negate Blender Y)
  to match the user's reference render
- Rotations: `B_VERT @ R @ B_VERT^T` (conjugation), then **negate
  the Z euler component** to undo the yaw flip the X-axis mirror
  introduces

`make_v42k7_inst_mesh_data` and `_import_inst_dir` in
`src/fh1_mapdecomp/blender_scripts/import_world.py` already
implement this. Whatever new extractors you write should emit raw
**Forza Y-up world / local coords** in the npz; the Blender importer
applies the basis. Don't try to pre-bake the swap.

## Gotchas the previous session learned the hard way

- World-authored vs local-authored rmbs need different handling. PVS
  hands you a translation; for local-authored rmbs that's the world
  position, for world-authored rmbs (centroid > 100m from origin)
  the vertices already encode world coords and PVS pos is (0,0,0).
  See `pvs_inst._is_world_authored`. Crowd PGEOs likely all act as
  one or the other consistently — figure out which.
- Race-event ribbons ship byte-identical copies of festival props
  under different model_indexes. Mesh-hash dedup in `pvs_inst`
  collapses those onto a canonical mi while merging instance
  positions. Reuse the same trick if crowd shows similar patterns.
- The mesh-hash dedup is `hash(positions.tobytes()) ^ hash(tris.tobytes())`.
  For crowd's instances-without-mesh case (positions only), use a
  position-tuple set instead of byte hash.
- Per-family LOD floor (`pvs_inst._strip_lod_suffix` + `family_min_lod`
  pre-pass) is the right pattern when same-asset siblings exist at
  multiple LODs. Reuse if applicable.

## Files to read first (priority order)

1. `docs/pgeo-body.md` — current Phase-B notes
2. `src/fh1_mapdecomp/pgeo_body/terrain.py` — minimal decoder template
3. `src/fh1_mapdecomp/pgeo_body/types.py` — `MeshData` schema
4. `src/fh1_mapdecomp/pgeo_body/v42k7.py` — the ambitious one (skim,
   don't read end-to-end)
5. `src/fh1_mapdecomp/pgeo.py` — variant classifier
6. `src/fh1_mapdecomp/pvs_inst.py` — extractor pattern to mirror
7. `src/fh1_mapdecomp/collobjs_inst.py` — second extractor pattern
8. `src/fh1_mapdecomp/blender_scripts/import_world.py` —
   `_import_inst_dir`, basis convention

## Memory entries worth checking

`MEMORY.md` is loaded automatically. Look at:

- `project_pvs_solves_placements_2026-05-03` — full PVS integration
  trail
- `project_landmark_anim_max_paths_2026-04-29` — landmark_anim has
  embedded `.max` paths; rig names already extracted
- `project_investigation_closer_2026-04-28` — earlier RE work that
  ruled out chunk VBUF as direct mesh source
- `project_world_chunk_streaming` — chunk pool / streaming model

## Definition of done

The Blender scene has crowd / spectator meshes visible at the
festival hub and along race routes. The festival metal stanchion
barriers (the user's specific gap) are present where they should be.
Foliage scatter (`grass` variant) visible across the world. Light
glows optionally exposed as Blender lights / empties. New
extractors documented in `docs/parsed-inventory.md` with
status promoted from 🟡 to ✅.
