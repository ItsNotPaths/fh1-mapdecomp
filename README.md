# fh1-mapdecomp

Reverse-engineered extractor for the Colorado open-world map shipped in
*Forza Horizon 1* (Xbox 360, 2012). Reads a legitimately-extracted disc
tree, reconstructs as much of the world as the on-disk data allows, and
emits a Blender scene plus intermediate JSON/NPZ artefacts.

The companion repo `fh1-memdump` (sibling project, separate tree) handles
the runtime-memory side: dumping Xenia's mapped memory, finding the
chunk-activation buffer, and watching it as the player drives around.

---

## Part 1 — TL;DR

### What this is

Forza Horizon 1's world data does not sit on disc as one neat scene
file. It is sprayed across half a dozen formats inside one giant
`bin.zip` (2.9 GB, 230,057 entries). This project decodes those
formats, applies what we know about how the engine reassembles them,
and dumps the result as a Blender file you can open and inspect.

### Where the data lives

Everything starts at the disc layout
`4D5309C9/00007000/2DC7007B/media/tracks/colorado/`:

- **`bin.zip`** — the giant container. Holds geometry, textures,
  foliage, lighting probes, sound, etc. This is where almost
  everything comes from.
- **`Ribbon_00/`** — a folder of XML and small binary tables. Holds the
  collision-prop list (`CollObjs.xml`), barn-find / race spawns
  (`GameObjs.xml`), the visibility grid, race routes, etc.
- **`default.xex`** — the game executable. We confirmed via Ghidra that
  it does **not** statically store world placement coordinates, so the
  extractor never reads it.

The 2026-04-29 strace breakthrough confirmed `bin.zip` is the engine's
runtime streaming source too: when the player drives across a chunk
boundary the engine `pread64`'s a new entry from `bin.zip` and there is
**no separate "master directory" file** — `bin.zip`'s own central
directory IS the master.

### What comes out

Run the `all` command and you get:

- `out/world/ribbon_00.json` — terrain grid + chunk AABBs + metadata.
- `out/terrain_hi/*.npz` — **2,569 terrain tiles**, full Colorado
  surface with real elevation (-39 m to 838 m).
- `out/rmb_world/blobs/*.npz` — **~7,500 named landmark placements**
  (smelter, dam, observatory, festival mainstage, redrocks, modular
  maintown buildings, Eagle Ridge ranch, etc).
- `out/v42k7_inst/blobs/*.npz` — **~50,000 prop instances** from
  per-chunk transform tables (decals, cables, fences, race-event
  dressing, modular town pieces).
- `out/collobjs_inst/*.npz` — **~7,500 collidable items** (signs,
  benches, picnic tables, bins, marker poles).
- `out/blender/colorado.blend` — the assembled scene. Roughly 168 MB.

### What works, plainly

The world geometry you can recognise: roads, terrain, named buildings,
landmarks, signs, race-event dressing, festival cablecars, festival
animated rides (windmills, fireworks, lasers, etc). All in correct
world positions, verified against user-supplied truth coords for nine
landmarks (smelter, dam, city centre, warehouse, redrocks, etc).

### What is missing

In honest terms, real visual coverage is at **10–20% of
the populated world**, not the 80% earlier docs claimed. What's gone:

- The full *city* (most building density beyond the named landmarks).
- Fence runs along farm roads.
- Decorative props scattered through fields and around the festival
  perimeter — golf carts, haybales, autoshow tents, grandstand props,
  individual wind turbines.

### Why it's missing

Two distinct problems:

1. **The chunk-activation map is runtime-only.** The engine knows which
   geometry-pool entries to load when the player enters a given chunk,
   but that mapping is built dynamically in RAM during world-load. It
   isn't a serialised table on disc, the executable does not
   hard-code coordinates, and the per-asset chunk membership has to be
   recovered either by capturing it at runtime (drive + watcher
   strategy in `fh1-memdump`) or by finding the streaming inputs that
   feed it.
