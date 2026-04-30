# v42k7 per-instance placement — bug investigation

> **Read `world-architecture.md` first.** As of 2026-04-28 we have a
> coherent model: the symptoms below are caused by chunk-streaming
> flat-union duplication (the rmb pool is the union of every chunk's
> local rmb table) plus an unsolved within-section selector. The
> "Hypotheses to test" sections below were the working theories; most
> are now superseded but kept for history. Jump to:
>
> - `world-architecture.md` §3 (chunkified-from-global export) for why
>   the same handle has multiple copies at the same world coords
> - `world-architecture.md` §5 for the two v42k7 selectors and their
>   solved/open status (cull_box[0] solves §5.1, slot picker §5.2 open)
> - `v42k7-section-mesh-selection.md` for the cull_box[0] details and
>   ruled-out within-section-selector candidates

---

## Original investigation (2026-04-17 visual)

Visual inspection of the first .blend produced after wiring
`parse_position_table` → `v42k7_inst` → Geometry Nodes importer
(2026-04-17). The core placement is *largely* right — city outline is
recognisable — but several handles render in obviously-wrong positions.

## Observed artefacts

| Handle                                            | Symptom                                                             |
| ------------------------------------------------- | ------------------------------------------------------------------- |
| `061985_PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00`   | Dense crowds in the sky, forming a circular ring around the NW city |
| `061945_cables_512`                               | In the sky, same circular pattern                                   |
| `061916_cables_570`                               | Low on the map, scattered everywhere                                |
| `061910_Shadow_Caster_005_LOD00`                  | Scattered everywhere, slight concentration east                     |

## Hypotheses to test

1. **Multi-handle sections.** Each 64B section record can hold up to
   3 `(count=1, ptr)` pairs. Current extractor treats all handles in
   one section as sharing the *same* instance list. If the 3 handles
   are actually meant for disjoint instance sub-ranges (not LOD
   variants of the same mesh), every handle is placed N times when it
   should only be placed N/3 times. That would match the "crowds" feel
   of the blast furnace.

2. **Handle-to-LOD filtering kept the wrong one.** The extractor drops
   blobs whose tag contains `LOD01/02` or `MIDDIST`, keeping
   `lod == "00"`. If the "right" hero mesh for a section was LOD01
   (rare but possible) we'd lose it and retain a cousin with the same
   world slot, producing a model-swap rather than a placement bug —
   worth ruling out.

3. **Fallback on multi-instance sections.** For sections where the
   position table slice was empty or `parse_position_table` returned
   `None`, we fall back to chunk-anchor + local-bbox centre. That
   placement reads like "one copy per chunk that references the
   handle" — which matches the scattered `Shadow_Caster_005` and
   `cables_570`. If many chunks reference these handles, we get a
   uniform sprinkle across the map.

4. **Shared sentinel handles.** Some handles might be intentionally
   placed *nowhere* by the engine (pre-created but conditionally
   skipped at runtime). We render them anyway because our extractor
   emits them at every referencing chunk's anchor.

5. **Circular ring in the sky.** NW = Colorado city. The ring
   diameter matches the chunk-grid spacing around the city centre.
   Combined with elevated Y, suggests positions from chunks whose
   bbox centre is high off the ground (e.g. mountain chunks that
   nonetheless reference "cables_512") are contributing the sky
   points via the fallback path — OR the position table for those
   chunks is being misread.

## First probes

1. Print `(handles, instance_count, sep_mult)` distribution across all
   sections. What fraction have >1 handle? What's the mean
   instance_count per (handle, chunk) pair for the 4 offenders above?
2. For each of the 4 offending handles, dump the chunks that reference
   them, split by "used multi-instance placement" vs "used fallback".
   Expectation: `cables_570` is almost all fallback.
3. Check whether the 3 per-section handles are LOD siblings (share a
   base name, differ in LOD digits) or genuinely different meshes.
   Grep the rmb tag strings.

## Next steps

Run the three probes, update this doc with actual numbers, decide
which hypothesis to attack first.

---

## 2026-04-17 — probe results

### Handles-per-section distribution (all 11,274 chunks, 72,995 sections)

