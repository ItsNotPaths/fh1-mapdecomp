# Parsed inventory — what we've decoded, what's left

Per-file status under `4D5309C9/00007000/2DC7007B/media/`. For the
mesh-export angle (geometry coverage + remaining missing models)
see `mesh-export-state.md` — that's the canonical answer; this doc
is the file-by-file backup.

Status legend:

- ✅ **decoded** — parser implemented, output flows through the pipeline
- 🟡 **partial** — header + classification done, body undecoded or
  partial-decoded
- ❌ **unparsed** — known to exist, no parser written
- ⛔ **out of scope** — explicitly excluded (cars, audio, UI, physics)

---

## Top-level container layer

| File / dir | Status | Parser | Notes |
|---|---|---|---|
| `aiopenworld.zip` | ✅ | `binzip.read_entry` | LZX-decompresses; 46 `.owt` road-centerline files. **Format fully decoded** 2026-04-27 (fifth pass): `OWTM` magic + 32-byte header (`version=1` u32 BE @+0x04, `record_count` u32 BE @+0x10) + `count × 48-byte records` + 16-byte trailer. Each record = 12×f32 BE: fields[5..7] are world (X, Y, Z), fields[0..4] are orientation/curvature, fields[9..11] are tangent/direction. 100 % of positions in Colorado bounds across all 46 routes. Probe: `probe_owt.py`. **No first-class parser yet** but layout fully understood. |
| `animatedobjects.zip` | 🟡 | `binzip.read_entry` | LZX-decompresses; 290 `ANIM_*.pgeo` (windmills, ferris, deer, pedestrians) + 361 `.xds` textures. **PGEO bodies for these all undecoded; placement source unknown.** 2 files (`Showcase_Event_Helicopter_Race`, `Timed_Event_Biplane`) fail `lzxd_decompress rc=11` — multi-chunk LZX edge case. |
| `camera.zip` | ⛔ | — | Cameras / cinematic. Out of scope. |
| `dynamicpost.zip` | ⛔ | — | Post-processing LUTs (.dds + xml). |
| `effects.zip` | ⛔ | — | 333 entries, 261 xml + 72 xds (particles). |
| `gamemodes.zip` | ⛔ | — | 292 entries, gameplay logic xml + 1 py. |
| `gametunablesettings.zip` | ⛔ | — | 37 entries. |
| `Livery.zip` | ⛔ | — | 1,372 `.carbin`. |
| `physics.zip` | ⛔ | — | 7 entries. |
| `realtimesky.zip` | ⛔ | — | 28 entries (skybox xds + xml). |
| `renderscenarios.zip` | ⛔ | — | 35 xml. |
| `Spectators.zip` | ⛔ | — | 119 skinned meshes. |
| `UI.zip` | ⛔ | — | 694 UI layouts. |
| `audio/`, `speech/` | ⛔ | — | Audio/speech assets. |
| `brakes/`, `carlights/`, `cars/`, `wheels/` | ⛔ | — | Car-related. |
| `db/`, `profileschema/`, `stringtables/` | ⛔ | — | Game DB / strings. |
| `navigation/` | ⛔ | — | SatNav VO config. |
| `shaders/` | ⛔ | — | Compiled shader bundles. |
| `ui/` | ⛔ | — | UI assets. |
| `tracks/` | (see below) | various | The map data we care about. |
| `ControllerFFB.ini`, `ControllerRumble.xml`, `DemoConfigs.xml`, `FilesToCache.xml`, `ThumbnailCamSettings.xml`, `UICarTargetCameraPresets.xml`, `zipmanifest.xml` | ⛔ | — | Plain config / cache lists. |

---

## Track files — `tracks/colorado/` and `tracks/colorado/Ribbon_00/`

### Colorado-level files

