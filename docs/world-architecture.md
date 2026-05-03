# FH1 Colorado world architecture — chunk streaming, rmb pool, placement sources

Synthesis of everything we know about how the shipped Colorado world
is structured. Read this first before any per-format doc — most of
the per-file weirdness in `bin.zip` makes sense only against this
backdrop. For per-format internals, jump to:

- `format.md` — bin.zip / LZX framing
- `rmb-bin.md`, `rmb-subblobs.md` — rmb pool entry layout
- `pgeo-body.md` — PGEO header + body classes
- `v42k7-section-mesh-selection.md` — v42k7 section/handle selectors
- `landmark-placement-investigation.md` — landmark placement rules
- `state-of-extraction.md` — pipeline status

---

## TL;DR

1. **The world is chunk-based streaming**, not a flat scene. Two tiers:
   - *Terrain*: a perfectly uniform 128 × 128 m grid (444 cells, only
     populated tiles emit a chunk).
   - *Props (v42k7)*: ~14k irregular AABBs averaging ~300 m on a side,
     scene-authored to hug clusters of related geometry. They overlap
     heavily — every map point sits inside ~6 chunk AABBs on average,
     up to 9–10 in dense zones.
2. **The "rmb pool" (102,012 entries) is the flat union of every
   chunk's *local* rmb table.** When the same asset is visible from
   N chunks, it is baked into N chunks' tables and surfaces as N
   separate rmb pool entries with the same tag and the same world
   centroid. This is a design decision (streaming atomicity, see §3).
3. **The exporter chunkified an originally global scene.** Source
   coords were authored once; the chunkifier stamps the same coords
   into every chunk that needs the asset. We never see "shifted"
   copies — we see byte-identical duplicates at identical positions.
4. **To recover the original scene we have to undo the chunkification.**
   The right primitive is *primary owner chunk*: the smallest AABB
   that contains an asset's centroid is the chunk that "really" owns
   it; rmb entries with the same asset in *other* chunks' tables are
   visitor / streaming-prefetch refs and should be dropped.
5. **Two independent placement selectors** in v42k7 — section-level
   ("is this section a real placement or a streaming impostor?") and
   within-section ("which of the 1–3 handle slots to render?"). The
   first is solved (`cull_box[0] >= 80`); the second is open.

---

## §1. The chunk grid is two-tier

| variant | chunks | typical size | grid? | role |
|---|---|---|---|---|
| terrain      | 444    | 128 m × 128 m (exact) | 100 % grid-locked | heightfield tiles + lightmap UVs (`CProceduralLightMaps`) |
| v42k7        | 14,122 | ~300 m × ~300 m (varies ±10 m) | 0.8 % snap → irregular | prop / scene chunks (`CProceduralModels`) |
| light_glows  | 3,316  | 2–20 m | 7 % snap → per-light AABBs | light entity placements (`CProceduralLightGlows`); was misnamed `vegetation` until 2026-05-03 |
| crowd        | 8,544  | varies | varies | NPC walk volumes (`CProceduralCharacters`) |
| grass        | 11,292 | small | varies | foliage scatter (`CProceduralVegetation` — actual flora) |
| landmark_anim| 447    | varies | varies | festival rides + animated landmarks (`CProceduralAnimatedObject`) |
| v44k5        | 1,578  | varies | varies | FX emitters (likely `CProceduralPoints`); undecoded body |

Per-chunk AABB lives in the PGEO header at +0x14..+0x2c; we extract
them in `world.py` and persist as `out/world/ribbon_00.json`.

The terrain grid is what your intuition expects: floor(x/128) gives a
chunk index. The prop grid is **not** like that — it's hand-authored,
overlapping, and varies in size to match scene density.

---

## §2. The rmb pool is a flat union of per-chunk rmb tables

The 102,012 rmb pool entries in Colorado are NOT a global asset
library. They are the concatenation of every chunk's local rmb table.
Same asset visible from N chunks → N pool entries with identical tag,
identical world centroid, identical mesh bytes.

Verified empirically: the 4 closest `PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00`
entries to the user-truth smelter location are pool handles 6395,
17333, 19447, 27277 — all at centroid (-1958, 94, -2616), all
vc=5367, all tri_count=10596. Different pool slots, byte-identical
content.

