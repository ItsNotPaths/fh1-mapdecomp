# Free-roam placement — `Ribbon_00/` XML layer

Status: **discovered 2026-04-18**. The free-roam object placements live in
plaintext XML files under
`4D5309C9/00007000/2DC7007B/media/tracks/colorado/Ribbon_00/`, not in the
PGEO / rmb.bin pool.

Only one ribbon exists for Colorado (`Ribbon_00/`), so these files cover
the entire map, both free-roam and race/festival content.

## Ribbon_00 directory contents

| file                | size  | purpose                                            |
| ------------------- | ----: | -------------------------------------------------- |
| `CollObjs.xml`      | 7.4 M | **22k collision-prop placements, rmb-referenced**  |
| `GameObjs.xml`      | 642 K | **2.1k gameplay markers** (Barn Finds, gas stations, planes, flyers, festival activities) |
| `FilenameMap_00.dat`| 1.0 M | binary string table — every asset this ribbon references (pgeo/rmb.bin/bix/pvsz/soundscape). 40k+ strings with u32 BE offset header. Not decoded yet; likely the master ribbon-manifest. |
| `CollObjs.xml` mirror types | — | All `CollObjs` entries resolve 100% to rmb.bin blob tags (verified 758/758). |
| `Colorado_00.hex`   |  34 K | HEXY grid (per existing `fh1_mapdecomp.world.build_placement`). |
| `Colorado_00.pvs`   | 3.2 M | **Authored placement table** (decoded 2026-05-03). 14,560 models, 62,173 instance slots; per-instance transform from matching `__R##Z#####.pvsz`. See `pvs-format.md`. |
| `Colorado_track_00.col` | 76 K | track collision geometry.                       |
| `GameObjs.xml`, `TrackRoute*.xml`, etc. | — | race routes and gameplay metadata. |

## CollObjs.xml — street-furniture placement layer

Each entry is:

```xml
<Obj0 PhysicsType="CO_ArmcoArrow_001.0.rmb" GraphicsName="#0">
  <Pos x="-608.297485" y="87.266258" z="-2952.991943"/>
  <Orientation>
    <XAxis x="0.933504" y="0.000000" z="-0.358568"/>
    <YAxis x="0.000000" y="1.000000" z="0.000000"/>
    <ZAxis x="0.358568" y="-0.000000" z="0.933504"/>
  </Orientation>
</Obj0>
```

- **21,877 `<ObjN>` entries total.**
- **`PhysicsType`** names match rmb.bin blob tags directly
  (`CO_ArmcoArrow_001.0.rmb` → tag `CO_ArmcoArrow_001_LOD00`). The
  `.N.rmb` digit is the LOD level.
- **`Pos`** is world position in game coordinates (Y-up, same as PGEO
  bbox coordinates — no further transform needed).
- **`Orientation`** is a right-handed 3×3 rotation matrix given as
  column vectors `XAxis`/`YAxis`/`ZAxis` (each is a world-space axis).
- **`GraphicsName="#N"`** is a hash reference, likely into
  `FilenameMap_00.dat`. Not decoded yet; irrelevant for placement since
  `PhysicsType` already resolves the mesh.

### Free-roam vs race filter

Type names fall into clean area prefixes:

| prefix        | count  | meaning                                 | keep for free-roam?  |
| ------------- | -----: | --------------------------------------- | -------------------- |
| `CO_CLRD_*`   | 7,802  | Colorado world-scenery props            | **yes**              |
| `CO_FEST_*`   | 7,785  | Festival / race-event dressing          | **no** (filter out)  |
| `CO_MOUN_*`   |   352  | Mountain-area props                     | **yes**              |
| `OBJ_CLRD_*`  |   453  | Colorado static signs (non-collision)   | **yes**              |
| `CO_Table_*`, `CO_Bench_*`, `CO_SignG_*`, `CO_MarkerPole_*`, `CO_WoodenBarrier_*`, `CO_ArmcoArrow_*`, `CO_HouseMailBox_*`, etc. | 5,485 | generic tokens (no area prefix) — mix of road furniture and festival kit | case-by-case |

Strong policy: **keep `CO_CLRD_*` + `CO_MOUN_*` + `OBJ_CLRD_*`, drop
`CO_FEST_*`**. The generic `CO_*` tokens (benches, tables, signs,
barriers) are mostly roadside furniture and should stay.

### Validation

Verified 2026-04-18: across the 200 rmb.bin files sampled at startup
(102,012 blobs total, 12,947 unique tags), **100% (758/758) of distinct
`PhysicsType` base names match an rmb blob tag** (exact or
`_LOD00`-suffixed). The XML is a complete, authoritative placement
source for the props it covers.