| handles | sections |
| ------: | -------: |
|       1 | 42,217   |
|       2 | 29,143   |
|       3 |  1,635   |

~42% of sections carry more than one handle. Current extractor places
every handle at every instance in the section's slice, so these become
2–3× over-rendered.

### Slot histograms for offenders

| handle | tag                              | slot 0 | slot 1 | slot 2 |
| -----: | -------------------------------- | -----: | -----: | -----: |
| 61910  | Shadow_Caster_005_LOD00          | 1,224  |   21   |    0   |
| 61915  | cables_512-related               | 2,746  |  601   |    0   |
| 61916  | cables_570                       |   76   | 2,714  |  557   |
| 61945  | cables_512                       | 1,282  |  282   |   46   |
| 61985  | PLA_SMELTER_BLDG_BlastFurnaceLow |   105  |   30   |    0   |

**`cables_570` sits in slot 1/2 2,714+557 times and in slot 0 only 76
times** — it is almost exclusively an auxiliary mesh. The other
offenders appear in slot 0 too (the blast furnace is slot 0 105×), so
the bug is compound, not single-cause.

### Top (slot0, slot1) pairs

```
(61908, 61909): 4642
(61900, 61901): 4318
(61925, 61926): 3263
(61933, 61934): 3086
(61898, 61899): 2943
(19591, 0):     2884    ← slot 1 literal 0 = no-second-handle sentinel
(61923, 61924): 2825
(61915, 61916): 2714
```

Pairs are **consecutive handle IDs** — canonical main/aux siblings in
the rmb pool. The `(X, 0)` pattern is a single-handle section.

### Canonical 07014 section layout

```
section 0: [61958, 61959]
section 1: [61931, 61932]
section 2: [61921, 61922]
section 3: [61908, 61909]
section 4: [61925, 61926]
section 5: [61955, 61956]
section 6: [19589, 0]
```

All 7 sections match the main/aux sibling pattern. 173 instances are
meant to be placed **173 times**, not 173 × 2.

### Conclusion

**Fix 1 (slot-0-only).** Keep only the first non-zero handle per
section and drop the rest. That immediately halves or thirds the
render count for sibling-pair sections and pushes `cables_570` out of
most placements. Not perfect — sections where the real mesh is in
slot 1 (e.g. some `Shadow_Caster` instances) will be wrongly dropped —
but it is a large correctness win with a small code change.

**Still open.** Even after the slot fix, the blast furnace is in
slot 0 of 105 sections with ~4k instances across them. The "ring
around the city" pattern suggests those instances are being placed at
chunk-anchor positions rather than the furnace's real world location.
Likely causes to investigate:
  - Some of those 105 sections use the fallback (chunk anchor + local
    bbox centre) because their position-table slice returned empty.
  - Or the 96B positions for those chunks are being misread (mis-aligned
    pos_table_start despite the exact-match fix).

---

## 2026-04-17 (later) — off-by-one in instance_count

### The smoking gun

Dumping rotation row magnitudes for every section's first + last
instance in canonical `__R00G07014.pgeo`:

```
sec 0 first: row_mags=(0.378, 0.378, 0.378)   ← clean
sec 0 last:  row_mags=(74.8, 385.5, 0.0)       ← GARBAGE
sec 1 first: row_mags=(1.257, 1.257, 1.257)
sec 1 last:  row_mags=(203.5, 2523.7, 0.0)    ← GARBAGE
sec 2 first: row_mags=(0.869, 0.869, 0.869)
sec 2 last:  row_mags=(0.0, 2517.9, 246.8)     ← GARBAGE
...
sec 6 last:  row_mags=(123.7, 519.6, 1.3e37)   ← GARBAGE
```

Every section's last "instance" has rotation rows filled with values
like `(300, 2500, 0)` and `(50, 100, 220)`. These look like **cull /
fade / LOD distance constants**, not rotation data. Sampling 232
chunks / 1,975 sections / 39,867 decoded instances:

- 2,025 (5.08%) instances are garbage.
- **100% of sections** contain at least one garbage instance.
- 1,778 garbage entries are at the section's last position (88%).
- 206 at the first position (10%).
- The rest are a single 19-instance chunk where every middle entry is
  garbage — unrelated outlier.

