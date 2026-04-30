# State of FH1 Colorado extraction

**Read `world-architecture.md` first** for the chunk-streaming /
rmb-pool-as-flat-union framing — most of what's below makes sense
only against that backdrop.

For per-format internals see `format.md`, `rmb-bin.md`,
`rmb-subblobs.md`, `pgeo-body.md`. For mesh coverage and the
still-missing list see `mesh-export-state.md`. For the per-file
inventory see `parsed-inventory.md`.

---

## Pipeline state

| stage | source | output | status |
|---|---|---|---|
| terrain (hi) | rmb TERR pool | `out/terrain_hi/*.npz` | ✅ 2,569 LOD00 tiles |
| world-space rmbs | rmb header centroid + named-asset/phantom-wrapper rules | `out/rmb_world/blobs/*.npz` | ✅ ~33k blobs after centroid-aware filter, phantom-Box drop, stacked-dedup, cross-pool ownership dedup |
| template instances | v42k7 PGEO chunks | `out/v42k7_inst/blobs/*.npz` + `chunks/*` | ✅ ~50k instances after cull_box[0] gate, LOD-pair dedup, vs-rmb_world cross-source dedup |
| collision props | `Ribbon_00/CollObjs.xml` | `out/collobjs_inst/*.npz` | ✅ 7,480 props (freeroam) |
| Blender import | aggregator | `out/blender/colorado.blend` | ✅ renders |

Pipeline ordering: rmb_world runs **before** v42k7_inst so its
blob-tag set can be passed as `exclude_tag_keys` to the v42k7
extractor (see `world-architecture.md` §6).

---

## Major decoded formats

- **rmb.bin** (102,012 entries) — primary blob + sub-blobs (24,650
  unlocked); positions + sections + triangle strips. Vertex stride
  16/20/24/28/32/36 supported. Sections decode index buffers via
  `0xFFFF` strip restart. Sub-blob discriminator: `00 00 00 05 00 00 00 01`
  followed by a non-`02` third u32. See `rmb-bin.md` and `rmb-subblobs.md`.
- **rmb named-section refs** (118,526 across 59,042 entries) — section
  whose name field is a foreign asset tag (e.g.
  `MOUN_DAM_BLDG_MainDam_003`) instead of `Material__NN`. Provides a
  **labelling map** from anonymous wrapper rmbs (which are already
  world-placed by `rmb_world.py`) to authored asset names. NOT a
  separate placement source — the wrappers themselves carry the
  geometry and world centroid. See `mesh-export-state.md` § "Named-
  section refs". Probe: `probes/probe_rmb_named_section_refs.py`.
- **PGEO** (41,587 entries, 7 variants) — header + bbox + (version,
  kind) classifier. Bodies decoded for terrain, v42k7, v42k7 proc-
  subvariant. Undecoded body classes: grass (11.3k), crowd (8.5k),
  vegetation (3.3k), v44k5 (1.6k), landmark_anim (447, festival
  rides). See `pgeo-body.md`.
- **v42k7 instance records** — per-instance 64B chunk in
  `Models_/AModels_/pAModels_*` chunks; 96B position table; proc
  subvariant inline 40B vbuf (10:10:10:2 packed positions, 167
  placements / 26 chunks) + position-table fallback (1722 placements
  / 87 chunks via `keep_all_rotations=True`). See
  `v42k7-per-instance-transforms.md`, `v42k7-placement-bugs.md`,
  `rmb-subblobs.md §2`.
- **CollObjs.xml / GameObjs.xml** — explicit `<Pos>` + `<Orientation>`
  per object. CollObjs has 21,877 props; 65% match the rmb pool via
  fuzzy resolver. GameObjs has 2,148 entries (Barn Finds, race
  spawns, plane race waypoints — no landmark hero meshes).
- **`__R00Z*.pvsz`** — zone streaming manifest, format
  `[u32 count][count × u32 (0x80000000 | rmb_handle)]`.
- **`Colorado_00.hex`** — HEXY v0x65, 91×60 grid at 100 m, origin
  `(-6700, -6495.19)`, 1,434 zone↔cell map. Probe-level only.