## GameObjs.xml — gameplay markers

```xml
<Obj0 GameplayID="BF_CUDA_426BF_CLOSEDC">
  <Pos x="-1291.224976" y="46.967731" z="-3502.273926"/>
  <Orientation>…</Orientation>
</Obj0>
```

- 2,148 entries with the same Pos+Orientation shape as CollObjs.
- Prefixes:
  - `BF_*` / `BARNFIND_*` (45)  — Barn Find classic cars (iconic free-roam).
  - `PLANE` (138), `flyer` (100) — planes & billboards (free-roam).
  - `GASSTATION` (30), `OUTPOST` (10) — free-roam service POIs.
  - `EXHIBITION` (62), `FESTIVAL` (39), `festival` (7), `race` (5) — festival activities.
  - `FR01`…`FR69`, `NR02`…`NR06` — race/route spawn markers (~1400 entries, 20 each).
  - `e3` (48), `average` (36), `speed` (44), `NEM` (38) — camera-rigs / timers / misc gameplay.
  - Small singles: `TRACK`, `airport`, `multiplayer`, `REDR`, `MAIN`.
- `GameplayID` identifies logic entities. Most map to a mesh via the
  name prefix (BF = car model, PLANE = aircraft mesh, etc.) but not
  via a file-path reference. For Barn Finds / planes, the mesh is
  likely in `media/cars/` or `animatedobjects.zip`; not in rmb.bin.

## animatedobjects.zip — landmark animations

40 `ANIM_*.pgeo` files (e.g. `ANIM_Bungee_Bridge_001.pgeo`,
`ANIM_Cablecars_001.pgeo`, `ANIM_Autoshow_Lights.pgeo`). These are
separately packaged from bin.zip and presumably placed by a still-to-
identify master file (likely one of the landmark XMLs in Ribbon_00
or a flag in GameObjs.xml).

## What this means for the extraction pipeline

1. **CollObjs.xml is the correct source for collision prop placement.**
   v42k7's race-dressing placements for these tags are redundant
   (possibly just a runtime cache). A new `collobjs-inst` pipeline can
   parse the XML, resolve names to rmb blobs, and emit per-instance
   `(pos, rot)` with no reverse-engineering needed.
2. **GameObjs.xml covers the gameplay-marker layer** (barn finds, gas
   stations) — smaller but equally clean.
3. **v42k7 is still load-bearing for buildings**: `BLDG_MainTown_*`,
   `MT_Area01_Terrain*`, `FEST_MISC_Tollbooth_*` handles only appear
   via v42k7 section records. So v42k7 cannot be abandoned; instead
   we should filter v42k7 handles **against** CollObjs.xml
   references — anything v42k7 places that CollObjs already covers
   is a duplicate and should be dropped.

## v42k7 is mixed free-roam + race — split by referenced rmb tag

Follow-up probe (2026-04-18): 300 `Models_Ungrouped_*` chunks sampled,
bucketed by the rmb blob tags their sections reference:

| prefix referenced      | instances | free-roam? | notes                          |
| ---------------------- | --------: | :--------: | ------------------------------ |
| `OBJ_BarrierBrand_*`   |       670 | no         | race barriers                  |
| `S_Debris_*`           |       585 | **yes**    | roadside debris                |
| `BLDG_MainTown_*`      |       384 | **yes**    | **town buildings**             |
| `TERR_CLRD_*`          |       290 | **yes**    | terrain patches                |
| `Barnfind_Barn_*`      |       275 | **yes**    | Barn Find barns                |
| `GrandstandStraight_*` |       240 | no         | race grandstands               |
| `MT_Area03_*`          |       233 | **yes**    | mountain area terrain          |
| `Plains_Area2_*`       |       220 | **yes**    | plains terrain                 |
| `OBJ_CLRD_*`           |       117 | dedup      | already in CollObjs.xml        |
| `cables_544/570/...`   |       250 | **yes**    | power/utility cables           |
| `CO_CLRD_*`            |       107 | dedup      | already in CollObjs.xml        |
| `OBJ_FEST_*`           |       104 | no         | festival dressing              |
| `RV_Dawning_*`         |        87 | **yes**    | area environment               |
| `MT_Area01/02_*`       |       100 | **yes**    | mountain area terrain          |
| `Ambulance_LOD00`      |        66 | no         | race-event prop                |
| `Countdown_Left_*`     |        63 | no         | race-start prop                |
| `Barrier_016/023_*`    |       107 | no         | race barriers                  |
| `PROC_cars_*`          |        34 | no         | NPC parked cars                |