The "173/173 in-bbox" I celebrated earlier was misleading — the
garbage entries still have plausible XYZ positions at offset 0x00,
and only the rotation rows at 0x30..0x60 are metadata. Position checks
alone pass.

### Interpretation

Each section's 96B slice is `(count - 1)` real per-instance records
plus one trailing section-metadata record. The metadata record reuses
the 96B layout so sections pack cleanly, and the engine keys off the
high-magnitude cull values to treat it as "end of section".

### Fix

Treat the final 96B of each section's slice as metadata, not a
placement. For the 10% "first garbage" cases, fall back to a magnitude
filter (rot row mag > 10 ⇒ discard). Re-probe after applying.

---

## 2026-04-17 (probe 2) — section ordering eliminated

Scan of every v42k7 chunk in colorado/bin.zip via
`src/probe_section_order.py`:

```
v42k7 chunks:               14,122
  with Table B parsed:       11,861
  section_index == i:        11,861
  mismatch:                        0
```

Table B records are stored in file order, and each record's embedded
`section_index` field equals its positional index in every single
chunk. The `records[i] ↔ layout.sections[i]` assumption in
`v42k7_inst.extract_v42k7_instances` is safe — this is not the bug.

Also note: 14,122 v42k7 chunks total, 11,861 with a parseable Table B
(~84%). The 2,261 that return None from `parse_table_b` are the
single-instance flavour and fall back to chunk-anchor + local-bbox
centre in the extractor.

Remaining candidates from the original probe list:

- **Separator_mult interpretation.** Still unverified across varied
  `sep_mult` values. If the between-section skip is wrong by any
  constant factor, later sections read misaligned 96B entries.
- **Handle slot semantics.** `cables_570` (61916) sits in slot 1/2
  3,271 times vs slot 0 76 times; slot-0-only keeps it out but also
  drops the (rare) sections where slot 1 is the real hero mesh.
- **Last 96B of each section being metadata** is already dropped by
  the rotation-magnitude filter in `parse_position_table`, so the
  "trailer fix" that just landed is functionally redundant — that
  explains why the .blend didn't visibly change.

Next probe: walk a few chunks with varied `separator_mult` values,
compare cursor arithmetic against the first-w-marker scan, and check
whether any chunk's position table is being read from a shifted
offset.

---

## 2026-04-18 — probe 3: separator_mult alignment

Walked the cursor arithmetic for every Table-B-parseable chunk and
checked the first-entry w-marker (0x3f800000 at +0x0C) at each section
boundary. Breakdown of the 11,861 chunks:

| state                                       | count |
| ------------------------------------------- | ----: |
| clean (every section's first entry is w=1)  | 5,667 |
| cursor drifts mid-table                     |    37 |
| no first-w, `sum(instance_count) == 0`      | 5,983 |
| **no first-w, `sum(instance_count) > 0`**   | **174** |

### 174-chunk bug (nonempty but no findable pos table)

These chunks declare real per-instance data in Table B (up to 292
instances in `__R00G07048.pgeo`) but `_find_first_w` returns -1, so
`parse_position_table` returns None and the extractor falls back to
chunk-anchor + local-bbox centre for **every** section. This is the
quiet source of the "scattered everywhere" artefact for any handle
referenced by these chunks. Spot-checked examples:

```
__R00G06534.pgeo  sc=7   total=33
__R00G06475.pgeo  sc=12  total=2
__R00G07185.pgeo  sc=11  total=6
__R00G07048.pgeo  sc=22  total=292       ← biggest offender
__R00G07226.pgeo  sc=11  total=152
__R00G06733.pgeo  sc=17  total=55
```

Likely cause: the current anchor scan looks in a 256-byte window after
the computed `table_b_end` and requires two consecutive w=1 markers.
If the pre-table footer size varies past 256B, or if the first real
entry's w is stored as something other than exactly 1.0 (unusual but
possible when w encodes per-instance scale), the scan gives up.

### 37-chunk drift (cursor off by 20·sep_mult mid-table)

