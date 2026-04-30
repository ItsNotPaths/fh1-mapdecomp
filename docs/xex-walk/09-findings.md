# fh1 xex investigation — findings

End-to-end investigation of `default.bin` (decrypted xex, 23.2 MB PE32 / PowerPC BE 32-bit, 14 sections, 57k functions) to locate world-model placement data.

## Headline result

**There is no static placement table for the missing decoratives in the xex.** The "definitive coord list somewhere" hypothesis is falsified by every encoding test. The on-disk rmb / v42k7 / collobjs data we already extract IS the placement source — and it already contains 3 of the 7 decoratives that `fh1_landmarks.txt` was hand-annotating.

## What was scanned (in order)

### 1. Truth-coord BE float scan — 0 hits
`scripts/xex_truthscan.py` — scanned the entire 23 MB xex for two consecutive BE-float words within ±5 m of any of 10 known landmark world-coords (smelter, dam, observatory, redrocks, etc.).
**Result: zero hits.** World coordinates are not stored as float pairs anywhere in the xex.

### 2. Asset-name string scan — 0 hits for missing decoratives
`scripts/xex_asset_strings.py` — searched for every asset-name family from `fh1_landmarks.txt` and related decoratives.
**Result: NONE of `PLA_GC_GolfCart`, `PLA_FARM_WindmillWood`, `GLOB_STFN_WindTurbine_02`, `STFT_Reservoir_Haybale`, `GrandstandStraight`, `OBJ_BarrierBrand`, `FP_AutoshowTent` exist as strings in the xex.** Even the family prefixes `PLA_`, `GLOB_`, `STFT_`, `STFN_` are absent. The xex does not reference these asset names.

### 3. Alternate coord encodings — noise only
`scripts/xex_truthscan_alt.py` — tested LE float, BE half-float, BE i32 mm, BE i32 cm, BE i16 m, BE Q16.16.
**Result: a handful of single-record matches at half-float (low-precision noise), one each at i16 and Q16.16.** None showed clustering — no real placement table at any encoding.

### 4. Dense coord-region scan — none look like placements
`scripts/xex_chunk_local.py` — sliding-window scan for runs where >85 % of consecutive BE u32s decode to plausible coord-magnitude floats.
**Result: 60 dense regions, all small (< 6 KB).** Sample magnitudes are normalized vectors / quaternions / animation curves, none in world-coord magnitude range.

### 5. Static-data inventory via `lis+addi` mining — no large placement arrays
`scripts/xex_static_inventory.py` — found every `.text` site that constructs a pointer immediate, bucketed targets by section.
**Result: 1495 distinct `.data` VAs referenced; largest contiguous static-data cluster is only 0xD10 bytes (3.3 KB).** Far too small for a world-scale placement array.

### 6. Embedded-blob hunt — overlay is a localized string blob
`scripts/xex_blob_hunt.py` + `scripts/xex_overlay_inspect.py` — found 2 MB of trailing data after `.reloc` (file offsets `0x1417200..0x167dcb0`). High-entropy 711 KB run inside.
**Result: it's the localized-strings + asset-path manifest** (`stringtables\\*.zip`, `tracks\\colorado\\trackrouteNNN.xml`, `aiopenworld.zip`, `gameobjs.xml`, `collobjs.xml`, `physics.zip`, etc.). Localized substrings include Spanish/Italian/Russian/Japanese/Polish text. Truth-coord scan in this region: 0 hits.

### 7. RTTI class mining — chunk-loading architecture recovered
`scripts/xex_rtti_classes.py` — extracted every `.?AV…@@` mangled C++ class name (6796 total).

