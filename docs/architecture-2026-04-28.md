# FH1 World Data — Engine Architecture (Reconstructed 2026-04-28)

This document consolidates everything we've reverse-engineered through the
2026-04-28 session about how Forza Horizon 1 (Xbox 360, PowerPC BE 32-bit)
stores Colorado world data, organized as we'd describe the system from a
Turn 10 / Playground Games engine engineer's perspective. Treat this as a
running ground truth for future investigation. Anything tagged ★ is a
high-confidence claim with concrete evidence; anything tagged ☐ is best-guess
or partially supported.

---

## 1. Design constraints (Xbox 360, 2012)

These shape everything below:

- **512 MB unified RAM.** No room to keep the full Colorado map resident.
  Streaming budgets per chunk are tight (low-MB).
- **DVD streaming at ~50 MB/s with 100+ ms seek.** Predictive prefetch
  matters; cross-chunk references must be pre-resolved at chunk-build time,
  not chased at runtime.
- **No virtual memory.** Page faults aren't an option; every cross-chunk
  asset reference has to be preloaded via an explicit dependency manifest.
- **GPU drawcall budget is precious.** Engine wants to batch — same VB,
  same material, same shader — to minimize state changes per frame.
- **Physics on Xenon CPU, separate from rendering.** Collision queries are
  decoupled from draws and can use a side-channel data layout.

These constraints force the engine to split world data across multiple
specialized stores, which is what we observe.

---

## 2. The five-store world architecture

★ The data we've inventoried fits cleanly into a "library + N placement
sources" model:

| # | Store | Role | Authoritative for |
|---|-------|------|-------------------|
| 0 | **rmb pool** | Library of meshes (102,012 entries; ~5–17× duplication per asset) | All geometry |
| 1 | **rmb_world** | Pre-baked world placements (~6,842 with `policy=all`) | Unique landmarks: smelter, dam, observatory, theatre, fountain, mainstage |
| 2 | **v42k7 chunks** | Per-region streaming bundles with transform tables | Repeated tiles/props: modular town, decals, cables, "batch templates" |
| 3 | **collobjs (CollObjs.xml)** | Physics-side placement table (21,877 entries) | Collidable props ONLY: signs, fences, marker poles, picnic tables, benches, ArmcoArrows, speed signs |
| 4 | **animatedobjects.zip** | Per-event placement table for animated assets (24/290 ANIMs world-placed) | Race events, autoshow lights, ski-lift cablecars, plane banners |
| 5 | **chunk VBUF (open)** | Soup-mesh embedded in each v42k7 chunk's tail (79 records × 40 B per chunk) | ☐ Likely: small decorative props (golf carts, haybales, grandstands, BarrierBrand fences) |

★ Stores 1–4 are confirmed. Store 5 is our strongest hypothesis for where
the missing decorative props live; investigation is the next step.

### Library duplication

★ The rmb pool is a **flat union of every chunk's local rmb table**. Same
asset in N chunks = N pool entries. So `PLA_GC_GolfCart_LOD00` appears 17×
in the pool, all near-origin (asset-local space), all with the same mesh.
This is intentional: it lets the engine stream a chunk independently
without chasing cross-chunk references.

### Why the world-placed split exists

★ When an asset is unique and hand-placed (the dam mesh, a specific blast
furnace at the smelter), the asset compiler bakes the *world transform*
into the mesh's vertex buffer. That entry's centroid sits >20m from origin.
The engine renders it with no per-instance matrix — just a draw call.

When an asset is repeated (modular wall, fence segment, decal), baking
N copies wastes disk and RAM. Instead, ship one library entry + a per-chunk
transform table.

The 20m centroid threshold is what `rmb_world` uses as its discriminator;
above 20m → world-placed → emit; below → library-only → skip.

---

## 3. The chunks (v42k7) in detail

★ Confirmed layout (see `docs/world_data_tree.txt` for offsets):

