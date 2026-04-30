# FH1 Colorado mesh export — state of the world

Authoritative snapshot of geometry coverage. Textures/materials are
out of scope here (see §"Out of scope").

For format internals see `format.md`, `rmb-bin.md`, `rmb-subblobs.md`,
`pgeo-body.md`. For the full file inventory see `parsed-inventory.md`.

---

## TL;DR

The geometry placement story is fully solved on disk. Five
mechanisms cover virtually every authored mesh in Colorado, and they
are all data-derived (no hand placement, no estimator-only fallbacks
for the major landmarks):

| layer | source | placement | extractor |
|---|---|---|---|
| terrain | rmb TERR pool | header centroid (world) | `rmb.py` + `terrain_hi.py` |
| world-placed rmbs (~70% of pool, includes most landmarks) | rmb header centroid `\|XZ\|>50m` | header centroid (world) | `rmb_world.py` |
| template instances (modular buildings, road segments, fences) | v42k7 PGEO chunks | per-instance 64B records | `v42k7_inst.py` |
| collision props (signs, barriers, smashables) | `Ribbon_00/CollObjs.xml` | explicit `<Pos>` + `<Orientation>` | `collobjs.py` |
| **named-section refs (labelling)** | sections embedded in wrapper rmbs naming a foreign asset tag | parent centroid (already-placed wrapper) + ref translation (cosmetic offset) | parsed (probe), not yet wired |

Important reframing (2026-04-27 second pass): the named-section-ref
discovery was a **labelling** discovery, not a placement discovery.
Every dam/REDROCKS/etc. wrapper rmb is already placed correctly by
`rmb_world.py` under an anonymous tag like `Object010_LOD00`. The
named-section refs let us re-label those anonymous wrappers with
human-meaningful names like `MOUN_DAM_BLDG_MainDam_*`.

---

## Currently shipping (in `out/blender/colorado.blend`)

| layer | count | accuracy |
|---|---:|---|
| terrain LOD00 tiles | 2,569 (5.06M verts) | exact heightmap |
| world-placed meshes (`rmb_world` freeroam policy) | 24,170 blobs | exact (header centroid). Includes the dam, REDROCKS, FOOTHILLS lodges, etc. — under anonymous wrapper names |
| template instances (`v42k7_inst` freeroam policy) | 20,934 instances across 1,138 chunks | per-instance transform |
| collision-prop instances | 7,480 props | exact (CollObjs.xml) |
| landmark estimates (HEXY+pvsz centroid fallback) | 292 placeholder positions | works for symmetric assets, drifts ~2 km on tall occluders. Will be retired once the labelling pass replaces them |

Loaded via `fh1-mapdecomp blender` / `fh1-mapdecomp all`.

---

## Named-section refs — the labelling source

`rmb.bin` section grammar permits arbitrary section names. ~58% of
pool entries (59,042 / 102,012) carry sections whose name field is a
foreign asset tag (e.g. `MOUN_DAM_BLDG_MainDam_003`) followed by a
short transform record.

Per-ref encoding (~80 bytes):

    [u16 name_len]
    [ascii name]
    [NUL pad to 4-align]
    [u32 = 6]                        version marker (matches rmb header version)
    [u32]                            varies — likely flags
    [u32 = 1]                        varies
    [12 bytes zero]
    [4 × f32]                        scale row (1,1,1,1 in all observed)
    [4 × f32 = (Tx, Ty, Tz, w)]      cosmetic offset within parent (small;
                                     typically a few meters or zero)

Pool-wide: 118,526 refs, 1,022 unique referenced names.

### What the wrapper actually contains

Verified on `Object010_LOD00` (h=5319):

- Wrapper rmb has a WORLD centroid `(6483, 234, 620)` — it's already in
  `rmb_world` output as a 9×16×9 m placed blob.
- Its vbuf is **already in world coordinates** (positions span
  6478-6488 X, 225-241 Y, 615-625 Z), NOT local-space.
- Its sections are named `MOUN_DAM_BLDG_MainDam_003` and
  `MOUN_DAM_BLDG_MainDam_004` instead of the usual `Material__NN`.
  The triangles in those sections are real geometry indexing into the
  wrapper's vbuf.

So the wrapper rmb IS the dam-piece geometry. The named-section ref
just identifies which authored asset that piece represents. The
"translation" field in the ref is a small cosmetic offset (sub-meter
to tens of meters) — not a primary placement.