### §2.1. Why the exporter ships duplicates

Three streaming-engine reasons fit the 360-era target:

1. **Streaming atomicity.** Engine reads ONE compressed blob from disc
   per chunk activation, decompresses, and has everything that chunk
   draws. Cross-chunk indirection would require either keeping
   neighbor blobs loaded forever or doing extra disc seeks. Disc seeks
   on a 360 are an order of magnitude worse than disc bytes.
2. **Cache eviction independence.** When chunk A leaves the streaming
   window, its entire rmb pool slice can be freed without checking
   reference counts against B, C, K. Per-chunk full copies → simple
   memory management.
3. **Predictable per-chunk LOD bake.** The exporter could in principle
   bake a slightly different LOD into each chunk's copy of the same
   asset (close-up in the owner chunk, lower-poly in distant chunks).
   We have not observed this in the LOD00 entries we've inspected
   (vcount matches across copies), but the format permits it.

### §2.2. Asset categories in the rmb pool

Across all 102k entries, four functionally distinct pool-entry classes
appear:

| class | tag pattern | centroid | role | what to do |
|---|---|---|---|---|
| **A. World-positioned named asset** | real-asset prefix (`PLA_`, `MOUN_`, `MAINTOWN_`, `MT_`, `OBJ_`, `REDROCKS_`, `BLDG_`, `FOOTHILLS_`, etc.) | > 100 m from origin | the actual rendering target for the named asset | KEEP — primary placement |
| **B. Authoring source** | same tag pattern as A | < 100 m from origin (asset-local space) | the artist's authored mesh in asset-local coords; library reference, never directly drawn | DROP unless we have a transform to put it at world position |
| **C. Per-chunk visibility wrapper** | `Object NNN_LOD\d+`, `BUILDINGS_NNN_NOLOD`, `GLOB_*`, `Hub NNN`, `Mesh NNN` | world-positioned at the wrapping chunk's centroid | scene aggregation: bakes multiple foreign asset meshes (each as a named section) at world coords for one chunk | KEEP only if wrapper is in-zone for one of its baked sections (verifiable via §4 ownership) |
| **D. Occluder / shadow proxy** | `Box NNN_LOD\d+` | world-positioned at a chunk centroid | low-poly aggregation used by the engine for shadow-casting math and z-buffer rejection, NOT visible rendering | DROP universally |

Implementation: `src/fh1_mapdecomp/rmb_world.py:_is_named_asset_tag`,
`_is_phantom_wrapper`, `_policy_drop`.

---

## §3. The exporter chunkified globally-authored data

The "why are there 9 copies at the same coords" question has a clean
answer: **the source scene was global, the export was per-chunk**.

```
SOURCE (Maya / 3DS / Turn10's tools, not shipped):
    one global scene
    smelter at world (-1958, 94, -2616)

EXPORT PIPELINE (Turn10WORLD2 or similar):
    for chunk in spatial_chunks:
        for asset in scene:
            if chunk.aabb intersects asset.visibility_radius:
                bake asset into chunk.rmb_table at asset.world_coords
                # exact world coords, not transformed

bin.zip (what we have):
    chunk_1.payload contains [smelter at (-1958, -2616), ...]
    chunk_2.payload contains [smelter at (-1958, -2616), ...]
    ...
    chunk_9.payload contains [smelter at (-1958, -2616), ...]

rmb_pool (flat union we extract):
    [..., smelter, smelter, smelter, ..., smelter, ...]   # 9 entries, same tag, same coords
```

The same pattern applies to landmarks, modular buildings, race-event
geometry, anything visible across chunk boundaries. The denser the
scene, the more redundancy.

Empirical signature: **same vcount + same tag + same centroid** across
multiple pool handles → flat-union duplicate.

---

## §4. Recovering the global scene: chunk-AABB ownership

Pure positional dedup is necessary but not sufficient (it picks an
arbitrary winner among same-position entries; sometimes the wrong
asset wins on tiebreak). The right primitive is **primary owner chunk**:

> The smallest v42k7 chunk AABB that contains an rmb entry's world
> centroid is the chunk that "really" owns the asset. All other
> chunks containing the same point are visitors and their copies of
> the asset are streaming-prefetch refs.

Algorithm:

1. Build an R-tree (or grid bucket) of all 14k v42k7 chunk AABBs.
2. For each rmb pool entry with world centroid:
   a. Query the R-tree for chunks containing that point.
   b. Among containing chunks, select the smallest AABB → primary owner.
   c. Among rmb entries with the same `(stripped_tag, primary_owner)`,
      keep one. Drop the rest.
3. For Object/BUILDINGS/GLOB anonymous wrappers with foreign sections:
   - Each foreign section's stripped tag has its own primary owner
     (from the named-asset's world position, recorded in pass 1).
   - If the wrapper's owner chunk is the same as (or contains) the
     section's primary owner → keep this section emission.
   - Otherwise → drop (visibility-buffer ref to a faraway asset).

Coverage:
- Resolves stacked-copy duplication (§2.1) — collapses N → 1 per asset.
- Resolves "wrong asset on top of right position" when the wrong asset
  is a visibility-buffer wrapper (§2.2 class C) baking foreign geometry
  far from where it belongs.
- Does **NOT** resolve within-section selector mystery (§5.2) — that's
  a separate problem.

This is what was wrong with my earlier 500m-radius heuristic: arbitrary
threshold instead of the actual chunk AABB. The chunk AABB is the
ground truth.

---

## §5. Two independent placement selectors in v42k7

v42k7 chunks are the primary prop placement source. Each chunk has:
- A **section list** (1–N sections, each with 1–3 rmb pool handle slots)
- A **Table B** (per-section attributes including `cull_box`)
- A **position table** (96 B per instance, world position + rotation
  + per-section uniform constant)

Two distinct decisions the engine makes per section:

### §5.1. Section-level: "render or streaming-only?" — SOLVED

`cull_box[0]` (f32 at +0x34 of each Table B record):
- `0` → metadata-only (instance_count == 0)
- `60` → streaming impostor / sentinel-bbox group; do NOT render
- `>= 80` → real placement; render

For u0=1 sections (direct render), cull_box just sets a per-section
distance LOD band and the gate doesn't apply.

Implementation: `_U0Z_CULL_THRESHOLD = 80.0` in
`src/fh1_mapdecomp/v42k7_inst.py`.

### §5.2. Within-section: "which of the 1–3 handle slots to render?" — SOLVED (RENDER mask)

A section that survives §5.1 carries 1–3 rmb pool handles in its slot
list. Empirically this is a *group* — `[Rail_LOD00, Rail_LOD01,
Billboard]` say — and the engine renders a subset of them.

**The selector is the trailing u32 at offset +0x54 of each 88-byte
Table B record (BE u32). It is a per-section *slot RENDER mask*: bit N
set = RENDER slot N. After applying the mask, the LOD-pair-leg-keep
filter drops the LOD01 sibling when both LOD00 and LOD01 are rendered.**

Initially documented (and shipped) as a SKIP mask, but visual ground
truth from the user shows that's the inverse. Three pieces of evidence:

1. **Festival barrier section** chunk 1463 sec[5]: mask=`0x05` slots=3
   `[Decals393, BarrierMetal_LOD00, BarrierMetal_LOD01]`. Under SKIP
   mask: render slot 1 (BarrierMetal_LOD00). Under RENDER mask: render
   slots 0+2 → LOD-pair dedup drops slot 2 → render Decals only.
   The user-supplied festival screenshot shows the inner festival
   circle is largely empty — only road decals visible, no inner-circle
   barrier. → RENDER mask matches reality.
2. **Fence sections** chunk 06788 sec[6]: mask=`0x3` slots=2
   `[BarrierBrand_LOD00, BarrierBrand_LOD01]`. Under SKIP mask: render
   none. Under RENDER mask: render both → LOD-pair dedup drops LOD01
   → render LOD00 fence. The user reports "should be fences" along
   the road, fences are missing under SKIP mask interpretation. →
   RENDER mask matches reality.
3. **Wrong-asset cases** chunks 1462 sec[4] and 1884 sec[7]: mask=`0x4`
   slots=2 → in-range mask = 0 → render none under RENDER mask.
   Drops the corner07/Modular_003 emissions the user flagged as
   wrong. → RENDER mask matches reality.

Common patterns:
- mask=`0x0`: render nothing (streaming-only metadata, very common —
  the bulk of mask=0 sections are NOT real placements)
- mask=`0x4` on 2-slot section: in-range = 0 → render nothing
- mask=`0x3` on 2-slot LOD-pair: render both LOD00+LOD01 → LOD-pair
  dedup keeps LOD00
- mask=`0x5` on 3-slot decal+LOD-pair: render slots 0+2 → LOD-pair
  dedup keeps slot 0 (decal); slot 2 (LOD01) dropped. The high-detail
  rendering must come from a different section (or from rmb_world's
  named-asset wrapper).

For 1-slot sections, the field is sometimes re-purposed (occasionally a
float ~1.0). Restrict to the section's actual slot count via
`mask & ((1 << slot_count) - 1)`. Bits beyond slot count are silently
dropped (effectively non-existent slots are not rendered).

The previous "high-bit deactivation" rule (deactivate the entire
section if any bit set beyond slot_count) is a side-effect of the
RENDER mask interpretation: high bits address non-existent slots, and
in-range mask becomes 0 → render none. Same outcome via the simpler
RENDER mask rule.

Earlier candidates (section_constant, u3/u4, runtime_ptrs[1] low
byte, position-table +0x2c pad, +0x3c/0x4c/0x5c rotation gaps) were
ruled out by direct probe — the rotation-row gaps actually carry
per-instance distance/cull constants (~150.0 and ~100.0 floats), not
slot indices.

Implementation: `V42k7TableBRecord.slot_skip_mask` is parsed by
`parse_table_b`, used in `extract_v42k7_instances` to filter the
section's slot list before emission.

---

## §6. The cross-source dedup story (v42k7 vs rmb_world)

Once §4 and §5.1 are applied, two pipelines emit overlapping
placements:

- `rmb_world` emits each pool entry's geometry at its centroid.
- `v42k7_inst` emits per-instance positions for sections that survive
  the cull-box gate.

When an asset has a world-positioned named-asset rmb (rmb_world emits
it correctly), the v42k7 references to that same handle are
streaming-visibility hints, not render targets. We dedup v42k7 vs
rmb_world by stripped tag — drop v42k7 emissions whose stripped tag
matches an rmb_world blob. ~21k Colorado instances of cables, decals,
smelter components, etc. are deduped this way.

Implementation: `cmd_v42k7_inst` reads `rmb_world/index.json` first,
passes the tag set as `exclude_tag_keys` to `extract_v42k7_instances`.
`cmd_all` reorders so rmb_world runs *before* v42k7_inst.

---

## §7. Status of recovery (what works, what's still off)

What works as of the chunk-streaming write-up:

- Terrain — clean LOD00 grid, no duplication issues.
- Named-asset rmbs at world position — emit at the right place. Verified
  against user-truth coords for warehouse (75 m from truth), city
  parking complex (177 m), Red Rocks amphitheatre walls (33 m).
- v42k7 cull-box gate filters streaming-impostor sections.
- Box NNN occluder proxies dropped universally.
- v42k7 vs rmb_world cross-source dedup eliminates ~21k cable/decal
  duplicate refs.

What still produces visual artifacts:

- "Stacked copies" of the same asset at the same world point (chunk-
  AABB ownership work in progress; see §4).
- Phantom wrapper rings when an Object NNN wrapper at a non-asset
  position bakes foreign geometry. Partial dedup via cross-pool
  500m-radius heuristic; full fix requires §4.
- Within-section "wrong asset on top of right asset" when v42k7
  sections list multiple handle slots (§5.2 unsolved).
- Authoring sources for some landmarks (observatory, amphitheatre,
  mining tower, gondola) only exist as origin-bound asset-local
  rmbs — they need a transform to render at world position. The
  transform's source is unknown (likely an xex lookup table).

User-supplied truth coords in `docs/placements.txt` give a reference
set to score against. `scripts/check_placements.py` produces the
per-landmark distance summary; `scripts/export_placements.py` dumps
the entire emission set for inspection.