```
[0x00..0x34]   OEGP common header     version, kind, scale, world bbox, name
[0x34..0x60]   body header            section_count, table_a_count, vbuf_size
[0x60..0x88]   chunk name             "Models_Ungrouped_NNNN" (40B C-string)
[0x88..]       section records        section_count × 64 B:
                                        +0x00..0x18  6 BE floats (local bbox)
                                        +0x1c..0x34  3 (count, ptr) pairs
                                                     pointing into section_handles
                                        +0x34..0x40  ★ NOT pure padding —
                                                     ~3,576 sections have data
                                                     here. Heterogeneous: extra
                                                     handles in some, coord-shaped
                                                     floats in others.
[after sections]
   section_handles array              N u32 BE rmb-pool handles
   Table A                            8B records (handle, pad)
   Table B header (16B) + 0xFFFFFFFF
   Table B records (88B + FFFFFFFF separator)
                                        +0x20..0x34  5 u32 sequence
                                        +0x34..0x44  cull_box (4 f32):
                                                     ★ cull_box[0] >= 80
                                                       = real instance,
                                                     < 80 = streaming impostor
                                        +0x48..0x54  3 LE runtime ptrs
                                        +0x54        ★ 'slot mask' (open;
                                                       see §3.2)
[position table]                      per-section instance_count × 96 B:
                                        +0x00..0x0c  pos (3 f32 BE world-space)
                                        +0x0c..0x10  w = 1.0
                                        +0x10..0x20  4 f32 section_constant
                                        +0x20..0x2c  3 f32 up vector
                                        +0x2c..0x30  ★ 4 bytes — confirmed
                                                       no per-instance picker
                                        +0x30..0x40  3 f32 row 0 + 4B pad
                                        +0x40..0x50  3 f32 row 1 + 4B pad
                                        +0x50..0x60  3 f32 row 2 + 4B pad
[separator]                           32 × separator_mult bytes between sections
                                      ★ Carries LOD distance thresholds for
                                        some sections (e.g. 300, 2500, 220, 10).
                                      ★ Confirmed NOT per-instance slot indices.
[trailing VBUF]                       79 records × 40 B per chunk:
                                        +0x00..0x04  often 0 (sometimes 5.0)
                                        +0x04..0x08  packed position
                                                     (10:10:10 + 2 bits)
                                        +0x08..0x20  un-decoded (~24B)
                                        +0x20..0x22  anchor 0xe3 0xd0
                                        +0x22..0x28  un-decoded (6B)
                                      ★ STRONGEST OPEN LEAD —
                                      see §6.
```

### 3.1 What "section" / "group" means

★ A v42k7 section is a logical group inside the chunk:

- **slot list**: 1–3 rmb-pool handles (the "candidate batch")
- **instance_count** transforms in the position table (real world-space coords)
- **+0x54 mask byte** (semantics open; see below)
- **separator block** holding LOD distance thresholds per section

The position table transforms are real placements in world space. We've
verified this against multiple landmark POIs and the smelter truth coord.

### 3.2 The slot picker mystery

★ Three competing rules that each fit some sections but generalize to none:

| Rule | Example fit | Counter-example |
|------|------------|-----------------|
| RENDER mask: `bit N set = render slot N` | Festival barriers (mask=0x3 LOD pair) | mask=0x3f4cae43 high-bit-deact → drops everything |
| Highest-set-bit | Bundle sections like (cables, Modular_003, Modular_004) where spacing matches Modular_004 width | LOD pairs would pick the impostor (LOD01) instead of LOD00 |
| Always slot 0 | Festival entrance speed cameras at user truth | Modular_003/004 then never render where the data shows them |

★ The masks are **heterogeneous**:

- Small byte values (0x1, 0x4, 0x6) — looks bit-mask-shaped
- Float-shaped values (0xbf4cae43 ≈ -0.8, 0x3f3d332b ≈ 0.74) — looks like a
  cosine, dot product, or normalized weight
- Monotonically increasing per chunk (0x1, 0x1, 0x3, 0x4, 0x4, 0x5, 0x5, ...) —
  could be a sub-section sequence number, not a mask

★ The +0x54 field is overloaded. Different section types use it differently.

### 3.3 What we've ruled out for the picker

★ Already tested negative:

- Per-instance picker hidden in the 96B transform record's "padding"
  (+0x2c, +0x3c, +0x4c, +0x5c) — bytes are 0 or constant per section
