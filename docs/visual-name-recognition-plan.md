# Visual model-recognition pipeline — fresh-context plan

Status: **planned 2026-04-29**, not yet implemented.

## What this solves

The "Break 1" gap in our placement chain (per `freeroam-placement.md` +
auto-memory `project_filename_map_decoded_2026-04-29.md`):

- `CollObjs.xml` has 21,877 placements. Each has `(Pos, Orientation, PhysicsType)`.
- The 4-rule textual resolver (exact / `_LOD00` / `CO_→OBJ_` / leading-zero
  collapse) maps **51** distinct `PhysicsType` base names to rmb-pool tags
  → recovers 9,119 of 14,092 freeroam placements (65%).
- **4,865 placements (35%) have a position but no recovered mesh.** Their
  PhysicsType base names match nothing in the rmb pool by any text rule.
- We've ruled out: hash functions (CRC32 / FNV-1a / djb2 / Murmur2 /
  Jenkins-OAAT all fail against PhysicsType strings); GraphicsName fallback
  (it's a sequential build-time ordinal, not a per-instance asset ref);
  cross-zip filename grep (the names just aren't there).
- **The meshes likely DO exist** in the rmb pool under different names
  (e.g. `Object0123_LOD00`, `Wood_Fence_03`, etc.). We need a non-text
  bridge — visual recognition.

The pipeline below pairs unresolved PhysicsType names to rmb pool tags by
**rendering each mesh and asking a vision LLM "what is this?"**, then
matching the answer against a curated description of each unresolved
PhysicsType. Confidence-thresholded so we only commit certain matches.

## Pipeline overview

```
   rmb pool        →   Stage 1     →   PNG renders        ─┐
   (~3,677 tags)       (Blender)       (3 views each)      │
                                                            ▼
   unresolved      →   Stage 2     →   names + visual    ─→  Stage 3   →   matches
   CollObjs            (curate)        descriptions          (Haiku        (json:
   PhysicsType                         file                  vision         tag → name)
   names                                                     subagent)        │
                                                                              ▼
                                                                          Stage 4
                                                                          (apply
                                                                          to resolver)
```

### Stage 1 — Render thumbnails

**Script**: `fh1-memdump/scripts/render_thumbnails.py` (to be written; can
extend `make_gallery_blend.py` which already loads .npz meshes via
`stage2()`).

Inputs:
- `/tmp/fh1_export/rmb-world/rmb_world/index.json` (from existing
  `fh1-mapdecomp rmb-world --policy all` run; if absent, regenerate)

For each rmb tag (deduped via `normalize_tag` from existing gallery
script):
- Apply Y-up→Z-up rotation `(x,y,z) → (x,-z,y)` (already in
  `make_gallery_blend.py`)
- Centre on centroid
- Render **3 views** at 256×256:
  - `_front.png` — camera at `(0, -dist, 0)` looking +Y
  - `_persp.png` — camera at `(dist*0.7, -dist*0.7, dist*0.5)` looking origin
  - `_side.png`  — camera at `(dist, 0, 0)` looking -X
  - where `dist = max(bbox extent) * 1.6`
- Neutral lighting (one sun + one ambient), white background, plain
  matte grey material — the LLM only needs shape, not texture.

Output:
- `fh1-mapdecomp-release/out/model_gallery/renders/{tag}_{view}.png`
- Per-tag manifest entry: `{tag, vcount, tri_count, bbox_extent, render_paths}`

Scope decision (start small):
- **First pass**: 873 distinct tags from the existing `--max-extent 50`
  filter (small props, where the unresolved CollObjs live). 873 × 3 =
  ~2,600 PNGs, ~10 MB, ~30 min render time.
- If validation succeeds, expand to the full ~3,677 rmb-world tags
  (~11k images, ~1-2 hr).

Render command (Blender headless):
```
"/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender" \
  --background --python scripts/render_thumbnails.py -- \
  --rmb-world /tmp/fh1_export/rmb-world/rmb_world \
  --out fh1-mapdecomp-release/out/model_gallery/renders \
  --max-extent 50
```

### Stage 2 — Build "names to find" file

**File**: `fh1-mapdecomp/data/unresolved_collobjs_descriptions.json`
(to be written by hand or with help from gameplay knowledge)

Source: the 180 (or 758, see freeroam-placement.md) distinct PhysicsType
base names from CollObjs.xml. Filter to the unresolved set (those that
the existing 4-rule resolver fails to match in `bin.zip`).

Format:
```json
[
  {
    "physicstype": "CO_HouseMailBox_001",
    "placement_count": 93,
    "description": "residential mailbox on a vertical wooden post, US-style flag-on-side, ~1.5m tall"
  },
  {
    "physicstype": "CO_MarkerPole_001",
    "placement_count": 1162,
    "description": "thin reflective road delineator pole, white or yellow, ~1m tall, often paired"
  },
  {
    "physicstype": "CO_Bench_001",
    "placement_count": 585,
    "description": "park bench, wood-and-metal, two legs, with backrest, ~1.8m wide"
  },
  ...
]
```

Top targets to seed (highest placement counts, most-bang-for-buck):
- `CO_CLRD_FenceF` (3829) — country wooden post-and-rail fence
- `CO_FEST_EquipSign_001` (2743) — festival event sign banner
- `CO_MarkerPole_001` (1162) — road delineator
- `CO_SignG_001` (988) — generic signpost
- `CO_Table_Picnic` (901) — picnic table
- `CO_CLRD_BinB` (711) — trash bin
- `CO_CLRD_FlowerholderA` (710) — flower planter
- `CO_Bench_001` (585) — park bench
- `CO_WoodenBarrier_001` (574) — wooden roadside barrier
- `CO_ArmcoArrow_001` (417) — metal direction-arrow guard

Even a rough description ("trash bin, cylindrical, dark") is enough for
shape-recognition.

### Stage 3 — Vision subagent recognition

**Script**: `fh1-memdump/scripts/recognize_models.py` (to be written)

Strategy: spawn `Agent` tool calls with `model="haiku"` and pass:
- 3 PNGs per mesh
- The full unresolved-names list with descriptions
- A strict prompt requiring `"unsure"` for confidence below threshold

Per batch of ~10 meshes (30 images):

```
PROMPT (concise):
You are matching 3D model renders to known object names.

For each MESH below, choose the ONE name from the LIST that best matches
the rendered shape, or output "unsure" if you can't confidently identify it.

Confidence threshold: only commit a match if you are at least 9/10 sure.
"unsure" is always preferred over a wrong guess.

OUTPUT FORMAT (one line per mesh):
  <tag>: <chosen_name_or_unsure>: <confidence_1to10>: <one-sentence-why>

LIST OF NAMES:
  CO_Bench_001 — park bench, wood-and-metal, with backrest
  CO_HouseMailBox_001 — residential mailbox on post
  ... (full unresolved list)

MESH 1: <tag1>
  [3 images attached]
MESH 2: <tag2>
  [3 images attached]
...
```

Subagent invocation (Agent tool):
```
Agent(
  description="Vision-recognize 10 rmb meshes",
  subagent_type="general-purpose",
  model="haiku",
  prompt="<above prompt>"
)
```

Collect responses; parse into:
```json
{
  "Object0123_LOD00": {"match": "CO_CLRD_FenceF", "confidence": 9, "why": "wood post and rail"},
  "Object0124_LOD00": {"match": "unsure", "confidence": 4, "why": "abstract metal shape"},
  ...
}
```

Confidence handling:
- Threshold defaults to **9/10** for first pass (very strict)
- After running once, lower threshold to 7 and re-run *only the "unsure"
  meshes* — get a second opinion on borderline cases
- Anything that survives at threshold ≥7 with a consistent name across
  re-runs becomes a high-confidence match

### Stage 4 — Apply matches to resolver

**File**: `fh1-mapdecomp/data/visual_name_overrides.json`

Format:
```json
{
  "physicstype_to_rmb_tag": {
    "CO_HouseMailBox_001": "Object0456_LOD00",
    "CO_Bench_001": "Wood_Bench_03_LOD00",
    "CO_CLRD_FenceF": "PostRail_Fence_001_LOD00",
    ...
  }
}
```

Patch `src/fh1_mapdecomp/collobjs.py` to consult this override map
**before** the 4-rule normaliser. Each successful override unlocks all
placements for that PhysicsType (often hundreds).

Re-run:
```
fh1-mapdecomp collobjs-inst \
  --source /run/media/paths/SSS-Games/fh1-xex/.../colorado/bin.zip \
  --output fh1-mapdecomp-release/out/collobjs_inst \
  --policy freeroam
```

Diff the new `kept` count vs prior 9,119. Each percentage point recovered
is ~140 placements.

## File paths & dependencies

Inputs (must exist):
- `/run/media/paths/SSS-Games/fh1-xex/4D5309C9/00007000/2DC7007B/media/tracks/colorado/bin.zip` — FH1 archive
- `/run/media/paths/SSS-Games/fh1-xex/.../Ribbon_00/CollObjs.xml` — placement source
- `/tmp/fh1_export/rmb-world/rmb_world/index.json` — pre-extracted meshes (else regenerate via `fh1-mapdecomp rmb-world --policy all`)
- `/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender` — Blender 5.1 binary

Outputs (will be created):
- `fh1-mapdecomp-release/out/model_gallery/renders/{tag}_{view}.png`
- `fh1-mapdecomp/data/unresolved_collobjs_descriptions.json`
- `fh1-mapdecomp/data/visual_name_overrides.json`
- `fh1-memdump/reports/recognize_models-<ts>.json`

Existing tooling to leverage:
- `fh1-memdump/scripts/make_gallery_blend.py` — has `.npz` mesh loader,
  Y-up→Z-up rotation, centroid-centring; reuse `stage2()` body for the
  renderer.
- `fh1-mapdecomp` `rmb-world` CLI — already produces the per-blob .npz
  files we feed to the renderer.
- `fh1-mapdecomp/src/fh1_mapdecomp/collobjs.py` — has the 4-rule resolver;
  patch it to read `visual_name_overrides.json` first.

## Validation strategy

Before scaling up, validate on a tiny known-good set:

1. **Sanity check** (10 meshes, ~5 min):
   - Manually pick 10 rmb tags from the gallery whose mesh IS already
     resolved (e.g. `BLDG_Bridge_RedstoneA3_01` is clearly a bridge).
   - Run the recognition pipeline against a names list including
     "bridge", "tunnel", "building", "tower" descriptions.
   - Verify the subagent picks the right category for each. If it can't
     even identify a bridge as a bridge, the pipeline needs work
     (better renders, better prompt, stronger model).

2. **Differential test** (50 meshes, ~30 min):
   - Pick 50 random rmb tags whose meshes are placed by `rmb-world`.
   - Render + recognise.
   - For high-confidence matches, check whether the chosen description
     plausibly fits the tag's name (e.g. tag `MOUN_DAM_BLDG_InletTower`
     should match a "tower" or "industrial structure" description, not
     "park bench"). False-positives at threshold 9 should be near-zero.

3. **Full run** (873 meshes, ~3 hr):
   - Render everything.
   - Recognise in batches of 10 with threshold 9.
   - Apply overrides; measure placement-count delta.

## Cost / time estimates

| Stage | Time | Cost |
|---|---|---|
| Render 873 meshes × 3 views | ~30 min Blender headless | local CPU |
| Render full 3,677 × 3 | ~2 hr | local CPU |
| Recognition (Haiku, 87 batches × 10 meshes) | ~30 min API time | ~$1-3 |
| Re-recognition pass at lower threshold | ~15 min | ~$1-2 |

If Haiku underperforms on first 10-mesh sanity test, escalate to Sonnet
(~3-5× cost, much higher reliability for shape recognition).

## Why this approach

1. **Bypasses the textual mismatch entirely** — vision recognition
   doesn't care whether a fence is named `CO_CLRD_FenceF` or
   `Object0123_LOD00`; it sees a fence.
2. **Confidence-thresholded** — we only commit certain matches; false
   positives are rejected via "unsure" answer.
3. **Scales cheaply** — Haiku is cheap; we can re-run with different
   thresholds without burning budget.
4. **Compounds** — every successful pairing unlocks all placements for
   that PhysicsType. 10 good pairings probably recover 80% of the 4,865
   missing placements (since the unresolved set is dominated by ~10
   high-count names).

## Background memory entries (load these first)

A fresh conversation will auto-load via `MEMORY.md`:

- `project_filename_map_decoded_2026-04-29.md` — why hash/ordinal
  paths were rejected
- `project_disk_source_FOUND_2026-04-29.md` — bin.zip is the chunk
  source; tool already parses it
- `project_named_section_refs_breakthrough.md` — labelling-map rules
  for opaque wrappers
- `project_landmark_placement_rule.md` — what counts as a real
  placement vs phantom
- `project_v42k7_within_section_selector_solved.md` — within-section
  RENDER mask logic (already-decoded grouping format)

The freeroam-placement.md doc itself enumerates the still-open threads
this plan tackles.

## TL;DR for fresh conversation

```
"Resume from fh1-mapdecomp/docs/visual-name-recognition-plan.md.
 Build the visual model-recognition pipeline:
 1. Render 873 small-prop rmb meshes to PNG (3 views each) using a new
    scripts/render_thumbnails.py (extend make_gallery_blend.py).
 2. Curate unresolved_collobjs_descriptions.json for the top-10 unresolved
    PhysicsType base names with brief shape descriptions.
 3. Spawn Haiku Agent batches of 10 meshes; collect tag→name matches at
    confidence ≥9.
 4. Validate on 10 known-good meshes first; if pass, run full set.
 5. Write visual_name_overrides.json and patch collobjs.py to consult it
    before the textual resolver. Measure placement-count delta."
```