For the drifted chunks, the delta between expected and actual w-marker
position is exactly `20 * prev_section_sep_mult`:

| prev sep_mult | delta | count |
| ------------: | ----: | ----: |
|             1 |    20 |    15 |
|             2 |    40 |    22 |

Equivalent to saying the true between-section skip is `52 * sep_mult`
rather than `32 * sep_mult` **for these chunks only**. First drift
always at section 1 (n=22) or section 6 (n=15); all 37 chunks are
small (low section counts with many empties). Possibilities:

- A different separator formula (52-byte base?) applies when the
  previous section has non-trivial u3 or type-flag — not yet checked.
- The pos_table_start anchor is landing on a spurious w-marker in
  the pre-table footer for these chunks specifically, so everything
  past section 0 is off by a constant.

### Priority

174 silently-fallback chunks >> 37 drifted chunks for visible
artefacts. Next step: read one of the 174 by hand (e.g.
`__R00G07048.pgeo`, 292 instances declared) and find where the
position table actually starts. If it's beyond the 256-byte scan
window, widen the window. If the w field isn't exactly 1.0 in that
chunk, relax the anchor condition.

---

## 2026-04-18 — probe 4: where does the pos table actually start?

Brute-force in-bbox scan across every chunk where `parse_position_table`
currently returns `None` AND Table B reports non-zero instances
(bin.zip contains both upper- and lower-case duplicates of each pgeo,
so counts below are roughly 2× the unique-chunk totals):

| outcome                                       | count |
| --------------------------------------------- | ----: |
| found, distance from table_b_end > 256        |    74 |
| found, distance <= 256 (w != 1.0)             |     0 |
| unfound (in-bbox scan can't match `total`)    |   216 |

### Finding 1: the w-anchor is correct, the window is too small

Every single found offset has the first entry's w stored as exactly
`0x3f800000` (= 1.0). The w-marker anchor is right; the 256-byte scan
window past `table_b_end` is what's wrong. Distance distribution of
the 74 recoverable chunks:

| distance (bucketed to 64B) | chunks |
| -------------------------: | -----: |
|                        256 |     25 |
|                        320 |     12 |
|                        704 |     20 |
|                        768 |     17 |

Two clusters: ~256-320 and ~704-768. The first is a mild widening; the
second suggests there's a larger variable-size block (possibly a
second Table-B-like region or a per-chunk footer) we haven't accounted
for. Widening the scan window to at least **1024 bytes** recovers all
74 cleanly.

### Finding 2: 216 chunks have no full in-bbox 96B run

For these, even an unbounded brute-force scan couldn't find a
contiguous 96B-stride run of length equal to `sum(instance_count)`.
But most DO have a partial run starting at a w=1 offset close to
table_b_end. Spot samples:

| chunk                 | sc  | total | partial offset (from tbl_end) | partial run |
| --------------------- | --: | ----: | ----------------------------: | ----------: |
| `__R00G06390.pgeo`    |   2 |     2 |                            48 |           1 |
| `__R00G06349.pgeo`    |   2 |     5 |                            48 |           3 |
| `__R00G06839.pgeo`    |   3 |    55 |                            44 |          27 |
| `__R00G07050.pgeo`    |  11 |   210 |                          5360 |         124 |
| `__R00G06531.pgeo`    |   6 |    80 |                          5748 |          23 |
| `__R00G06581.pgeo`    |   4 |    48 |                          3116 |          17 |

Two possibilities:

- The chunk bbox+20m margin is too tight — some instances legitimately
  extend past chunk bounds (bridges, power lines spanning chunks).
  Loosening or disabling the in-bbox filter in the probe would let us
  measure the real run length. High-confidence fix if the run length
  jumps to `total`.
- Table B's `instance_count` for some sections includes trailing
  sentinel/metadata entries that were counted but don't match the 96B
  record layout — the rotation-magnitude filter in
  `parse_position_table` would have dropped them later anyway.

### Proposed fix (first pass)

1. Widen `scan_end` in `_find_first_w` from `table_b_end + 256` to at
   least `table_b_end + 1024`. Expected gain: 74 chunks cleanly
   relocated to per-instance placement.