**Chunk-loading classes** (these describe HOW chunks load — they don't store coords):

| RTTI name VA | Class |
|---|---|
| `0x832c5448` | `CFileChunkResource` |
| `0x832c5074` | `CFileChunkResourceType` |
| `0x83237640` | `CPreLoadResourceType` |
| `0x83237664` | `CStreamedDataResourceType` |
| `0x8323768c` | `CTrackModelResourceType` |
| `0x832376b4` | `CTrackPVSZoneResourceType` |
| `0x83237768` | `CTrackSHZoneResourceType` |
| `0x832b74d8` | `CRouteChunk@Navigation` |
| `0x832317cc` | `CLoadingRequestFreeRoam` |
| `0x83244a1c` | `CTrackRouteManager` |
| `0x83236b28` | `CTrackProceduralGeometryResourceType` |

**Placement-related classes** are mostly UI/cutscene cruft (`CPlaceCarAtHub`, `CCutscenePlaceCarTrigger`); none look like a static-placement-table consumer.

### 8. Vtable scan — 4667 vtables found, 221 linked to a class
`scripts/xex_vtable_scan.py` — recovered every C++ vtable in `.rdata` by detecting runs of consecutive `.text`-pointing BE u32s. Linked 221 vtables to mangled class names via the COL `[-1]` slot (Ghidra's MSVC RTTI analyzer is x86-only and doesn't recover PPC vtables automatically).

**Useful entry points** in the linked set: `CSkillsFreeRoamChallenge` vtable @ `0x82022ea4`, `CAllocatedDynamicRoute@AIOpenWorld` vtable @ `0x8222c42c`, `IDynamicRouteBuilder@AIOpenWorld` vtable @ `0x8222ae1c`.

## Cross-check vs `fh1_landmarks.txt` "missing" decoratives

The hand-annotation file claims 7 decoratives need manual placement. Cross-checking against `out/v42k7_dump.txt` (1949 instances, 168 unique tags):

| Tag | In v42k7? |
|---|---|
| `OBJ_BarrierBrand` | **✓ present** (`OBJ_BarrierBrand_LOD00_`, `_LOD01_`) |
| `GrandstandStraight` | **✓ present** (`GrandstandStraight_LOD00_`) |
| `Smelter` (PLA_SMELTER_*) | **✓ present** (`PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00`) |
| `PLA_GC_GolfCart` | ✗ absent (also no `cart`, `Buggy`) |
| `PLA_FARM_WindmillWood` | ✗ absent |
| `GLOB_STFN_WindTurbine_02` | ✗ absent |
| `STFT_Reservoir_Haybale` | ✗ absent |
| `FP_AutoshowTent` | ✗ absent (no `tent`, `Autoshow`) |

**3 of 7 hand-annotations are redundant** — those assets are already extracted. The remaining 4 (golf cart, windmill, wind turbine, haybale, autoshow tent) are absent from every checked source.

## Where the missing 4 most likely live

Given everything above, the missing 4 cannot be in the xex (proven) and are not in any extracted `.bin` we read. Most likely sources:

1. **Unparsed sub-blob sections of rmb.** Memory note `project_subblob_walk_findings.md` says the sub-blob walk was negative *with the structure assumptions made then*. With the `_NNN`-suffix world-baked-siblings insight, it might be worth a re-run — but only on rmbs that contain the missing tag families. (Diff against v42k7's 168 unique tags to find rmb sections we drop.)
2. **Procedurally generated at runtime** from some scattered-anchor list — possibly the `Festival_Area*` or `STFT_*` anchors we already have but don't densify. (Per memory: `STFT_` family doesn't exist as a string in the xex either, though, so this is unlikely.)
3. **DLC content** loaded from a file outside this xex (`aiopenworld.zip` / `gamemodes.zip` mentioned in the overlay manifest are candidates).

## Recommendation

1. **Drop the 3 redundant entries** from `fh1_landmarks.txt` (BarrierBrand, GrandstandStraight, Smelter). These are already in v42k7 — adding them again creates duplicates.
2. **Keep the manual fallback** for the 4 truly-missing decoratives until DLC zips / unparsed rmb sub-blobs are checked.
3. **Stop xex investigation for placement coords** — the binary genuinely doesn't contain them. Evidence is overwhelming across 7 independent scan strategies.
4. **If continuing for chunk-loader internals** (e.g. to validate rmb parsing), the entry points are `CTrackModelResource::Load` and `CFileChunkResource::Load` — vtables are scannable via `scripts/xex_vtable_scan.py`. RTTI string positions documented above.

## Files produced

- `scripts/xex_truthscan.py`, `xex_truthscan_alt.py`, `xex_chunk_local.py`, `xex_static_inventory.py`, `xex_blob_hunt.py`, `xex_overlay_inspect.py`, `xex_rtti_classes.py`, `xex_vtable_scan.py`, `xex_vtable_walk.py`, `xex_asset_strings.py`
- `scripts/ghidra/post_import_recon.py`, `rtti_recovery.py`, `list_classes.py` (PyGhidra)
- `docs/xex-walk/03-ghidra-recon.txt` (string xrefs + FP-density function ranking from Ghidra)
- `docs/xex-walk/04-rtti-classes.txt` (6796 RTTI class names, bucketed)
- `docs/xex-walk/05-vtable-walk.txt` (RTTI→COL→vtable walk for 14 named classes)
- `docs/xex-walk/08-vtable-scan.txt` (4667 candidate vtables, 221 class-linked)
- Ghidra project: `/run/media/paths/SSS-Core/ghidra-projects/fh1xex` (analyzed `default.bin`, 231 s analysis runtime, available for further work)