| File | Size | Status | Parser | Notes |
|---|---:|---|---|---|
| `bin.zip` | ~600 MB | ✅ | `binzip.list_entries`, `read_entry` | Pseudo-zip (no LFH). 41,587 entries: 7,970 case-unique pgeo + 102,012 rmb.bin + .pvsz + .bix + .fiz + .soundscape + .sh. |
| `co_cone.bin` | 35 KB | 🟡 | (probe-level) | CAFF chunked container (magic `CAFF21.11.05.0034`) holding `.gpu`/`.data` sub-chunks. Single-asset (the cone). Not placement. Probe `probe_unparsed_binaries.py`. |
| `colorado.nav` | 586 KB | 🟡 | (probe-level) | Free-roam AI navigation graph. Magic `0x0e177551` at +0. Header @+0x04..+0x28: 9 section counts (12036, 459, 3680, 13238, 13238, 17, 312, 223, 3299). Records start @+0x2c, **stride 32 B = 3×f32 BE pos + 5×u32**. Sample positions sequential along roads (~12 m apart) within Colorado bounds. AI driving graph; **not** a landmark-placement source. |
| `colorado.owr` | 12 B | 🟡 | (probe-level) | Stub — only the `0x0e177551` magic + a zero u32 + the magic again. No payload. |
| `colorado.oww` | 32 B | 🟡 | (probe-level) | Stub — magic `OWWF`, length `0x24`, hash `0x87d73394`, count `0x8a4` — repeated twice. No real payload. |
| `colorado_ambience.xml`, `colorado_reverb.xml`, `colorado_soundbank_lookup.xml` | — | ⛔ | — | Audio config. |
| `ColorGradingLookup.dds`, `ColorGradingLookup_Night.dds` | — | ❌ | — | Standard DDS, not wired into our material pipeline yet. |
| `PhysicsDefinitions.bin` | 129 KB | 🟡 | (probe-level) | Header u32 BE @+0x00 = 0x54 (84), @+0x04 = 0x0d (13). Body is f32 BE constants (e.g. 100, 271.39, 235.07, 38.29). Per-vehicle physics tunables. Not placement; out of scope for map export. |
| `PVSZoneSpeeds.dat` | 5,736 B | ✅ | (probe-confirmed) | **Solved.** Pure f32 BE array — `5736/4 = 1434 = exact PVS zone count`. One AI speed-limit value per zone (samples 50–75 m/s). Trivial parser; no first-class wrapper yet. |
| `SmashableObjectTypes.xml` | — | ❌ | — | Plain XML; readable but not consumed. |
| `staticcarcubemap.xpr` | — | ❌ | — | Xbox 360 packed resource (cubemap). |
| `smoke.dds` | — | ❌ | — | Standard DDS. |
| `TrackSettings.xml` | — | ❌ | — | Plain XML, readable but not consumed. |
| `airborne_challenges.xml` | — | ❌ | — | Plain XML, gameplay. |

### Ribbon_00 (per-track scope; 260 files)

#### Free-roam placement authorities — **decoded and wired**

| File | Status | Parser | Notes |
|---|---|---|---|
| `CollObjs.xml` | ✅ | `collobjs.py` | 21,877 prop placements. Resolved via 4-pass fuzzy resolver; 9,119/14,092 freeroam placements (~65%) hit the rmb pool. Remaining ~35% are collision-only proxies with no rmb entry anywhere in the game tree. |
| `GameObjs.xml` | 🟡 | `collobjs.py` (parser) | 2,148 markers (Barn Finds, gas stations, planes, festival, race routes). Parser works but **emit-to-Blender wiring not done** — Barn Finds are not yet placed in the scene; v42k7 over-emission of `Barnfind_Barn` is filtered by drop-list workaround. |

#### PVS / streaming subsystem — **partially decoded**