- **`PVSZLookup_00.dat`** — 1,434 × `(zone_hash, zone_index)` pairs.
- **`PVSZoneSpeeds.dat`** — 1,434 f32 BE AI speed limits.
- **`.owt`** (in `aiopenworld.zip`) — OWTM v1, 48-byte records;
  46 routes, all positions in Colorado bounds. Decoded, not yet
  emitted to .blend.
- **`.fiz`** (in bin.zip, 16,934 entries) — `'fiz '` magic v1, world
  bbox + 10 m cell + 0.1 quant step. Per-zone foliage instance lists.
  Format identified, decoder not written.

---

## Known issues

- **v42k7 within-section selector unsolved.** Each v42k7 section
  carries 1–3 rmb pool handles in its slot list (e.g.
  `[Rail_LOD00, Rail_LOD01, Billboard]`). The engine picks ONE per
  section/instance based on a selector we haven't found. We currently
  emit ALL of them, so a billboard renders on top of the rail at
  every rail position. See `v42k7-section-mesh-selection.md` §5.2 for
  the full list of candidate bytes ruled out and not-yet-tested.
- **Origin-only landmarks** (Observatory, Amphitheatre, MiningTower,
  Gondola, MAINTOWN_BLDG_WarehouseOffices, MOUN_GM_HillSideMine)
  exist in the rmb pool only as authoring-local sources at origin.
  No world-positioned named-asset rmb. They must be rendered through
  some other transform mechanism (Object NNN wrapper bake, or xex
  code-side transform). 6 landmark families currently missing /
  mis-placed because of this. See
  `landmark-placement-investigation.md` § "Cases where the
  named-asset rmb does NOT exist at world position".
- **Stacked-copy redundancy** — chunk-streaming flat-union ships the
  same asset baked into N chunks. Stacked-dedup in `rmb_world.py`
  collapses by `(stripped_tag, rounded_centroid)`; chunk-AABB
  ownership (in progress) replaces the rounding heuristic with
  smallest-containing-AABB query.
- **Per-vertex attributes past position** are still raw bytes for
  every stride. Needed for shading + UV unwrap.
- **Section material-name strings** interleave in the index-stream
  region but aren't surfaced as data.
- **2 ANIM PGEOs** (`Showcase_Event_Helicopter_Race`,
  `Timed_Event_Biplane`) fail `lzxd_decompress rc=11` (multi-chunk
  LZX edge case). animatedobjects.zip is deferred so this is
  harmless.

---

## Currently out of scope

- Materials / textures (`.bix` 42k entries, `.bundle` 429,
  `.dds`/`.xds`)
- Lighting (`.sh` SH probes 4,768, lightmaps)
- Audio, UI, gameplay, cars, animation
- Drivable road surface as separate mesh (currently mixed into
  `terrain_hi`)
- LOD01/LOD02 + non-LOD00 TERR tiles (LOD00 covers visual scale)

---

## What's known about file types we haven't fully decoded

- **`.bix`** (42,657) — custom texture format with two variants
  (`BIX1` magic + width/height/mip header for some, raw block-
  encoded payload for most). Highest-leverage texture format.
- **`.bundle`** (429, hash-named `_0x10000xxx.bundle`) — magic
  `0x1A207F52` (= `BIX1`-related). Probable texture atlases
  (largest at ~4 MB dsize).
- **`.fiz`** (16,934) — confirmed foliage with world-bbox header,
  body decode pending.
- **`.sh`** (4,768) — confirmed SH probes, world-XZ-keyed filenames
  (`Colorado__shdata__n2550x_n1645z.sh`); per-probe stride ~175 B.
- **`.fxobj`** (173) — compiled shaders, out of scope for map
  export.
- **PGEO body classes**: grass (11.3k), crowd (8.5k), vegetation
  (3.3k), v44k5 (1.6k), landmark_anim (447). All hold per-chunk
  point/instance data we currently render as bbox-placeholder
  cubes.