- Per-instance picker in the inter-section separator (it's LOD distances)
- Section header floats (`attr_floats`, `extra_floats`) being a per-slot
  count split — only 2/30 sections had a 3-field-sum match `instance_count`
- The `(count, ptr)` pair count being a slot index — every active pair has
  count == 1; never anything else

### 3.4 Bbox is not position

★ **Critically important**: a v42k7 chunk's PGEO header bbox **is NOT the
chunk's world placement**. The smelter is at game (-1961, -2593); 32 v42k7
chunks reference smelter assets in their slot lists, but ALL 32 have bbox
centers 1745–5119m away from the smelter (median 4045m). The slot list is
a *streaming dependency manifest* (preload these handles when this chunk
activates), not a placement list.

The position table is the placement list. Its transforms are world-space.

---

## 4. The five stores in detail

### 4.1 rmb_world (Store 1)

- ★ 6,842 placements with `policy=all`
- ★ Chooses pool entries with centroid >20m horizontal magnitude
- ★ Verified correct at every truth POI (smelter, dam, observatory, theatre,
  fountain, mainstage, golf course, ranch, eagle ridge — see
  `docs/fh1_pois.txt`)
- ★ Subset of pool (71,570 world-placed of 102,012 total)
- ☐ The 30,442 "skipped_local" entries are library entries — not placement
  candidates for this store

### 4.2 v42k7 (Store 2)

- ★ 13,926 unique v42k7 chunks (de-duped by case-insensitive filename;
  bin.zip stores ~5–8× duplicates of each)
- ★ Position tables hold real world-space transforms
- ★ Slot lists are streaming/dependency manifests, NOT direct model
  placement
- ☐ Currently disabled in the conf (`--no-v42k7-inst`) because the slot
  picker produces wrong-asset clusters (warehouse rack-shelving at the dam,
  Modular_004 along farm roads)
- ☐ Some v42k7 transforms ARE real model placements; figuring out which
  is the open problem

### 4.3 collobjs (Store 3)

- ★ `Ribbon_00/CollObjs.xml`: 21,877 placements
- ★ Resolves to 48 distinct rmb tags via type→handle mapping
- ★ Type vocabulary: `CO_CLRD_FenceF` (3829), `CO_FEST_EquipSign_001`
  (2743), `CO_MarkerPole_001` (1162), `CO_SignG_001` (988), `CO_Table_Picnic`
  (901), `CO_CLRD_BinB` (711), `CO_CLRD_FlowerholderA` (710), `CO_Bench_001`
  (585), etc. — **all collidable**
- ★ User-confirmed mental model: "collobjs = objects with collision"
- ★ 14,397 unresolved entries — XML names that have no rmb pool match
  (these are likely stale/build-time asset references that didn't ship)
- ★ Decorative non-collidable props are NOT in collobjs (verified for golf
  cart, haybale, wind turbine, grandstand, BarrierBrand)

### 4.4 animatedobjects.zip (Store 4)

- ★ 290 ANIM_*.pgeo files; only 24 have non-origin world bboxes
- ★ World-placed ANIMs are all: race events (Showcase_Event_*, P51_Mustang,
  Helicopter, Biplane), plane banners, autoshow tent lights, ski-lift
  cablecars (which DO place the gondola at ~(605, 477)), crop dusters
- ★ Asset-local ANIMs include `ANIM_WindTurbine_001`, `ANIM_FARM_Windmill[Metal|Wood]`,
  `ANIM_Autoshow_Lights*` — meshes exist but world placement transforms are
  NOT in this zip
- ★ Race-only content; not a placement source for static landmarks

### 4.5 chunk VBUF (Store 5 — open)

- ★ Each v42k7 chunk has a trailing 79 × 40 B vertex buffer
- ★ Anchor `0xe3 0xd0` at offset 32 of every record (validates layout)
- ★ Per-record structure: `(unknown_4B, packed_pos_4B, ~24B unknown,
  anchor_2B, ~6B unknown)`
- ☐ Currently treated as decode-preview point cloud (see legacy
  `decode()` in `src/fh1_mapdecomp/pgeo_body/v42k7.py`)
- ☐ **Hypothesis**: this is the chunk's *local soup mesh* — vertex-baked
  geometry for small decorative props (haybales, golf carts, grandstands,
  BarrierBrand fences) drawn as one drawcall per chunk
- ☐ This would explain why golf carts/haybales are in the rmb pool as
  library-only with no world placement found in any of stores 1–4: their
  geometry is rendered directly from the chunk VBUF, not as instances
- See §6 for next-step plan

---

## 5. What we've ruled out (don't re-investigate)

| Source | Status | Notes |
|--------|--------|-------|
| `Colorado_track_00.col` | ★ Closed | Index only; pool built at runtime |
| `colorado.owr` / `.oww` | ★ Closed | 12 / 32 byte sentinels |
| `Colorado_track_00.crowd` | ★ Closed | 12 byte stub |
| `Colorado_00.pvs` / `*.pvsz` | ⚠️ **Re-opened 2026-05-03** | These are the engine's authored placement table, not a visibility tree. Decoded by `pvs.py` (port of Doliman100/austinbaccus FH1 fork). See `pvs-format.md`. |
| `PVSZLookup_00.dat` | ★ Closed | Runtime zone-hash → slot lookup; not needed for static placement now that `pvs.py` decodes the table directly. |
| `ParticleEmitters.xml` | ★ Closed | Emission points, not anchors |
| `PostProcessingZones_Safe.xml` | ★ Closed | Post-FX bounds |
| `GameObjs.xml` | ★ Closed | Barn finds, gas stations, race spawns |
| `TrackRoute*.xml` | ★ Closed | Race-event NamedTransform |
| `FilenameMap_00.dat` | ★ Closed | File-name strings only |
| `Colorado_00.hex` (HEXY) | ★ Closed for placements | 91×60 far-LOD impostor index |
| `co_cone.bin` / `PhysicsDefinitions.bin` | ★ Closed | Physics types, no placements |
| `db/gamedb.slt` | ★ Closed | SQLite, all geo tables track-relative |
| `aiopenworld.zip` (.owt) | ★ Closed | OWTM AI traffic waypoints |
| `airborne_challenges.xml` | ★ Closed | Race-event timing config |
| `SmashableObjectTypes.xml` | ★ Closed | 4 type strings only |
| `.fiz` files | ★ Closed | Foliage cells, header decoded |
| `bundle` (in bin.zip) | ★ Closed | No placements |
| Sub-blob marker walk in xex | ★ Closed | Negative result |
| `lis`+`addi` immediate scan in xex | ★ Closed | Negative result |
| `nav` / `db` / `co_cone` / hash-search | ★ Closed | All negative |

---

## 6. Open frontier

### 6.1 v42k7 chunk VBUF — INVESTIGATED, RULED OUT

**Hypothesis (closed)**: The 79-record trailing VBUF in each v42k7 chunk is
a soup mesh (one drawcall) rendering small decorative props baked into the
chunk's local geometry — golf carts, haybales, grandstands, fence posts,
etc.

**Result**: Tested all 7 plausible implicit-topology interpretations
(point cloud, tri_list, tri_strip, tri_fan, line_strip, line_pairs, by-
material). All produce visual noise — no recognizable mesh shape under any
topology. Combined with the byte-budget audit showing no hidden region in
the chunk file, **the VBUF is not the chunk's renderable mesh**.

The chunk-internal investigation is closed. See `where-we-stopped.md` for
the consolidated final state.

**Evidence supporting this**:
- Library entries for these props exist in the rmb pool but with near-origin
  centroids (asset-local) — i.e., they're *meshes*, not placements
- Zero v42k7 slot-list references to these handles (so they're not
  per-instance transformed)
- Zero collobjs entries (so they're not collidable)
- Zero animatedobjects entries (so they're not animated)
- 79 records × 13,926 chunks ≈ 1.1M potential prop placements — right
  order of magnitude for "haybales in every field, fences along every road"
- Each record is 40 B — exactly the GPU vertex stride for
  `(packed_pos, packed_normal, packed_uv, color/material_id)`
- Record 0 has a leading non-zero float (often 5.0); rec[1..N-1] start with
  zeros — looks like rec[0] is a header (count? scale? group ID?)

**To verify**:
1. Decode the per-record fields beyond the packed position (+0x08..0x20 and
   +0x22..0x28). Look for color or material IDs varying per record.
2. Render the resulting mesh (positions only) in Blender for a chunk near
   a known POI (e.g. the user's farm-road intersection at (-2299, -103))
   and compare visually to the in-game scene.
3. Check whether record 0 is a header (different shape from rec[1..N-1]).

### 6.2 +0x54 slot mask — semantics

★ Heterogeneous; small byte for some sections, float for others. Possibly
overloaded based on a per-section type discriminator we haven't identified.
Lower priority than VBUF investigation since it may not be solvable from
disk data alone (could depend on runtime predicates).

### 6.3 landmark_anim PGEO bodies

☐ 33 chunks; currently dismissed as Maya rig metadata. One more focused
pass to look for world-coord triples in body could be worth it.

### 6.4 crowd / vegetation / grass PGEO bodies

☐ 5,260 chunks total, bodies undecoded. Unlikely placement source for
static props but possible for procedural foliage / actors.

### 6.5 PVSZoneSpeeds.dat

☐ Never investigated. Probably speed-limit zones.

---

## 7. Truth-checking & POI files

★ User-supplied truth coords for visual cross-reference:

- `docs/placements.txt` — original 9 landmarks (smelter, dam, golf course
  variants, bunker, red rocks observatory/amphitheatre/entrance, warehouse,
  city center)
- `docs/fh1_pois.txt` — newer additions (autoshow tent, eagle ridge,
  fountain, golf carts, mainstage, ranch, theatre, dam, observatory,
  smelters)

★ Checker scripts:

- `scripts/poi_check.py` — for each POI, lists every rmb_world + v42k7
  emission within 150m. Auto-reads `docs/fh1_pois.txt`.
- `scripts/trace_asset.py` — for a tag keyword, finds all rmb pool entries
  + every v42k7 chunk that references the handle.
- `scripts/find_at_target.py` — list every rmb pool entry within RADIUS
  of a target world coord (regardless of tag).
- `scripts/sections_near_point.py` — list every v42k7 section that places
  a transform near a world coord.
- `scripts/sections_with_handle.py` — given a handle, find every section
  that references it, dump slot list + nearest transform.
- `scripts/dump_groups.py` — laid-out section dump for chunks of interest.
- `scripts/decode_hex.py` / `scripts/hex_world_check.py` — HEXY decoder.

---

## 8. Memory of decisions taken (extractor configuration)

★ Current `fh1-mapdecomp.conf` (release-dir override):

```
all
--source /run/media/paths/SSS-Games/fh1-xex
--output ./out
--no-meshes
--no-v42k7-inst        # disabled until slot picker is solved
--rmb-world-policy all # emit everything (even race-only)
--collobjs-policy all  # emit everything
```

Rationale: rmb_world (correct) + collobjs (correct collidable items) +
animatedobjects (race / cablecars). v42k7 disabled until we crack the
chunk VBUF or slot picker. Output: `~7,578 rmb_world + 48 collobjs` blobs
in a 168 MB Blender file.

---

## 9. Why this story is the most coherent so far

Putting on a Turn 10 / 360-engineer hat in 2012:

1. **Library + instancer is the standard memory pattern** for streaming
   open-world. We see exactly that.
2. **Pre-baked landmarks for unique buildings is standard** — shave the
   per-instance matrix multiply, render with no transform.
3. **Streaming dependency manifests are standard** for predictive prefetch
   when DVD seeks are expensive — that's the v42k7 slot list.
4. **Collision in a side-channel** is standard for separating render and
   physics pipelines — that's CollObjs.xml.
5. **Small props baked into chunk-local soup meshes** is standard for
   keeping drawcall count down — that's our hypothesis for the chunk VBUF.
6. **Animated content in a separate pipeline** is standard because skin/
   keyframe shaders have different memory and CPU budgets.
7. **Far-LOD impostor cell index** is standard for horizon culling — that's
   HEXY.

Every store we've identified maps onto a recognizable engine pattern from
the 360 era. The architecture is consistent with experienced TT/Playground
practice. The remaining mystery (the VBUF) sits in the same shape as a
known pattern (chunk-local soup mesh).

This is the working theory we'll use to investigate next, starting with the
v42k7 chunk VBUF decode.