2. **A within-section selector ambiguity.** Each per-chunk transform
   row carries a small list of candidate model handles. We solved one
   of the two selectors (the section-level cull/render gate) but the
   within-section "which of the 1–3 handles to render" picker is
   only partly solved (works on most cases, leaks wrong assets on
   others).

The project has explored every plausible static disc source. They are
catalogued (with negative results) in *Part 2 §6*. The remaining road
runs through runtime memory capture, which lives in the sibling
`fh1-memdump` repo.

### How to run it

```bash
fh1-mapdecomp all \
    --source /path/to/4D5309C9 \
    --output ./out \
    --no-meshes \
    --rmb-world-policy all \
    --collobjs-policy all
```

`--source` accepts either a `bin.zip` path or the extracted disc tree.
`--no-meshes` skips the heavy geometry decode pass for a quick scene
build. The `blender` step shells out to a Blender install on `$PATH`
(or `--blender /path/to/blender`).

For specific landmarks the static path can't recover, the user-driven
**landmark file** workflow snaps hand-pinned positions to the
nearest in-chunk transform within a radius:

```bash
docs/fh1_landmarks.txt    # human-edited annotations
scripts/build_landmarks.py
out/landmarks/landmarks.blend
```

---

## Part 2 — Deep dive

This section captures the architecture, the formats, and the chain of
investigations (with their dead-ends) that produced the current
extractor. Read `docs/world-architecture.md` and
`docs/architecture-2026-04-28.md` for the longer per-format treatments;
this is the synthesis.

### §1. Engine architecture: chunk-based streaming

The 360 has 512 MB of unified RAM, ~50 MB/s DVD with 100+ ms seek, and
no virtual memory. Every design choice we observe falls out of those
constraints.

The Colorado world is divided into **streaming chunks** at two tiers:

| variant       | count   | typical size      | grid?              | role |
|---------------|--------:|-------------------|--------------------|------|
| terrain       | 444     | 128 m × 128 m     | exact grid         | heightfield tiles |
| v42k7         | 14,122  | ~300 m × ~300 m   | irregular, overlap | prop / scene chunks |
| vegetation    | 3,316   | 2–20 m            | per-prop AABBs     | flora |
| crowd         | 8,544   | varies            | varies             | NPC walk volumes |
| grass         | 11,292  | small             | varies             | grass density |
| landmark_anim | 447     | varies            | varies             | festival rides + animated landmarks |
| v44k5         | 1,578   | varies            | varies             | undecoded variant |

The terrain grid is intuitive (`floor(x / 128)` selects a tile). The
prop grid is hand-authored, overlapping, and varies in size to track
scene density — every map point sits inside ~6 chunk AABBs on
average, up to 9–10 in dense zones.

### §2. The "rmb pool" is a flat union, not a global asset library

`bin.zip` contains 102,012 `coloradoout.NNNNN.rmb.bin` entries forming
the geometry pool. **This is not 102,012 distinct assets.** It is the
concatenation of every chunk's local rmb table.

When the same asset (say, the smelter blast furnace) is visible from N
chunks, the exporter bakes it into N chunks' tables. We see it as N
pool entries with **identical tag, identical world centroid, identical
mesh bytes, different pool handles**. Empirically verified: the four
nearest `PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00` entries to the
truth-coord smelter are pool handles 6395 / 17333 / 19447 / 27277, all
at centroid (-1958, 94, -2616), all 5,367 verts / 10,596 tris.

Why the exporter ships duplicates rather than indirection:

1. **Streaming atomicity.** One compressed blob per chunk, no cross-
   chunk reference chasing on a DVD with seek penalties.
2. **Cache eviction.** When chunk A leaves the streaming window, its
   rmb-pool slice frees without ref-counting against neighbours.
3. **Per-chunk LOD bake.** Permits a different LOD per chunk-copy of
   the same asset (we haven't observed this in practice, but the
   format permits it).

Pool entries fall into four functional classes by tag/centroid:

| class | tag pattern | centroid | role | what to do |
|---|---|---|---|---|
| **A — World-positioned named asset** | `PLA_`, `MOUN_`, `MAINTOWN_`, `MT_`, `OBJ_`, `REDROCKS_`, `BLDG_`, `FOOTHILLS_`, … | > 100 m from origin | actual render target | KEEP — primary placement |
| **B — Authoring source** | same prefixes as A | < 100 m from origin (asset-local) | artist's authored mesh in asset-local coords; library only | DROP unless you have a transform |
| **C — Per-chunk visibility wrapper** | `Object NNN_LOD\d+`, `BUILDINGS_NNN_NOLOD`, `GLOB_*`, `Hub NNN`, `Mesh NNN` | wrapping chunk centroid | aggregates foreign sections | KEEP only if in-zone for one of its baked sections |
| **D — Occluder / shadow proxy** | `Box NNN_LOD\d+` | a chunk centroid | shadow + z-buffer math, not visible | DROP universally |

Implementation: `_is_named_asset_tag`, `_is_phantom_wrapper`,
`_policy_drop` in `src/fh1_mapdecomp/rmb_world.py`.

### §3. The five-store placement architecture

Once you accept that the rmb pool is a library (with built-in chunk
duplication), the question is which other files tell you *where to draw*
a given pool entry. There are five known stores:

| # | Store | What it is | Authoritative for |
|---|-------|------------|-------------------|
| 0 | **rmb pool** | 102,012 mesh blobs in `bin.zip` | All geometry |
| 1 | **rmb_world** | Pool entries whose centroid is already at world position | Unique landmarks (smelter, dam, observatory, theatre, fountain, mainstage, named modular buildings) |
| 2 | **v42k7 chunks** | Per-region streaming bundles with transform tables | Repeated tiles / props (modular town walls, decals, cables, fences, race dressing) |
| 3 | **collobjs** (`Ribbon_00/CollObjs.xml`) | XML placement table for collidable props | Signs, benches, picnic tables, marker poles, fences-with-collision |
| 4 | **animatedobjects.zip** | ANIM_*.pgeo placements | Race events, autoshow lights, ski-lift cablecars, plane banners, festival rides |

A sixth candidate — small decoratives baked as "soup mesh" into the
trailing VBUF of each v42k7 chunk — was investigated and conclusively
ruled out (see §6).

#### 3.1 rmb_world (Store 1)

`rmb_world.py` walks the pool, picks entries whose centroid is more
than ~20 m horizontal from origin, and emits each at its embedded
position. The result, after the centroid filter, the
`Box NNN_LOD\d+` drop, the centroid-aware authoring-source filter, the
stacked-copy dedup (`stripped_tag, rounded_centroid` collapse), and
the 500 m wrapper-vs-named-asset cross-pool dedup, is around 7,500
named placements. Verified at every truth-coord landmark we have.

#### 3.2 v42k7 chunks (Store 2)

PGEO chunks of variant v42k7 are the primary prop placement source.
Each chunk's body holds:

- A **section list** (1–N sections, each pointing into a per-chunk
  array of pool handles).
- **Table A** (8 B per record: handle, pad).
- **Table B** (88 B per section, plus an `0xFFFFFFFF` separator), which
  carries cull bbox, runtime pointers, and the slot mask.
- A **position table** at 96 B per instance — world-space pos,
  rotation rows, plus a per-section uniform constant.
- A trailing VBUF (79 records × 40 B) — verified NOT a renderable mesh
  (see §6).

Two distinct decisions the engine makes per section:

**§3.2.1 Section-level: render or streaming-only? — solved.**
`Table B cull_box[0]` (f32 BE at +0x34):

- `0` → metadata only (instance_count = 0)
- `60` → streaming impostor / sentinel-bbox; do not render
- `>= 80` → real placement; render
- For u0=1 sections (direct render), the gate doesn't apply — cull_box
  is just a per-section LOD distance band.

Implementation: `_U0Z_CULL_THRESHOLD = 80.0` in
`src/fh1_mapdecomp/v42k7_inst.py`.