So **v42k7 carries BOTH layers intermixed**. Free-roam content can be
extracted by filtering section-referenced rmb tags by prefix.

### Free-roam filter policy for v42k7 `Models_Ungrouped_*`

**KEEP** sections whose slot-0 rmb tag starts with any of:
- `BLDG_*` (buildings)
- `Barnfind_*` (Barn Find barns)
- `TERR_*` (terrain patches beyond the rmb TERR pool)
- `Plains_Area*`, `MT_Area*`, `RV_*` (area environment)
- `S_Debris` (roadside debris)
- `MiningTower*`, `Pavementcap_*` (landmarks, road finish)
- `cables_*` (utility cables, free-roam infrastructure)

**DROP** sections referencing:
- `OBJ_BarrierBrand*`, `Barrier_0*` (race barriers)
- `Grandstand*`, `Countdown*`, `Ambulance*` (race-event props)
- `OBJ_FEST*`, `PROC_cars` (festival dressing, NPC cars)

**DEDUPE** against CollObjs.xml (DROP):
- `CO_*`, `OBJ_CLRD_Signs_*` — these are already placed by the
  authoritative CollObjs layer.

## v42k7 proc-subvariant (`models_proc_clrd_*`) — decoded 2026-04-18

469 chunks (the ones with 0xFFFFFFFF sentinel at 0x60; descriptor
name at 0x98). Verified on `__R00G06389.pgeo`
(`models_proc_clrd_trees_redstone_area02[freeroam]_0`):

- Format is **identical to regular v42k7**: vbuf at file end
  (`vbuf_start = len - body[0x50] - 4`), 40-byte packed-10-10-10
  position records with the `e3 d0` anchor at rec[32].
- 13 tree positions all unpack inside chunk bbox.
- **No per-instance rotation table** (trees are axis-aligned), so the
  96B post-vbuf table that regular v42k7 carries is absent.
- Name at 0x98 identifies CONTENT: `trees_redstone_area02[freeroam]`
  explicitly flags free-roam. `festival_barriers` / `track_foot` flag
  race-only.

### Free-roam filter policy for v42k7 proc subvariant

**KEEP** descriptor names starting with:
- `models_proc_clrd_trees_*` (54 chunks — trees per area)
- `models_proc_clrd_street_*` (31 chunks — street dressing)
- `models_proc_clrd_multiplayer_*` (33 chunks — MP-zone dressing;
  likely free-roam zones like golfcourse, maintown, beaumont,
  warehouses, foothills, mediacentre)

**DROP** descriptors:
- `models_proc_clrd_festival_*`, `..._fest_*`, `..._barriers_*`,
  `..._track_*`, `002_barriers`, `barriers_showcase`
- Anything with `[freeroam]` is explicit; treat its absence as
  ambiguous only for the `multiplayer_*` subclass, which is
  generally free-roam multiplayer zones.

## FilenameMap_00.dat decoded (inventory, not placement)

Structure: u32 BE offset table header, then null-terminated ASCII
strings. 43,546 entries pointing into `0x2a868..0x10255c`. Contents
are a flat asset inventory for this ribbon — every .bin, .bix, .pgeo,
.fiz, .soundscape, .pvsz, .sh, .fxobj, .bundle referenced.

```
 17,257 .bin        (rmb.bin etc.)
 10,468 .bix        (texture containers)
  7,970 .pgeo       (chunks — same count as bin.zip unique pgeos)
  3,201 .fiz        (foliage index zones?)
  1,450 .soundscape
  1,434 .pvsz
  1,421 .sh         (terrain-height tiles with world-coord filenames)
```

**No BLDG_* placement data here** — FilenameMap is a manifest, not a
placement source.

## Final placement-layer map

1. **CollObjs.xml** — 21,877 street-furniture placements, rmb-direct,
   authoritative for all `CO_*` and `OBJ_CLRD_Signs_*` tags.
2. **GameObjs.xml** — 2,148 gameplay markers (barn finds, gas
   stations, planes, festival activities, race-route spawn markers).
3. **v42k7 `Models_Ungrouped_*` chunks** — buildings + Barn Finds +
   terrain patches + area environment + race dressing, all
   intermixed. Filterable by referenced rmb tag.
4. **v42k7 proc subvariant (`models_proc_clrd_*` chunks)** — trees
   + street dressing + multiplayer-zone dressing + festival dressing,
   filterable by descriptor name. Same vbuf format as regular v42k7.