2. Separately, relax the `pos_table_start >= table_b_end` check —
   when `leading_sep` exceeds the pre-table gap, the current function
   returns None even though a valid table exists at `first_real`.
   Fallback: treat `leading_sep` as an upper bound and clamp
   `pos_table_start` to `table_b_end` when the back-up would underflow.
3. Re-run probes 2/3/4 after fix, confirm the 74 drop out of the
   failure buckets.

### Next probe

Re-run the brute-force pos-table locator on the 216 "unfound" chunks
with the in-bbox constraint replaced by a NaN-only filter, to see
which of the two hypotheses explains them.

---

## 2026-04-18 — probe 5: relaxed (finite/w=1-only) walker

Walker variant: finite-triple at +0x00 + w=0x3f800000 at +0x0C; no
bbox, no rotation check. Walks 96B contiguously from each w=1 anchor,
picks the longest run anywhere past `table_b_end`.

| run vs declared total | chunks (raw incl. duplicates) |
| --------------------- | ----------------------------: |
| `run == total`        |                            95 |
| `run > total`         |                             0 |
| `run <  total`        |                           195 |

**Key reinterpretation:** the `run < total` chunks are *not* missing
data — the walker can't see across the 32·sep_mult separator bytes
between sections, so the "run" it measures is really the largest
single contiguous section's instance count. The real fix just needs
the separator-aware walker (which already exists in
`parse_position_table`) to be handed a correct `pos_table_start`.

---

## 2026-04-18 — probe 6: end-to-end fix simulation

Proposed fix:
1. Widen `_find_first_w` scan window from 256B to 8192B.
2. Drop the "second consecutive w=1" check when the first non-empty
   section has only 1 instance (nothing to anchor on — the bytes at
   `first_w + 96` are separator, not another entry).
3. Keep the existing separator-aware per-section walker.

Simulated across every chunk where `parse_position_table` currently
returns `None`:

| metric                            | count |
| --------------------------------- | ----: |
| raw failing (incl. duplicates)    |   290 |
| **unique chunks failing**         |  **29** |
| fix recovers fully                |    28 |
| fix recovers partially (1 of 2)   |     1 |
| fix can't find pos_start          |     0 |
| approx instances recovered        | 1,536 |
| approx instances still lost       |     2 |

The 290/29 ratio means bin.zip stores each pgeo ~10× (case variants
and/or per-LOD duplicates — the extractor already handles this).

**Partial case:** `__R00G06439.pgeo` (sc=7, 2 sections with data, 5
instances). Section 0's first entry aligns at pos_table_start; the
other non-empty section's first entry doesn't land on w=1. Almost
certainly the separator-formula quirk from probe 3 (the 37 "drift"
chunks — separator is `52 * sep_mult` instead of `32 * sep_mult` in
these). Living with 2 lost instances out of 1,536 recovered is fine
for now; the 37-chunk drift is a separate follow-up.

### Cost/benefit

- 28 chunks × ~50 instances each ≈ 1,500 placements currently
  collapsed to chunk-anchor (1 per referencing chunk, scattered across
  the map) become real per-instance placements.
- The chunks involved include `__R00G07048.pgeo` (292 instances),
  `__R00G06682.pgeo` (58), `__R00G06534.pgeo` (33) — good candidates
  to contain the cables / blast-furnace placements seen scattered or
  ringed in the .blend render.

### Fix is a ~5-line change

`src/fh1_mapdecomp/pgeo_body/v42k7.py`, in `parse_position_table`:
- Change `scan_end = min(table_b_end + 256, ...)` to `+ 8192`.
- Wrap the "second w-marker" check in `if records[first_nonempty].instance_count > 1`
  (it already has this guard but the anchor also runs the check for
  count==1 via the `second + 0x10 > len(buf)` path — refactor to be
  explicit).

Ready to implement. Re-render after applying and see if the ring /
scattered artefacts collapse.

---

## 2026-04-18 — fix applied

Four-part change landed in `parse_position_table`:

1. **Scan window** 256 → 8192 bytes (catches 25+12+20+17 = 74 chunks
   whose position table is past the old window).