**§3.2.2 Within-section: which slot to render? — solved with caveat.**
The trailing u32 of each 88 B Table B record at +0x54 (BE) is a
**per-section slot RENDER mask**: bit N set ⇒ render slot N. After the
mask, an LOD-pair-leg-keep filter drops the LOD01 sibling when both
LOD00 and LOD01 would render. A high-bit-deactivation rule kicks in
when bits beyond the slot count are set ⇒ render nothing.

Caveat: the field is heterogeneous. On 1-slot sections it sometimes
holds a float (~1.0). The high-bit-deactivation rule degrades safely
in that case. The shipped behaviour is correct on the cases
checked visually (festival barriers, fence runs, road decals) but
isn't proven exhaustive.

**§3.2.3 v42k7 vs rmb_world cross-source dedup.** When an asset has a
named-asset rmb at world position, v42k7 references to that same
handle are streaming hints, not render targets. Without dedup we
emit "rings" of duplicates around chunk centroids. Fix: drop v42k7
blobs whose LOD-stripped tag matches an rmb_world emission. Removes
~21k Colorado instances (cables, decals, smelter components, etc).
`cmd_all` is reordered so rmb_world runs first.

#### 3.3 collobjs (Store 3)

`Ribbon_00/CollObjs.xml`: 21,877 placements with explicit `<Pos>` and
`<Orientation>`, resolving to 48 distinct rmb tags via
PhysicsType→handle mapping. Vocabulary: `CO_CLRD_FenceF` (3,829),
`CO_FEST_EquipSign_001` (2,743), `CO_MarkerPole_001` (1,162),
`CO_SignG_001` (988), `CO_Table_Picnic` (901), `CO_Bench_001` (585),
etc. **All collidable.** Decorative non-collidable props are not in
collobjs (verified for golf cart / haybale / wind turbine / autoshow
tent).

`GraphicsName="#N"` is a **sequential ordinal** into `FilenameMap_00.dat`,
not a hash. Don't try to use it as an asset pointer (verified
2026-04-29; see *§6 Closed channels*).

#### 3.4 animatedobjects (Store 4)

`animatedobjects.zip` ships 290 `ANIM_*.pgeo` files. Only 24 have
non-origin world bboxes; those are the actual world placements (race
events, autoshow lights, ski-lift cablecars including the gondola at
~(605, 477), plane banners, crop dusters).

`landmark_anim`-variant PGEOs (447 files) **embed their authored 3DSMax
filename** as `D:\p4\horizon\Forza2\Main\Media\Src\Tracks\<TRACK>\Scene\Animated\<NAME>.max`.
This is a labelling lever for festival rides — windmills, fireworks,
lasers, ITLY rides imported from the LaSpezia track. 29 distinct asset
names; full table in `probes/out/landmark_anim_names.tsv`.

### §4. Recovering the scene from the union

Pure positional dedup picks an arbitrary winner among same-position
entries. The right primitive is **primary owner chunk**:

> The smallest v42k7 chunk AABB that contains an rmb entry's world
> centroid is the chunk that "really" owns the asset. All other
> chunks containing the same point are visitors and their copies of
> the asset are streaming-prefetch refs.

Algorithm:

1. R-tree of all 14k v42k7 chunk AABBs.
2. For each rmb entry with world centroid, query the R-tree, pick the
   smallest containing AABB as primary owner.
3. Among entries sharing `(stripped_tag, primary_owner)`, keep one.
4. For Object/BUILDINGS/GLOB wrappers with foreign sections: keep a
   foreign section emission only when the wrapper's owner chunk is
   the same as (or contains) the section's primary owner.

This collapses N stacked copies → 1 per asset and resolves the
"wrong asset on top of right position" pattern when the wrong asset
is a visibility-buffer wrapper baking foreign geometry far from
where it belongs.

### §5. The runtime side: what we proved is missing on disc

The chunk activation map — *for a given chunk, which pool handles to
preload* — is what is missing. A series of investigations narrowed
where it lives.

