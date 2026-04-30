# Landmark placement — corrected understanding

> **Read `world-architecture.md` first.** This doc is now a
> per-landmark / per-bug log; the architectural framing lives in
> world-architecture.md.

## Current answer

Real landmark placements come from **named-asset rmb pool entries at
world position** — `MT_Warehouse_Ramp_LOD00`, `MAINTOWN_BLDG_MMSCarPark_Modules_LOD00`,
`PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00`, `MOUN_DAM_BLDG_ViewPlatform_LOD00`,
`OBJ_RedRock_Area03_StoneWall2_LOD00_05`, etc. Their pool centroid is
the asset's world centroid; their vbuf is in world coords. Verified
against user-supplied truth coords across 9 Colorado landmarks
(see `placements.txt`):

| landmark | named-asset wrapper | distance from truth |
|---|---|---|
| Warehouse | `MT_Warehouse_Ramp_LOD00` at (-4479, -1479) | 75 m |
| City parking complex | `MAINTOWN_BLDG_MMSCarPark_Modules_LOD00` at (-3917, -883) | 177 m |
| Smelter | `PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00` at (-1958, -2616) | 23 m |
| Dam ViewPlatform | `MOUN_DAM_BLDG_ViewPlatform_LOD00` at (6494, 656) | 371 m (off-base, plausible) |
| Red Rocks amphitheatre wall | `OBJ_RedRock_Area03_StoneWall2_LOD00_05` at (-5692, 5276) | 33 m |

## Updates to the older "solved" framing

The 2026-04-27 conclusion said "landmarks were already being placed
correctly under anonymous wrapper-rmb tags like `Object010_LOD00`,"
and that the named-section-refs labelling work was the missing piece.
Subsequent ground-truth checking against `placements.txt` showed this
was *partially* right and *partially* wrong:

- **Correct**: anonymous wrappers DO carry foreign-named sections
  baked in world coords, and the labelling pass is the right way to
  surface them in the .blend outliner.
- **Wrong**: the wrapper is NOT always at the asset's real position.
  Many `Object NNN_LOD\d+` and almost all `Box NNN_LOD\d+` wrappers
  sit at phantom positions thousands of meters from the asset they
  bake. They are streaming-visibility / occluder buffers, not
  primary placements. See `world-architecture.md` §2.2 for the four
  asset categories.

The fix path (see `world-architecture.md` §4 for full algorithm):

1. The `_DETAIL_AUTHORING_SOURCES` filter must be **centroid-aware**
   — drop only when centroid is < 100 m from origin (the asset-local
   case). Tag-only matching previously also killed world-positioned
   variants of the same tag (verified: `MOUN_DAM_BLDG_InletTower`
   has 11 world-positioned entries near the dam,
   `MOUN_DAM_BLDG_ViewPlatform` 6, `MOUN_DAM_RoadWall1` 2 — all real
   placements that were being wrongly suppressed).
2. **Drop `Box NNN_LOD\d+` wrappers entirely.** They consistently
   emit at positions thousands of meters from any landmark's
   user-truth coord across smelter / dam / bunker / amphitheatre.
   Engine treats these as occluder / shadow proxies, not render
   targets.
3. **Cross-pool ownership dedup for foreign-section emissions** in
   anonymous wrappers (Object NNN, BUILDINGS NNN, GLOB_*, Hub NNN):
   keep the wrapper only when its centroid is within 500 m of at
   least one of its baked sections' real-world named-asset position.
   Phantom wrappers like `Object104_LOD00` at (5946, 969) baking
   smelter sections that really live at (-1958, -2616) get dropped
   correctly.
4. **Stacked-copy dedup**: collapse N rmb pool entries with the
   same `(stripped_tag, rounded_centroid)` to 1. Required to undo
   the per-chunk duplication baked by the exporter (see
   `world-architecture.md` §3).
5. **Chunk-AABB ownership** (in progress, see §4 of architecture
   doc): replace the 500 m radius heuristic with a proper
   smallest-containing-AABB query so the dedup is structural, not
   numeric.

## Per-landmark ground truth (from docs/placements.txt)

User-supplied Blender coords; convert to game space via
`(bx, by, bz) → game (bx, bz, -by)`.