5. **animatedobjects.zip** — 40 ANIM_*.pgeo landmark animations,
   packed separately from bin.zip. Placement source not yet found
   (not in CollObjs, not in GameObjs). Candidate: embedded in
   `Colorado_00.hex` (HEXY grid) or the `.bundle` files referenced
   by FilenameMap.

## Still open

- `GraphicsName="#N"` semantics in CollObjs.xml — probably a hash
  index, not needed for placement.
- `.owt` files in `aiopenworld.zip` — per-route AI driving data.
  Static-placement-irrelevant.
- Where are ANIM_*.pgeo landmark animations placed?
  Candidates: `Colorado_00.hex`, undiscovered bundle file.
- `u0=0` semantics in v42k7 Table B (still source of the NW-city ring
  artefact). Per `v42k7-placement-bugs.md`, likely occluder/shadow-
  proxy points. Filter these out for free-roam export.

## Recommended pipeline for free-roam export

```
   CollObjs.xml  ──────►  street furniture       (direct rmb lookup)
   GameObjs.xml  ──────►  gameplay markers       (car models, POIs)
   v42k7 Models_Ungrouped (filtered by rmb tag)
                 ──────►  buildings, barns,
                          terrain patches,
                          cables, debris
   v42k7 proc_clrd (filtered by descriptor name)
                 ──────►  trees, street dressing,
                          multiplayer-zone dressing
   terrain_hi (existing)
                 ──────►  Colorado heightfield
```

Filter out:
- CollObjs: `CO_FEST_*` prefix
- v42k7 Models: race-dressing tags (barriers, grandstands, etc.)
- v42k7 proc: festival/barrier/track descriptors
- v42k7 Table B: `u0=0` records (ring artefact)

## 2026-04-18: CollObjs extractor shipped

New module `src/fh1_mapdecomp/collobjs.py` + CLI subcommand
`fh1-mapdecomp collobjs-inst --source … --output …`. Emits an
`index.json` + `blobs/*.npz` set with the same schema as the
`v42k7_inst` pipeline, so the Blender importer consumes it unchanged
through a new shared helper `_import_inst_dir`. The importer has a
new `--collobjs <dir>` flag (parallel to `--v42k7-inst <dir>`); the
`blender`, `all`, and `cmd_all` paths wire it through.

### Resolution reality check

The earlier claim that 100% of distinct `PhysicsType` names resolve
to rmb tags (`_LOD00` suffix or exact) was wrong. Against the full
Colorado `bin.zip` pool (12,947 distinct rmb tags, 102k entries), the
XML carries 758 distinct base names. The rmb pool uses at least four
naming conventions interchangeably:

| XML base              | rmb raw tag            | rule                        |
|-----------------------|------------------------|-----------------------------|
| `CO_CLRD_Sign_Deer`   | `CO_CLRD_Sign_Deer`    | exact                       |
| `CO_SignG_001`        | `O_SignG_001`          | leading `C` dropped         |
| `CO_CLRD_FenceF`      | `O_CO_CLRD_FenceF`     | `O_` prepended              |
| `CO_CLRD_FenceF`      | `OBJ_CLRD_FenceF_LOD00`| `CO_→OBJ_`, `_LOD00` suffix |
| `CO_WoodenBarrier_001`| `O_WoodenBarrier_01`   | all of the above + zero-pad |

The current 4-pass resolver (candidate tags → normalised fallback
with leading-zero collapse) handles all of these. End-to-end:

    21,877 xml entries
     7,785 dropped by freeroam policy (`CO_FEST_*` / `OBJ_FEST_*`)
     9,119 kept instances resolved (≈65% of the freeroam set)
     4,865 unresolved (top: CO_MarkerPole_001, CO_Table_Picnic,
                              CO_Bench_001, CO_Arrowright)
        51 unique rmb blobs covering those 9,119 placements

The remaining ~35% unresolved are bases with **no** matching rmb tag
anywhere in the extracted game tree (grep across every `.bin`/`.zip`
under `4D5309C9/` confirms the names appear only in `CollObjs.xml`).
They are likely collision-only proxies reused by the engine, or
assets missing from this dump. Treat them as an upper bound on what
this XML can yield without a second asset source.

### Blender verification

Headless import of the 9,119 CollObjs placements builds in ~4 s and
produces a 51-material scene where every PhysicsType shows up as a
point-cloud with a Geometry Nodes "Instance on Points" modifier, via
the shared `_import_inst_dir` loader.