### Why this matters

Before this discovery the export shipped `Object010_LOD00.npz` for
the dam main piece. The user couldn't tell that `Object010_LOD00`
*was* the dam; the labels were opaque.

After: parse the named-section refs once, build a `wrapper_handle →
authored_asset_name` map, rename the .npz outputs accordingly. ~28
of the 37 "missing landmark" families resolve to existing wrappers
once labelled.

Probe: `probes/probe_rmb_named_section_refs.py`. Output:
`probes/out/rmb_named_section_refs.json`.

### Wrapper map for the dam complex (illustrative)

| wrapper handle | tag | world position | named contents |
|---|---|---|---|
| 5319 | `Object010_LOD00` | (6483, 234, 620) | `MainDam_003`, `_004` |
| 5312 | `Object001_LOD00` | (6568, 220, 544) | `Lower_01`, `_04` |
| 8447 | `Dam_Platform_See concepts001_LOD00` | (6476, 240, 499) | `Lower_03`, `_ALPHA`, `VisitorCentre_03` |
| 8448 | `MOUN_DAM_BLDG_ViewPlatform_LOD00` | (6494, 227, 656) | `Lower_02`, `ViewPlatform_002/3/4` |
| 5286 | `MOUN_DAM_RoadWall1_LOD00` | (6001, 284, 943) | `RoadWall` |
| 5349 | `MOUN_DAM_BLDG_InletTower_LOD00` | (108, -14, 35) | `InletTower_001..5` |
| 8167 | `MOUN_DAM_Bridge_LOD00` | (69, 33, -5) | `Bridge_*` |

All seven are already placed. The labelling pass turns this from "9
anonymous wrappers" into "9 named dam-component pieces in the
outliner."

### Local-authored detail rmbs vs wrapper rmbs

For each landmark there are TWO geometry sources:

1. **The wrapper rmb** (above table). Lower vertex count, world-
   placed, the version that renders in-game.
2. **The local-authored detail rmb** (e.g. `MOUN_DAM_BLDG_MainDam_LOD00`
   h=8468, vc=8861, local centroid). Higher vertex count, in
   asset-local space, no direct placement record.

Hypothesis (worth confirming visually): the wrapper is the
shipping/streamed mesh; the local-authored detail rmb is the
high-detail authoring source, used for static/cinematic shots or as
the LOD0 source from which the wrapper was simplified. If you want
maximum geometry detail, render the local-authored mesh at the
wrapper's world centroid.

---

## Extraction ceiling (theoretical, with everything wired)

For pure mesh export (no textures), the data on disk supports:

- **100%** of terrain
- **~70%** of pool by header centroid (already shipping)
- **~21k** template-building instances (already shipping)
- **~9k** collision-prop instances (already shipping)
- **~1.8k** procedural placements (trees/barriers/boulders) via
  v42k7 proc-subvariant inline 40B vbufs (already in pipeline as
  placeholder synthetic blobs)
- Labelling for **~28-30** landmark families (named-section-refs
  pass — pending wiring)
- **46** AI road centerlines via `.owt` (decoded, not yet emitted)
- **16,934** per-zone foliage instance lists via `.fiz` (format
  identified, decoder not written)

That covers virtually every authored mesh in the scene. Per-vertex
attributes (normals/UVs/colours) and material assignment are the
remaining geometry-side gaps but don't block placement.

---

## Models we have NOT located

A short, sharp list. Each entry has been checked against:

1. World-baked sibling under any name (bbox-fingerprint match
   across 102k pool — negative)
2. Named-section ref pointing at it from any parent (full bin.zip
   decompressed scan — negative)
3. Self-referenced in own LOD wrapper that's then world-placed
   (recursive walk — pending wiring; would resolve ~13 more)

The genuinely-stuck remaining set:

| family | likely status | notes |
|---|---|---|
| `MAINTOWN_Warehouse1`, `_Warehouse2`, `_Warehouse2_Grunge`, `_BLDG_WarehouseOffices` | **probably authoring leftovers** | `MT_Warehouse_Ramp_LOD0?` ×50 placed at (-4479, -8.5, -1479) is the maintown warehouse infrastructure that DOES render. The named warehouse meshes likely got baked into per-instance world rmbs under different names. Verify by visually checking `MT_Warehouse_Ramp` instances in Blender. |
| `BLDG_MainTown_Warehouse04_grunge`, `BLDG_Maintown_Warehouse03_Grundge` | same as above | same maintown industrial story |
| `AuctionShowTent` | **real gap** | Festival hub area is well-covered (`Anim_ANIM_FRGND_*`, `_Autoshow_*`, `_MainStage_*`) but the static auction tent itself has no wrapper ref. Most credible candidate for being inside a `landmark_anim` PGEO body (variant 44/4, undecoded, 447 entries — the festival rigs). |
| `MOUN_DAM_BLDG_LowerB`, `MOUN_DAM_BLDG_Bridge` | **probably stuck names** | Dam complex is comprehensively wrapped (9+ wrappers above). LowerB and Bridge variants may be referenced under `Lower_05/06` or similar suffixes I didn't search; or they are authoring chunks that never got named-section-ref'd. World position is bounded by the existing dam wrappers. |
| `CO_CLRD_FinleyDam`, `_Left`, `_PowerStation_Left` | **probably authoring placeholders** | 4-vertex 1.5×0.3 m bbox — that's a marker, not a building. `MOUN_DAM_BLDG_MainDam` is the actual dam. |
| `Mesh008_LOD00/01/02` | **probably dead** | Generic blockout-style name (`Mesh008` is what you get from `Mesh.008` in 3ds Max). |
| `MiningTower01`, `MiningTower02` | **probably superseded** | `MiningTower_d01_*` (`_d01_` = "designer revision 01") is placed at (-538, 100, 3522). The non-`_d` versions are likely older revisions left in the pool. |
| `maintownBridge`, `northmaintownBridge` (LOD00 local-centroid) | **probably authoring source** | The bridge family has 396 pool entries; 108 are world-placed via duplicates. The 4 LOD00 prototypes with local centroid likely never instantiate. |
| `FOOTHILLS_BLDG_SteelBridge` | same as bridges above | one of two pool entries, both local |

That collapses to ~2-3 actually-credible gaps (AuctionShowTent +
maybe LowerB/Bridge), not 37. The rest are authoring leftovers
correctly absent from the live scene.

### Verification next steps

- Render `MT_Warehouse_Ramp_LOD00` instances in Blender and confirm
  they're warehouse buildings — closes the warehouse gap.
- Decode `landmark_anim` PGEO bodies (variant 44/4, 447 entries) —
  most likely candidate for AuctionShowTent and the festival hero
  meshes generally.
- Re-grep named-section-refs JSON with `Lower_05`, `Lower_06`,
  `Bridge_NN` etc. patterns to catch LowerB variants under
  numbered suffixes.

### Sources definitively ruled out (do not re-investigate)

- `default.xex` `.text`/`.data` (immediate-load scan, u32-handle
  scan, matrix scan, name-string scan, hash-search all negative)
- `.col` (directory only; pool is virtual; not reconstructible from
  any bin.zip subset)
- `colorado.nav` (road graph; not landmarks)
- `Colorado_00.pvs`, `__R00Z*.pvsz`, `PVSZLookup_00.dat` (visibility
  manifests, no positions)
- `FilenameMap_00.dat` (filename index, no landmark tags)
- `colorado.owr`, `.oww`, `.crowd` (12-32 byte stubs)
- `co_cone.bin` (CAFF compiled GPU resource)
- `PhysicsDefinitions.bin` (vehicle physics tunables)
- `gamedb.slt` SQLite (UI metadata only)
- `ParticleEmitters.xml`, `PostProcessingZones_Safe.xml` (FX, not anchors)
- `GameObjs.xml` (Barn Finds, race spawns, planes — no hero meshes)
- `TrackRoute*.xml`, `PreRaceCams.xml`, `TrackSegCams.xml` (race props/cameras)
- `animatedobjects.zip` (rig assets, no placement field)
- bin.zip subset reconstructions of the .col 105 MB virtual pool
  (no hex-named, .bundle, or other extension subset matches)

See `landmark-placement-investigation.md` for the negative-result
appendix.

---

## Out of scope (this doc)

- Materials / textures (`.bix`, `.bundle`, `.dds`, `.xds`)
- Lighting (`.sh` spherical-harmonic probes, lightmaps)
- Per-vertex attributes past position (normals/UVs/colours/tangent)
- Animation / dynamics
- Audio, UI, gameplay