| File | Status | Parser | Notes |
|---|---|---|---|
| `Colorado_00.hex` | ✅ | (probe-level) | Magic `HEXY` v0x65. Full layout: 32-byte header (cell=100m, origin=(-6700, -6495.19), n_refs=1434, grid=91×60) + body @+0x20..+0x5570 = `91×60 = 5,460 u32 BE cells, cell-major`, each holding a `zone_id` (or `0xFFFFFFFF` for empty). Exactly **1,434 cells are non-empty, holding all 1,434 zone_ids exactly once** → 1:1 zone↔cell map. World XZ for cell `(cx, cz)` = `(origin_x + (cx+0.5)*100, origin_z + (cz+0.5)*100)`. Trailer @+0x5570..EOF holds `(u32, u32)` pairs — almost certainly the zone-adjacency graph for PVS. 2026-04-27 (fourth pass) probe: `probe_hexy_decode.py`, `probe_hexy_layout.py`. |
| `Colorado_00.pvs` | ✅ | `pvs.parse_pvs` | Magic `FPVS` v0x32 (= 50 = FH1), 3.2 MB. **PVS is the engine's authored placement table — NOT a "visibility tree".** Body decoded against the Doliman100/austinbaccus FH1 fork: 19,613 textures, 173 shaders, **14,560 models, 62,173 model_instances**, 1,434 streamed zones. Each `PVSModelInstance` resolves to `{prefix}.{model_index:05d}.rmb.bin`; transform comes from the matching `__R##Z#####.pvsz`. See `pvs-format.md`. |
| `PVSZLookup_00.dat` | 🟡 | (probe-level only) | 1,434 × `(zone_hash, zone_index)` pairs. Layout fully understood; **no first-class parser yet**. Used by the engine's runtime zone lookup; not needed for static placement extraction now that we have `pvs.py`. |
| `__R00Z*.pvsz` (1,434 files inside `bin.zip`) | ✅ | `pvs.parse_pvsz` | **Re-classified 2026-05-03.** Earlier docs called these "per-zone streaming manifests" because we only parsed the leading `u32 count + count × u32` index array. The file actually continues past that with several length-prefixed arrays and ends with a `model_instance_details[]` array carrying the **per-instance world transform** (3× f32 BE translate + 3×3 f16 BE rotation + f32 material data). The two arrays zip position-wise to fill `PVSModelInstance.transform[index]`. **Decoded by `pvs.parse_pvsz`** following the Doliman addon's reader. The legacy `probe_pvsz_zone_xref.py` is still useful for the index-only handle-to-zone reverse map. |

#### Ribbon meta — **mostly unparsed**

| File | Status | Notes |
|---|---|---|
| `FilenameMap_00.dat` | ✅ | **43,546 u32 BE byte-offsets at +0..+0x2A878** (= start of string area), then null-terminated ASCII strings to EOF. Strings are bin.zip filenames the ribbon depends on (samples: `__R00G02725.pgeo`, `_0x000047D7_B.bix`, `coloradoout.02993.rmb.bin`, `colorado_00_00572.soundscape`). Top prefix bins: `coloradoout` 14,291 (= unique rmb count), `Colorado/colorado` 2,874, `_0x` hash names 13,578. **Zero `'dam'` and zero `'LOD'` strings**, confirming this is a ribbon-scoped streaming dependency list, not a name lookup or placement table. 2026-04-27 (third pass) probe: `probe_sh_filenamemap.py`. |
| `Colorado_track_00.col` | 🟡 | 77 KB. Directory of 3,200 `(idx, rmb_handle, base_off, size, 0, next_off)` records starting at +0x58, stride 24, into a virtual 104.5 MB collision pool that is not on disk and not reconstructible from any bin.zip subset. World bbox @+0x08, sentinel `0xABCD1234` @+0x30, cell 100 m. **Per-handle placement is NOT in this file** — 461 of the 3,200 references are local-authored landmarks; their placement is the named-section-ref mechanism in rmb bodies (see `mesh-export-state.md`). Probes: `probe_col_decode.py` etc. |
| `Colorado_track_00.crowd` | 🟡 | 12 B stub: `count=2, 0, 0`. No real payload — likely a placeholder for a feature unused by this track. |
| `ParticleEmitters.xml` | ⛔ | Plain XML. |
| `PostProcessingZones_Safe.xml` | ⛔ | Plain XML. |
| `PreRaceCams.xml` | ⛔ | Plain XML. |
| `SmashableEffects.xml` | ⛔ | Plain XML. |
| `TimeOfDay*.xml` | 🟡 | Readable; not consumed by our renderer. Could drive a Blender world shader. |
| `TrackRoute*.xml` (~211 files) | ⛔ | Race-only route definitions. |
| `TrackSegCams.xml` | ⛔ | Per-segment cinematic cameras. |

---

## bin.zip contents — by file kind