2. **Finite-triple sanity** added at the anchor offset to avoid false
   positives now that the window is wider.
3. **Removed `pos_table_start < table_b_end` guard.** When leading
   sections are empty, the `leading_sep` back-up can push
   `pos_table_start` a few bytes before `table_b_end`; the w-anchor
   already confirmed alignment, so the guard was spurious-reject.
4. **Zero-padded buffer view** so `struct.unpack_from` never overruns
   when the engine truncated the final entry on disk (the 14 small
   chunks where the last 96B record's rotation row 2 sits past the
   file end — pos + w + rows 0/1 all fit, row 2 reads as zeros).

### Post-fix verification

Full census of colorado/bin.zip (2,031 unique v42k7 chunks,
deduped — bin.zip keeps ~7× copies per file):

| metric                       | value   |
| ---------------------------- | ------: |
| table B parseable            |   1,537 |
| of which nonempty            |     660 |
| parse_position_table OK      | **660** |
| parse_position_table None    |   **0** |
| declared instances           | 116,459 |
| emitted instances            | 110,504 |
| emit/declare ratio           |   0.949 |

The 5.1% shortfall is the expected per-section trailer (rotation rows
hold cull/fade constants; filtered out by `_rot_is_placement`). No
regressions on previously-clean chunks.

### Extractor re-run

Ran `fh1-mapdecomp v42k7-inst` end-to-end against colorado/bin.zip.
Output: 11,258 chunks, 61,841 sections, 700,993 per-instance
transforms, 111 unique blobs. Offender-handle counts after fix:

| handle | tag                       | sections | instances |
| -----: | ------------------------- | -------: | --------: |
|  61910 | Shadow_Caster_005_LOD00   |    1,224 |     5,815 |
|  61915 | cables_512                |    2,746 |    37,003 |
|  61945 | cables_512                |    1,282 |    21,065 |
|  61985 | PLA_SMELTER_BlastFurnace  |      105 |     3,939 |
|  61916 | cables_570                |       76 |       459 |

`cables_570` fell from "slot 1/2 in 3,271 sections" to only 76 slot-0
sections — the slot-0-only heuristic plus the per-instance recovery
should collapse the scattered-everywhere artefact.

### Next

Blender re-import + topdown render to visually confirm:

    fh1-mapdecomp blender --output out \
      --source .../colorado/bin.zip
    fh1-mapdecomp render-topdown --output out

If the ring around the NW city collapses and the scattered shadow
casters disappear, the fix is done. If artefacts persist, the
remaining suspects are the 37 "drift" chunks (separator is
`52 * sep_mult` not `32 * sep_mult`) or hypothesis 3 (slot-0 dropping
the real mesh in some chunks).

---

## 2026-04-18 — visual check post-fix: artefacts unchanged

Reopened the .blend from the previous fix. Ring of blast furnaces
around NW city and scattered shadow casters look identical. Two
independent bugs uncovered when probing why:

### Bug A — pgeo case-variant over-emission

`bin.zip` stores every PGEO chunk with 1-26 lowercase duplicates that
are **byte-identical** to the uppercase original. Verified on
`__R00G06579.pgeo`: 1 uppercase + 18 lowercase copies, all MD5-equal.
Total zip: 7,970 uppercase .pgeo + 33,617 lowercase .pgeo = 41,587
entries, 7,970 case-insensitive uniques.

`extract_v42k7_instances` iterated every entry. Each unique chunk was
emitted up to 26× to `index.json`, and the Blender importer obediently
placed each instance that many times. Post-dedup census:

| metric             | before | after |
| ------------------ | -----: | ----: |
| chunks in JSON     | 11,258 |  1,379 |
| sections           | 61,841 |  6,996 |
| instances          |694,562 | 72,576 |
| unique blobs       |    111 |    111 |

Offender handle re-count:

| handle | tag                       | before | after |
| -----: | ------------------------- | -----: | ----: |
|  61910 | Shadow_Caster_005_LOD00   |  5,815 |   891 |
|  61915 | cables_512                | 37,003 | 4,936 |
|  61945 | cables_512                | 21,065 | 2,167 |
|  61985 | PLA_SMELTER_BlastFurnace  |  3,722 |   241 |
|  61916 | cables_570                |    412 |    49 |

