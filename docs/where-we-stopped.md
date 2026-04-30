# Where the FH1 Colorado map-decomp investigation stopped

Final state as of 2026-04-28. This document is the honest closer.

## What works

- **rmb_world** extracts ~7,578 placements (with `policy=all`). Verified
  semantically correct at every truth-coord POI we checked: smelter, dam,
  observatory, fountain, mainstage, theatre, golf course, eagle ridge,
  ranch.
- **collobjs** extracts ~48 collidable items (signs, benches, picnic tables,
  marker poles, fences-with-collision).
- **animatedobjects** placements for race events / autoshow lights /
  ski-lift cablecars (~24 world-bbox ANIM PGEOs).
- **terrain_hi** extracts the world terrain mesh.
- **landmarks** annotation system (`docs/fh1_landmarks.txt` +
  `scripts/build_landmarks.py`) lets a user manually pin asset placements
  with a "snap to nearest v42k7 transform within radius" mode that
  reduces ±10m memory to ±1m precision.

Together these produce a **mostly-correct Blender scene** of the world with
all major landmarks at correct positions. The renderer recognizes the
world.

## What doesn't work

- **v42k7 chunks** (the bulk per-region data, ~13,926 chunks). The position
  tables are correct world-space transforms, but the slot lists ("which
  models to render at those transforms") are **not** the rendering list —
  they're a streaming dependency manifest. We never figured out the actual
  picker. v42k7 emission is **disabled** in the shipping config.
- **Decorative props** (golf carts, haybales, wind turbines, autoshow
  tents, grandstands, BarrierBrand fences) have **no automatic placement
  source we can find**. They exist as library entries in the rmb pool
  (centroid near origin, asset-local space) but aren't placed by:
  - rmb_world (centroid-near-origin filter excludes them)
  - v42k7 slot lists (zero references in any section)
  - collobjs (collision-only)
  - animatedobjects (race events only, not these)
  - HEXY (far-LOD impostor index; doesn't span golf cart area)
  - chunk VBUF (verified: NOT a mesh; all common topologies produce visual
    noise)
  - xex code (zero asset tag strings; zero truth-coord float pairs;
    even adjacent BE u32 pointers to anchor strings produce zero hits;
    debug printf format strings are physically present but completely
    unreferenced — retail compiler stripped or `#define`d-away every path
    that touched them)
  - any other file class on disk (.fxobj, .bundle, .sh, .pvsz, db, etc.)

## What we tried and ruled out (full inventory)

```
Static placement source for missing decorative props:

✗ rmb_world (pool centroid > 20m)            — library only for these tags
✗ rmb_world sub-blob scan (24,650 sub-blobs) — 425 keyword matches all near origin
✗ v42k7 slot list                            — 0 references to these handles
✗ v42k7 slot list with alternative interps   — slot 0, highest-bit, mask-bit; all wrong
✗ v42k7 chunk VBUF as mesh                   — 7 topologies tested, all visual noise
✗ v42k7 chunk byte budget                    — every byte mapped, no hidden region
✗ collobjs (CollObjs.xml, 21,877 entries)    — collision-only types
✗ animatedobjects.zip ANIM_*.pgeo (290)      — race events / lights / cablecars only
✗ HEXY (Colorado_00.hex)                     — far-LOD impostor index, sparse, doesn't span area
✗ xex code, lis+addi immediate scan          — all anchor strings unreferenced
✗ xex code, BE u32 pointer table scan        — anchor VAs nowhere as bytes
✗ xex code, anchor coord float scan          — truth-coord f32 pairs nowhere
✗ xex code, asset tag string presence         — zero PLA_/BLDG_/SMELTER/etc.
✗ pgeo .fxobj / .bundle / .sh / .pvsz / .fiz — no asset name hits
✗ db/gamedb.slt SQLite (221 tables)          — track-relative only
✗ aiopenworld.zip OWTM .owt                  — AI traffic waypoints only
✗ TrackRoute*.xml                            — race-event NamedTransforms only
✗ GameObjs.xml                               — barn finds / gas stations / race spawns
✗ ParticleEmitters.xml                       — emission points, not anchors
✗ PostProcessingZones_Safe.xml               — post-FX bounds
✗ FilenameMap_00.dat                         — file-name strings only
✗ co_cone.bin / PhysicsDefinitions.bin       — physics types
✗ all loose .bin/.dat/.xpr files in tree     — zero hits
```

## What's actually going on (best theory we have)

The FH1 engine appears to construct prop placements **procedurally at
runtime** from data we can't statically analyze. Most likely candidates
for the "generator":

- A region/biome ID per chunk (or per HEXY cell) → engine-side scatter
  that places haybales/turbines/etc. at procedurally-deterministic
  positions. We'd need to decompile the generator code, which lives in the
  xex but wouldn't appear in any string/coord scan (procedural code uses
  hash inputs, not literal strings or coords).
- A separate per-asset-class placement pass that builds positions from
  some encoded state we don't recognize. We've examined every byte of
  every file class without finding it.

Either way, **the placements aren't on disk in a form we can recover**.

## What you can do with what works

The shipping config already produces a mostly-correct scene:

```
all
--source /run/media/paths/SSS-Games/fh1-xex
--output ./out
--no-meshes
--no-v42k7-inst
--rmb-world-policy all
--collobjs-policy all
```

Output: `./out/blender/colorado.blend` — 168 MB, ~7,578 rmb_world blobs +
48 collobjs blobs + animatedobjects + terrain. Correct landmarks in correct
positions.

Gaps: distant decorative props (haybales, individual fence runs, golf
carts, autoshow tents, grandstand props) won't appear automatically, but:

- The world IS there: roads, terrain, all named landmarks, named buildings
  (smelter, dam, observatory, festival workshops, redrocks, modular
  maintown stuff that IS in rmb_world), highway barriers, signs, picnic
  tables, festival cablecars, etc.
- For specific named landmarks you care about, the **landmark file**
  workflow can pin them precisely:
  ```
  docs/fh1_landmarks.txt
  → scripts/build_landmarks.py
  → out/landmarks/landmarks.blend
  ```
  Append/link this into the main scene. Snap mode aligns to v42k7
  transforms within radius for high precision.

## Why we didn't fully solve it

Three structural reasons, in order of likely contribution:

1. **The retail xex was stripped of debug paths**. Every printf format
   string and assert message is unreferenced. We can see the strings but
   can't trace from them — there's literally no code that touches them.
   This eliminates the most common entry point for static analysis.

2. **The procedural generator pattern**. If a console game procedurally
   spawns decorative content from a region/biome ID, the placements aren't
   data — they're code. Static analysis without symbols / RTTI / decompiled
   code is a wall.

3. **The v42k7 slot picker mystery**. We've cataloged what doesn't work
   (RENDER mask, highest-bit, slot-0-only, mask-byte deactivation), but no
   single rule fits all sections. The picker may be runtime-state-dependent
   (player progression, time of day, biome) and not solvable from disk
   data alone.

## Final verdict

The investigation produced a **functional extractor for ~80% of the
visible world** with high precision. The remaining ~20% (small decorative
props procedurally placed in fields, along farm roads, around festival
support areas) is genuinely off-disk and requires either Ghidra-GUI-level
reverse engineering of the engine code or a hardcoded annotation per
landmark.

The architecture, byte layouts, and data semantics we *did* recover are
documented across:

- `architecture-2026-04-28.md`
- `world_data_tree.txt`
- `file_tree.txt`
- `world-architecture.md`
- `pgeo-body.md`
- `rmb-bin.md`
- `bin-zip-layout.md`
- `xex-walk/01-anchors.md`
- `xex-walk/02-callers.md`

This document. End of investigation.