| Kind | Count | Status | Parser | What's decoded / left |
|---|---:|---|---|---|
| `*.pgeo` | 41,587 entries (7,970 case-unique) | 🟡 | `pgeo.py`, `pgeo_body/*`, `crowd_inst.py` | Header + variant classifier ✅. Body decoders: **terrain ✅, v42k7 ✅, v42k7 proc-subvariant ✅, crowd ✅ (inanimate-only, see §3.6)**. Crowd body parses 8,514 / 8,525 files into per-instance positions; 11 outliers are Format C (large-bbox, f32-explicit) and deferred. Inanimate filter (`*_barriers_*`/`*_stalls_*`/`*_stages_*`/`*_grandstands_*`) emits 7,696 placements via `crowd_inst.py`; animated spectator scatter is intentionally skipped. Undecoded body classes: **grass (11,292), vegetation (3,316), v44k5 (1,578), landmark_anim (447)** — grass-tufts / light-glows / festival-rides / animated-procs. The landmark_anim PGEOs carry embedded `Anim_ANIM_*` strings naming the rig (Ferris, Zipper, Windmill, autoshow lights, etc.); body format undecoded. |
| `*.rmb.bin` | 102,012 | ✅ | `rmb.py` | Header + tag + centroid + bbox ✅. Vertex positions ✅ (any tag/stride). Sub-blob discovery ✅ (24,650 unlocked). Triangle-strip index decode ✅. **Named-section refs identified** (2026-04-27): 118,526 refs across 59,042 entries; section name field can hold a foreign asset tag (e.g. `MOUN_DAM_BLDG_MainDam_003`). Provides a **labelling map** from anonymous wrapper rmbs (`Object010_LOD00`) to authored-asset names — the wrappers are already placed by `rmb_world.py`. See `mesh-export-state.md`. **Still TODO**: vertex attributes past position (normal/UV/colour); wiring the labelling pass into export. |
| `*.pvsz` | 3,502 | ✅ | `pvs.parse_pvsz` | Per-zone authored placement transforms (one file per `(ribbon, zone)` pair across all ribbons). Decoded — see PVS row above. |
| `*.bix` | 42,657 | 🟡 | (probe-level) | Texture format with at least two flavours. Some entries (e.g. `_0x00003360.bix`) carry a **`BIX1` magic** with `width=128, height=128, mipmaps=8` u32 BE in the header. Most entries are raw block-encoded payload (no ASCII magic) with strong DXT-block-style byte patterns (`0xAAAA*`, `0x1082_0841`). Custom format — NOT standard DDS/XPR. Filenames are mostly `_0xHHHHHHHH_B.bix` (hash-keyed) plus a few human-named. **Highest-leverage unparsed format for materials.** Probe: `probe_bix_fiz.py`. |
| `*.bin` (non-rmb) | 13,188 | 🟡 | (probe-level) | **Compiled textures**, NOT collision data. Magic `CAFF21.11.05.0034` followed by `.data` + `.gpu` sub-chunks. Embedded build path: `D:\scratch\p4\horizon\Forza2\Main\Media\Src\Tracks\colorado\scene\compiled\textures\_0xHHHHHHHH.bin`. Hash-keyed filenames `_0xHHHHHHHH.bin`. The CAFF body holds the texture's `.gpu` resource (XPR-like). Apparent "world-coord triples" inside CAFF are byte-pattern coincidences from struct offsets, not real positions. Probe: `probe_caff_bin.py`. |
| `*.bundle` | 429 | 🟡 | (probe-level) | Hash-keyed (`_0x10000xxx.bundle`). Header magic `0x1A207F52` at +0x14 — **same magic that appears in `.bix`'s BIX1 variant**, suggesting a shared Turn-10 cooked-resource container. Large (up to 4 MB dsize). Probable texture bundles / atlases. Probe: `probe_misc_kinds.py`. |
| `*.fxobj` | 173 | ⛔ | — | Compiled shaders (filenames like `shaders/track/H_BLND3_SPEC_NORM2_AO.fxobj`). Out of scope for map export. |
| `*.fev` | 66 | ⛔ | — | FMOD audio events, magic `FEV1`. Out of scope. |
| `*.fsb` | 66 | ⛔ | — | FMOD audio sound banks. Out of scope. |
| `*.fiz` | 16,934 | 🟡 | (probe-level) | **Confirmed foliage with world-coord header.** Magic `'fiz '` @+0x00, version `1` @+0x04. Header carries count fields, then 4×f32 BE world bbox @+0x38 (e.g. `(-2916.86, -2796.5, -2348.80, -2225.5)` — in Colorado bounds), cell size 10 m, quant step 0.1 (`0x3DCCCCCD`). Filenames are zone-numbered (`1024.fiz`..`16934.fiz` etc.) — one file per zone. Probable per-zone foliage instance lists. **Strong candidate for vegetation density.** Probe: `probe_bix_fiz.py`. |
| `*.soundscape` | many | ⛔ | — | Audio. |
| `*.sh` | 4,768 | 🟡 | (probe-level) | **Confirmed spherical-harmonic light probes.** Filenames are world-XZ-keyed: `Colorado__shdata__n2550x_n1645z.sh` (X=-2550, Z=-1645). Body header @+0x00..+0x20: u32 BE `0x08`, u32 BE `0x06`, **f32 BE world X** @+0x08, zero, **f32 BE world Z** @+0x10, zero, u32 BE cell flag (`0x1a`/`0x1b`) @+0x18, u32 BE `n_probes` @+0x1c. Then a per-tile origin block at +0x20. Per-probe stride ≈ 175 bytes. Header XZ is the tile's bottom-left corner (offset by ~100m from the filename's tile-center). Lighting only — not landmark placement. Probe: `probe_sh_filenamemap.py`. |