**Fix:** `v42k7_inst.extract_v42k7_instances` now dedupes `pgeo_entries`
by case-insensitive filename. `rmb_entries` is left alone — handles
index into its duplicate-allowed positions, so collapsing the rmb list
would break mesh lookup.

### Bug B — `u0 = 0` Table B sections probably not placements

In `__R00G06579.pgeo` (7-section industrial chunk), sections split
cleanly on `seq[0]`:

| sec | u0 | cnt | u3  | sep | handle(tag)                    | per-inst const |
| --: | -: | --: | --: | --: | ------------------------------ | -------------- |
|   0 |  1 |   4 | 178 |   2 | CO_CLRD_Outhouse               | jittered ~0.5  |
|   1 |  1 |   7 |   5 |   2 | cables_544                     | jittered ~0.5  |
|   2 |  1 |  30 |  20 |   2 | OBJ_BarrierBrand               | jittered ~0.5  |
|   3 |  1 |   0 |  15 |   1 | SpeedCamera                    | —              |
|   4 |  1 |  38 |  19 |   4 | OBJ_FEST_CanopyClosed          | jittered ~0.5  |
|   5 |  1 |  23 |  14 |   2 | OBJ_BarrierBrand               | jittered ~0.5  |
|   6 |  1 |   5 |   3 |   2 | BLDG_MainTown_Modular_024_LOD00| uniform 0.5    |
|   7 |  0 | 136 |   0 |   1 | PLA_SMELTER_BlastFurnaceLow    | all-zero       |
|   8 |  0 |  79 |   0 |   1 | (skipped LOD)                  | all-zero       |
|   9 |  0 |  79 |   0 |   1 | cables_517                     | all-zero       |
|  10 |  0 |  39 |   0 |   1 | cables_526                     | all-zero       |

`u0=1` rows read cleanly (4 outhouses, 30 barriers, 5 buildings — plausible).
`u0=0` rows collectively claim 333 positions in a 240m × 200m area,
including 136 blast furnaces. That's the NW ring artefact.

Globally (sampled 2,000 chunks): 26,181 `u0=1` instances vs 12,550
`u0=0` instances. Per-handle:

| handle | tag                       | u0=0 | u0=1 |
| -----: | ------------------------- | ---: | ---: |
|  61985 | BlastFurnaceLow           |  149 |    0 |
|  61910 | Shadow_Caster_005_LOD00   |    0 |  279 |
|  61915 | cables_512                |    0 | 1,806 |

BlastFurnace is exclusively `u0=0`; shadow casters + cables are
exclusively `u0=1`. Suggests `u0=0` encodes a separate mesh-placement
semantics (occluder proxies? visibility cells? impostor ring?) that
should NOT render the section's hero mesh at each position. Not
decoded yet; filtering them out is the safe next move.

### Priority

1. Dedup fix is landed — re-render and see how much of the ring
   survives. Expect cluster density to drop ~15×.
2. If the ring persists, add `if record.u0 == 0: skip` to the
   extractor's instance loop and re-render. That would take
   BlastFurnace from 241 → 0 placements — acceptable until the real
   semantics of `u0=0` is decoded.
3. Shadow_Caster_005 is a valid rendering-pipeline artefact (a mesh
   that the engine only uses for shadow casting). Placement count of
   891 is correct-looking; filter it out of the Blender export by
   tag (`Shadow_Caster_*`) rather than by count.

---

## 2026-04-18 (session 2) — visual check after LFH-bug fix build

Rebuilt `dist/fh1-mapdecomp` with the standard-zip LFH handling and
re-ran `all`. The .blend render is still chaotic: lots of duplicates
scattered everywhere, some faint road-following clumps. Root cause of
what's visible now is **not a placement-math bug** — it's that the
v42k7 extractor is not applying the tag keep/drop policy documented
in `docs/freeroam-placement.md` and `project_fh1_freeroam_placement.md`.

### Handle-instance census of current `out/v42k7_inst/index.json`

1,377 chunks, 6,480 sections, 58,935 instances, 70 unique blobs after
u0=0 filter. Top handles by instance count:

| inst  | handle | tag                                     | category                          |
| ----: | -----: | --------------------------------------- | --------------------------------- |
| 7,536 |  61933 | `OBJ_BarrierBrand_LOD00_`               | race barrier — should drop        |
| 5,484 |  61898 | `CO_CLRD_Outhouse`                      | duplicate of CollObjs placement   |
| 4,936 |  61915 | `cables_544`                            | free-roam — keep                  |
| 4,523 |  61929 | `OBJ_BarrierBrand_LOD00_`               | race barrier — drop               |
| 3,696 |  61943 | `Barrier_016_Armco_…`                   | race barrier — drop               |
| 2,897 |  61908 | `Barnfind_Barn__LOD00`                  | suspicious: Colorado has ~30 BFs  |
| 2,732 |  61923 | `O_CO_FEST_SpeedCamera_001`             | race — drop                       |
| 2,620 |  61941 | `OBJ_BarrierBrand_LOD00_`               | race — drop                       |
| 2,533 |  61900 | `S_Debris`                              | free-roam — keep                  |
| 2,167 |  61945 | `cables_512`                            | free-roam — keep                  |
| 1,629 |  61921 | `OBJ_FEST_CanopyClosed`                 | festival — drop                   |
| 1,043 |  61911 | `GrandstandStraight_LOD00_`             | race — drop                       |

Race/festival dressing (`OBJ_BarrierBrand*`, `Barrier_*`, `Grandstand*`,
`Countdown*`, `Ambulance*`, `OBJ_FEST*`, `O_CO_FEST_*`) plus
CollObjs-layer duplicates (`CO_*`) account for **≥40,000** of the
58,935 instances — ~70% of the clutter the user is seeing.

### `Barnfind_Barn__LOD00` is placed 2,897× — almost certainly wrong

Colorado has roughly 30 Barn Finds. The barn mesh is being dragged in
as a section slot-0 handle for chunks that don't represent barns.
Candidate explanations:
- The section's slot-0 handle is a generic "landmark" entry pointing
  at the barn mesh and slot-1 is the real mesh for that chunk. Current
  extractor keeps slot 0 only, so we trust the wrong handle.
- Barn handle is used as a default/placeholder by the engine when a
  section has no renderable asset (similar to `Shadow_Caster_005`).

Either way the fix is tag-based drop for this specific handle until
the slot semantics is decoded.

### Fix plan

1. Add a `--v42k7-policy {freeroam,all}` CLI flag to `all` and
   `v42k7-inst`, default `freeroam`, mirroring the CollObjs pattern.
2. In `v42k7_inst.extract_v42k7_instances`, after resolving the
   section's primary handle's rmb tag, apply the keep/drop rules from
   `docs/freeroam-placement.md`:
   - **Drop if tag matches any of:** `OBJ_BarrierBrand`, `Barrier_0`,
     `Barrier_016`, `Grandstand`, `Countdown`, `Ambulance`, `OBJ_FEST`,
     `O_CO_FEST_`, `PROC_cars`, `Shadow_Caster`, `Barnfind_Barn__LOD00`
     (the over-emitted specific handle, until we decode slot semantics).
   - **Drop if tag starts with `CO_` or `OBJ_CLRD_`** — CollObjs
     already owns these placements.
   - **Keep** everything else (`BLDG_*`, `MT_Area*`, `MainTown_*`,
     `Plains_*`, `RV_*`, `MiningTower*`, `Pavementcap_*`, `cables_*`,
     `S_Debris`, `TERR_*` via terrain_hi already, etc.).
3. Re-run and re-render; expect instance count to drop from 58,935
   to roughly 15,000 with race-dressing gone.

### Still open after that

- `Barnfind_Barn__LOD00` 2,897 → 0 under the drop rule is fine for
  visuals but hides the real question: what slot convention means
  "render this handle"? Decoding that probably requires re-examining
  the 64B section record for a render/proxy flag field. Not blocking
  a first-pass free-roam export.
- The 37 `separator = 52·sep_mult` drift chunks are still unfixed;
  low priority given the current churn sits upstream of that.