**5.1 The xex is not the source.**
A full Ghidra-driven scan ran in 2026-04-28: truth-coord BE float
scan, LE float, BE half-float, BE i32 mm/cm, BE i16, BE Q16.16,
asset-name string presence, lis+addi immediate mining, RTTI vtable
walk, sub-blob marker walk. All negative for static placement
storage. The largest contiguous static-data cluster found via
lis+addi mining is 0xD10 bytes — far too small for a world-scale
placement table.

The retail xex was stripped of debug paths: every printf format
string and assert message is unreferenced. Static analysis through
string anchors is therefore impossible.

**5.2 No "master chunk pool" sits in memory.**
Step A of the FIND_CHUNK_DATA_SOURCE plan ran on 15 fresh
`min_mb=0` Xenia memdumps (~35 GB) covering festival, dam, smeltery,
red rocks, observatory, bunker, finley + funley outposts, NE/NW
city, just-loaded, and main-menu states. The record-shape scan
returned chunk_id=220 only, exactly 2 records every time. That is
the **festival fast-travel preset**, not a runtime per-chunk
activation table. The earlier "activation table FOUND" interpretation
was wrong.

**5.3 But chunk data does come from disc.**
Switching from ISO mode to xex-install mode (so Xenia uses
`open` / `pread64` instead of mmap fault paths), strace during a
festival → smeltery drive showed:

| file | bytes | pread64s | span |
|------|------:|---------:|-----:|
| **`media/tracks/colorado/bin.zip`** | 71,340,032 | 2,048 | 74.6 s |
| `audio/tracks/colorado/AMB_Quads_Stream.fsb` | 3,801,088 | 29 | 81.3 s |
| traffic AI `.zip` files (16) | ~2 MB each | 60-100 | <0.2 s |

`bin.zip` dominates by 19× over the next file. The engine reads its
central directory once, then `pread64`'s individual entries by name
on demand as chunks activate. **There is no separate master-
directory file.** `bin.zip`'s own central directory IS the master.

That means the missing geometry — assuming it isn't computed
procedurally — is somewhere in the bin.zip entries we already parse.
The "missing 80%" is then a **decoding** problem, not a discovery
problem.

**5.4 Recovering the activation map from runtime.**
The strategy in `fh1-memdump/docs/CHUNK_MAPPING_STRATEGY.md` is to
run a watcher (`scripts/watch_chunk_table.py`) at 200 ms polling
against the 39.9 MB anon mapping, drive every named area in
Colorado, and capture the per-chunk active rmb-handle list. The
union over a long drive is the whitelist of every pool entry the
engine ever renders. Cross-referencing that against `rmb_world.py`'s
output identifies what static logic dropped.

This is the open-frontier work and it lives in the sibling repo, not
here.

### §6. Closed channels (don't re-investigate)

Static placement sources for missing decorative props that have been
exhaustively ruled out:

- rmb pool centroid (library-only for these tags)
- rmb pool sub-blob scan (24,650 sub-blobs, 425 keyword matches all near origin)
- v42k7 slot list (zero references to these handles)
- v42k7 slot list with alternative interpretations (slot 0, highest-bit, mask-bit; all wrong)
- v42k7 chunk VBUF as mesh (7 topologies tested, all visual noise; byte budget audit shows no hidden region)
- collobjs (collision-only types)
- animatedobjects (race events / lights / cablecars only)
- HEXY (`Colorado_00.hex` — far-LOD impostor index, sparse, doesn't span the relevant zones)
- xex code via `lis`+`addi` immediate scan
- xex code via BE u32 pointer table scan
- xex code via anchor coord float scan
- xex code via asset tag string presence (zero hits for `PLA_`, `MOUN_`, `STFT_`, `GLOB_`, etc.)
- pgeo `.fxobj` / `.bundle` / `.sh` / `.pvsz` / `.fiz`
- `db/gamedb.slt` SQLite (221 tables, all geo track-relative)
- `aiopenworld.zip` OWTM `.owt` (AI traffic waypoints only)
- `TrackRoute*.xml` (race-event NamedTransforms)
- `GameObjs.xml` (barn finds, gas stations, race spawns)
- `ParticleEmitters.xml`, `PostProcessingZones_Safe.xml`
- `FilenameMap_00.dat` (filename strings, no hashes; `GraphicsName="#N"` is an ordinal, not a hash)
- `co_cone.bin`, `PhysicsDefinitions.bin` (physics types, no placements)
- `.fiz` files (foliage cells; Block A decoded as X/Y/Z/packed; Block B is a 24-bit render-flag bitmask, not triangles — visualised OBJ confirms it is not a mesh)
- `.bix` files (BIX1 + `_B.bix` sampled across 16 files; zero embedded paths or names)
- `_0xHHHH.bin` files (all 13,187 are CAFF v21.11 textures with `compiled\textures\` source paths)
- `Colorado_00.hex` (HEXY) for placements
- `Colorado_track_00.col` (index only; pool built at runtime)
- `colorado.owr` / `.oww` / `.crowd` (sentinels)
- `*.pvsz`, `Colorado_00.pvs`, `PVSZLookup_00.dat` (visibility tree, not placements)
- bundle entries in bin.zip
- sub-blob marker walks in xex

For each of these the negative is documented in
`docs/where-we-stopped.md` and the per-investigation memory entries.

### §7. File / format reference

The detailed format docs live alongside this README:

- `docs/format.md` — bin.zip envelope, LZX framing
- `docs/bin-zip-layout.md` — every extension's pattern + count
- `docs/rmb-bin.md` / `docs/rmb-subblobs.md` — pool entry layout
- `docs/pgeo-body.md` — PGEO header + body classes (terrain, v42k7,
  v42k7 proc subvariant, grass, crowd, vegetation, v44k5, landmark_anim)
- `docs/v42k7-section-mesh-selection.md` — section + slot selectors
- `docs/v42k7-per-instance-transforms.md` — 96 B position table
- `docs/landmark-placement-investigation.md` — landmark placement rules
- `docs/world-architecture.md` — long-form chunk-streaming write-up
- `docs/architecture-2026-04-28.md` — engine-level reconstruction
- `docs/state-of-extraction.md` — pipeline status table
- `docs/where-we-stopped.md` — full closed-channel inventory
- `docs/world_data_tree.txt`, `docs/file_tree.txt` — exhaustive tree dumps
- `docs/xex-walk/` — Ghidra investigation chain
- `docs/fh1_landmarks.txt`, `docs/fh1_pois.txt`, `docs/placements.txt` —
  truth-coord references for visual scoring

### §8. Repo layout

- `src/fh1_mapdecomp/` — the package
  - `cli.py` — entrypoint (`list`, `extract`, `world`, `blender`, `render-topdown`, `all`)
  - `binzip.py`, `lzx.py` — container reader
  - `pgeo.py`, `pgeo_body/` — PGEO header + body class decoders
  - `rmb.py`, `rmb_world.py` — pool reader + landmark emitter
  - `v42k7_inst.py` — per-chunk transform extractor with cull_box and slot mask
  - `terrain_hi.py` — terrain mesh builder
  - `collobjs.py` — CollObjs.xml emitter
  - `world.py` — world / ribbon JSON aggregator
  - `index.py` — bin.zip indexer
  - `blender_run.py`, `blender_scripts/` — Blender shellout + import script
- `scripts/` — analysis utilities (truth checks, asset tracing, slot
  probes, landmark builder, xex scans, etc.)
- `probes/` — one-off investigation scripts kept for evidence
- `vendor/` — third-party deps pulled in by `download-deps.sh`
- `out/` — extractor output (gitignored)

### §9. Final verdict

The investigation produced a functional extractor for the parts of the
world that are statically encoded: terrain, named landmarks,
collidable props, animated content. That is enough to render a
recognisable Colorado in Blender — landmarks at correct positions,
roads, terrain, festival.

The bulk of the populated world (city density, fence runs, scattered
decoratives) is not statically encoded. It is built at runtime from
streaming-engine logic the disc data does not enumerate. Recovering it
requires the runtime memory-capture path in the sibling
`fh1-memdump` repo, not further static analysis here.