---

## Output pipeline status

| Output | Source authority | Status |
|---|---|---|
| `out/terrain_hi/` | `rmb.py` TERR pool | ✅ 2,569 LOD00 tiles, 5.06M verts, real heightmap |
| `out/rmb_world/` | rmb header centroid (\|XZ\|>20m) | ✅ 24,058 world-placed blobs (post-2026-04-27 festival-hub keep + dam-leak deny) |
| `out/collobjs_inst/` | Ribbon_00/CollObjs.xml | ✅ 7,480 instances at correct (pos, rot) |
| `out/crowd_inst/` | bin.zip crowd PGEOs (inanimate filter) | 🟡 outer ring of festival metal stanchion barriers placed (87 instances of `OBJ_FEST_BarrierMetal_LOD00_` along NE/NW/SE/SW + 2 NE stub loops). Inner barrier ring missing (different storage). Rotations off — orientation byte (record +0x06) still undecoded. Stalls / stages / grandstand_supports filter dropped after visual review (those descriptors place spectator scatter, not the prop meshes). Shelved 2026-05-03 — see `memory/project_crowd_barriers_shelved_2026-05-03.md` and `docs/pgeo-body.md §3.6`. |
| `out/v42k7_inst/` | bin.zip v42k7 PGEOs | 🟡 1,138 chunks / ~21k instances; **slot-0 sentinel issue still open** (Modular_003/015/019 over-emission, same family as the Barnfind_Barn 2,897× pattern) |
| `out/blender/colorado.blend` | aggregator | ✅ renders, top-down PNG generated |
| Materials / textures | `.bix` / `.xds` | ❌ flat-grey |
| Lighting | `.sh` / lightmap_* refs | ❌ unlit |
| `GameObjs.xml` instances (Barn Finds, gas stations, planes, festival markers) | parsed but not emitted to scene | 🟡 |
| `aiopenworld.zip` road centerlines | unparsed `.owt` | ❌ would render ribbons of AI-traffic paths |
| `animatedobjects.zip` ANIM rigs (windmills, ferris, etc.) | undecoded PGEO bodies + unknown placement source | ❌ |

---

## Items left to scout (ranked by user-visible impact)

### High impact — needed for "every authored mesh in the scene"

1. **Landmark placement — solved.** See `mesh-export-state.md` and
   `landmark-placement-investigation.md`. Wrappers like
   `Object010_LOD00` already ship the dam (and other landmarks) via
   `rmb_world` at world centroid; the named-section-refs scan
   provides the wrapper → authored-asset-name labelling map. The
   ~10 truly-stuck families (warehouses, AuctionShowTent, etc.) are
   tabulated in `mesh-export-state.md` § "Models we have NOT located".

2. **v42k7 slot-0 sentinel decode.** Probe E confirmed the dispersion pattern: `BLDG_MainTown_Modular_003_LOD00` (370 inst across 78 cells) and a few siblings share slot-0 across many chunks where the *real* hero mesh is in slot 1 or 2. Same root cause as `Barnfind_Barn__LOD00` (2,897× over-emission). Need a hand-decode of the 64B section record at `pgeo_body/v42k7.py:~146` looking for a render/proxy flag distinguishing slot 0 from slots 1/2.