| landmark | game (X, Z) | radius | notes |
|---|---|---|---|
| Smelter (real green) | (-1961, -2593) | ~50 m | one correct emission; 8 phantom blue copies SW are duplicates |
| Dam base | (6627, 310) | varies | landmark complex, wider than 200 m |
| Golf course | (-2284, 2105) and (-1619, 3167) | ~200 m | two ends of the course |
| Bunker / mining | (4438, -1099) | ~150 m | old mining building en route to dam |
| Red Rocks entrance | (-5663, 5292) | ~250 m | festival outpost |
| Observatory | (-5403, 5446) | ~150 m | |
| Amphitheatre | (-5871, 5589) | ~200 m | iirc; carpark hub nearby |
| City centre fountain | (-4088, -836) | ~300 m | precise vertex; carpark complex correctly placing |
| Warehouse side of town | (-4488, -1404) | ~200 m | |

## Cases where the named-asset rmb does NOT exist at world position

The audit (probe over rmb pool, group by stripped tag, count
world-vs-origin entries) shows three categories:

| category | examples | fix |
|---|---|---|
| **All world** (real placements exist) | `MT_Warehouse_Ramp` (50 world / 0 origin), `MAINTOWN_BLDG_MMSCarPark_Modules` (40 / 0), `PLA_SMELTER_BLDG_BlastFurnaceLow` (45 / 0) | drop `_DETAIL_AUTHORING_SOURCES` filter for these (it was tag-only and over-eager); chunk-AABB ownership dedup handles redundancy |
| **Mixed** (some world, some origin) | `MOUN_DAM_BLDG_ViewPlatform` (6 / 10), `MOUN_DAM_BLDG_InletTower` (11 / 0) | centroid-aware filter — drop near-origin entries only |
| **All origin** (only authoring-local exists) | `REDROCKS_BLDG_Observatory` (0 / 7), `REDROCKS_amphitheatre` (0 / 2), `MiningTower01` (0 / 33), `Gondola`, `MAINTOWN_BLDG_WarehouseOffices` (0 / 42), `MOUN_GM_HillSideMine` (0 / 7) | **OPEN.** No world-positioned named asset for these landmarks. They must be rendered through some other mechanism — possibly via an Object NNN wrapper that bakes them with a transform we haven't yet decoded, or an xex code-side transform table. |

The "all origin" group is where remaining landmark-placement
investigation effort goes. ~6 landmark families. The chunk-AABB
ownership pass cannot help these — the asset literally only exists
as asset-local geometry in the pool. Either:

- An Object NNN wrapper at the landmark's world position has the
  asset baked in (then chunk-AABB ownership keeps it correctly).
- An xex code-side lookup table provides the world transform
  (similar pattern to the within-section selector mystery in
  `v42k7-section-mesh-selection.md` §5.2).

## What was ruled out (history, do not re-investigate)

- `default.xex` `.text`/`.data` (immediate-load scan, u32-handle
  scan, 4×3/4×4 matrix scan, name-string scan, FNV1a/JOAAT/CRC32
  hash search — all negative)
- `.col` directory + virtual 105 MB pool (no on-disk source,
  no bin.zip subset reconstruction)
- All visibility files: `Colorado_00.pvs`, `__R00Z*.pvsz`,
  `PVSZLookup_00.dat`, `Colorado_00.hex`
- `FilenameMap_00.dat` (filename strings only)
- `colorado.owr`, `.oww`, `.crowd` (12-32 B stubs)
- `colorado.nav` (road graph)
- `co_cone.bin` (CAFF compiled GPU resource)
- `PhysicsDefinitions.bin` (vehicle physics)
- `media/db/gamedb.slt` (UI metadata)
- All Ribbon_00 XML: `ParticleEmitters`, `PostProcessingZones_Safe`,
  `GameObjs`, `TrackRoute*`, `PreRaceCams`, `TrackSegCams`,
  `SmashableEffects`, `TimeOfDay*`
- `animatedobjects.zip`
- Bbox-fingerprint sibling search across 102k pool (negative for
  the truly-stuck origin-only landmarks)

## File pointers

- Truth coords: `docs/placements.txt`
- Diagnostic: `scripts/check_placements.py`
- Export: `scripts/export_placements.py`
- Implementation: `src/fh1_mapdecomp/rmb_world.py` (`_is_named_asset_tag`,
  `_is_phantom_wrapper`, `_policy_drop`, `extract_rmb_world` two-pass loop)
- Probe: `probes/probe_rmb_named_section_refs.py`
- Probe output: `probes/out/rmb_named_section_refs.json`