3. **`rmb.bin` per-record vertex attributes**. Positions decode for any stride, but normals/UVs/colours past position are still raw bytes. Needed for textured rendering (and to identify which UV channel a material samples).

4. **`.bix` texture decode** (42,657 entries; the largest single asset class). 2026-04-27 scout: confirmed two-flavour format. Some entries carry `BIX1` magic + width/height/mip u32 BE header, most are raw block-encoded payload. Custom format, NOT standard DDS/XPR. Highest-leverage unparsed format for the materials/textures workstream.

### Medium impact — fills unparsed corners of the placement layer

5. **First-class `__R00Z*.pvsz` parser** + zone-manifest filter. Layout fully understood (see `project_fh1_pvs_subsystem.md`). Once shipped, `v42k7_inst` and `rmb_world` can use the union of all `__R00Z*.pvsz` handles as an authoritative Ribbon_00 whitelist — replaces the prefix-based KEEP/DROP heuristics with the engine's own truth and avoids cross-ribbon leakage.

6. **`Colorado_00.hex` first-class parser** (HEXY grid). Layout known; produces world-XZ → zone_id mapping. Useful for zone-aware filtering.

7. **`.fiz` foliage parser** (16,934 entries; one per zone). 2026-04-27 scout: confirmed magic `'fiz '`, version 1, and an in-Colorado world bbox (4×f32 BE @+0x38) + cell size 10 m + quant step 0.1. Filenames are zone-numbered. Parser would unlock per-zone vegetation density beyond what `grass` PGEO chunks already cover.

8. **PGEO body decoders for grass / crowd / vegetation / v44k5 / landmark_anim.** These hold per-chunk grass tufts, spectator clusters, light-glow billboards, animated festival procs, festival rides respectively. None carry placement tables (already verified) but each holds geometry we currently render as bbox-placeholder cubes.

9. **`.owt` road-centerline parser** (in `aiopenworld.zip`). 46 routes; magic `OWTM` v1, 48B records; first f32 BE triple is world position. Easy decode, would render AI-traffic ribbons.

### Low impact — nice-to-have

10. **`animatedobjects.zip` ANIM_* PGEO body decoders + placement source.** 290 rigged objects; placement source not located (candidate: `Colorado_00.hex` or `.col`). User has previously deferred.

11. **`.col` mesh export.** 2026-04-27 scout reframed this: the `.col` is a directory file pointing at an external 104.5 MB collision pool that we don't have direct disk access to. Mesh export needs the pool first. If the pool is reconstructible (e.g. from per-zone `__R00Z*.pvsz` + per-handle rmb chunks), this turns into a streaming decoder rather than a one-file parser.

12. **`.xds` texture decoder** + `.dds` wiring. Once `.bix` is decoded, materials need texture data; both formats currently extract but don't render.

13. **`.sh` spherical-harmonic light probes**. For baked lighting in Blender.

14. **`TimeOfDay*.xml` → Blender world shader.** Plain XML, readable; needs a one-off translator.

### Out of scope (deliberately excluded)

- All car-related files (`cars/`, `wheels/`, `brakes/`, `carlights/`, `Livery.zip`).
- Audio (`audio/`, `speech/`, `*.soundscape`, `Spectators.zip`).
- UI (`UI.zip`, `ui/`, fonts).
- Gameplay logic (`gamemodes.zip`, `gametunablesettings.zip`, `physics.zip`, `airborne_challenges.xml`).
- Cameras, post-processing (`camera.zip`, `dynamicpost.zip`, `renderscenarios.zip`).

---

## Cross-reference

- **Bug-and-decode notes per format**: `format.md`, `pgeo-body.md`,
  `rmb-bin.md`, `rmb-subblobs.md`, `bin-zip-layout.md`,
  `freeroam-placement.md`, `v42k7-per-instance-transforms.md`,
  `v42k7-placement-bugs.md`.
- **Latest synthesis snapshot**: `state-of-extraction.md`.
- **Probe results (2026-04-27 scout)**: `fh1-mapdecomp/probes/out/`.
