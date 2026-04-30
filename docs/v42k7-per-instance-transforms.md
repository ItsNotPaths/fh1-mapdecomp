# v42k7 — per-instance transform decode

This is the working note for decoding **placement transforms** inside `v42k7`
PGEO chunks. The schema for the chunk-level header + section records is
already covered in [`pgeo-body.md` §3.2.5](./pgeo-body.md) and the auto-memory
"v42k7 section record schema". This file picks up where that ends:
**how each section's mesh is actually positioned in the world.**

Status as of 2026-04-17: structure largely identified, section→entry-range
mapping not yet finalised. Current Blender export uses a chunk-anchor
approximation (one mesh per section dropped at the chunk's bbox centre +
section local-bbox centre) and renders a recognisable Colorado outline.

---

## 1. Two flavours of v42k7 chunk

Across **12,057 v42k7 PGEO chunks** in `colorado/bin.zip`:

| Flavour                    | Count | Has 96-byte position table? |
| -------------------------- | ----- | --------------------------- |
| Multi-instance ("ribbon")  | 5,869 | yes                         |
| Single-instance / sparse   | ~6,188 | no                         |

Single-instance chunks place each section once at the chunk anchor, so the
section's local bbox plus the chunk header bbox is enough. Multi-instance
chunks contain a flat array of per-instance transforms after Table A; one
section's mesh is reused across many world points.

Stride distribution across the 5,869 chunks with a detectable position
table:

| Stride (bytes) | Chunks |
| -------------: | -----: |
| **96**         | 5,832  |
| 160            | 17     |
| 128            | 9      |
| 4              | 6      |
| 192            | 5      |

96-byte stride dominates so heavily that it's the only one decoded so far.

Average instance count grows with section count (chunks with more sections
also pack more instances):

| section_count | mean entries | sample chunk             |
| ------------: | -----------: | ------------------------ |
| 2             | 5            | small grid chunks        |
| 7             | 108          | `__R00G07014.pgeo` (173) |
| 22            | 444          | mid-size festival chunks |
| 30            | 566          | dense LARGE chunks       |

---

## 2. The 96-byte per-instance entry

Verified inside `__R00G07014.pgeo` (7 sections, position table starting at
`0x570`, 173 entries). All values **BE** unless stated.

```
0x00..0x0c   3× f32   world position (X, Y, Z, game axes)        ← confirmed
0x0c..0x10   1× f32   = 1.0                                       (w / scale?)
0x10..0x20   4× f32   chunk-wide constant (e.g. 0.4609, 0.4757, 0.4664, 0)
0x20..0x30   3× f32 + pad     unit-length up vector (~0.999 mag)
0x30..0x40   3× f32 + pad     X axis × radius
0x40..0x50   3× f32 + pad     Y axis × radius
0x50..0x60   3× f32 + pad     Z axis × radius
```

The 4×3 block at `0x20..0x60` is a rotation matrix scaled by an apparent
"model radius" (~0.378 in the sample). The chunk-wide constant at
`0x10..0x20` looks like a per-chunk attribute (LOD distance? cull bias? not
yet pinned down).

**Verification:** every position read this way fell inside the chunk's
bbox + 20m margin; nothing else in the body did. That's the strongest
single signal that this layout is correct.

**No section-index field is visible inside the 96B entry itself.** Mapping
each entry back to a section must come from outside the entry — see §3.

---

## 3. Per-section "Table B" — variable-length records

For chunks with the position table, the bytes between Table A's end and the
position-table start hold one variable-length record per section, separated
by `ff ff ff ff` markers. In `__R00G07014.pgeo` (section_count = 7),
exactly 7 records sit between `0x2a8` and `0x570` (712 bytes total).

Per-record layout (partially decoded):

```
4× f32   rotation/scale-ish      (e.g. 0.75, 1.0, 0.5, 0)
4× f32   per-section attributes  (e.g. 10.0, 7.54, 1.0, 0)
N× u32   sequence                (likely (count, start_idx, idx_a, idx_b)…
                                  partitioning the position table by section)
4× f32   cull box                (e.g. 220, 150, 150, 200 — chunk-extent scale)
3× u32   LE runtime pointers     (matching the 0x376a… pattern from the
                                  64B section records)
0xff 0xff 0xff 0xff              record separator
```

The u32 sequence is the unfinished part. Hypothesis: it encodes
`(instance_count, start_index_into_position_table, …)` pairs. If true,
slicing the 96-byte position array by these ranges recovers each section's
instance list.

The LE runtime pointers tie individual records back to other in-memory
structures (probably the same indexing trick we saw in the 64B section
records — the engine dumped its loaded structs straight to disk).

---

## 4. What's known vs. unknown

**Known:**
- Two flavours of chunk (multi-instance vs single-instance), reliably
  detectable by scanning for in-bbox 3-tuples at 4-byte alignment.
- Position-table stride is overwhelmingly 96 bytes.
- Per-entry layout: world position + chunk-wide 4-float + 4×3 rotation matrix.
- Per-section Table B records exist, are separated by `0xFFFFFFFF`, and
  contain cull/attribute floats plus a u32 sequence and LE pointers.

**Unknown:**
- Exact decoding of the u32 sequence inside a Table B record — specifically
  the section→entry-range mapping. Highest-value open question.
- Meaning of the chunk-wide 4-float at entry offset `0x10`. Plausible:
  LOD-distance band, fade-bias, alpha-test threshold.
- The "model radius" baked into the rotation rows (~0.378 in the sample) —
  whether it's per-instance scale or just a unit-length-multiplied-by-radius
  artefact of how the engine stored normals.
- The 5%-ish chunks with non-96 strides (160 / 128 / 192) — likely related
  variants. Defer until 96 is fully working.

---

## 5. Reproduction recipes

The probe scripts that produced these findings live in `/tmp/` (not
checked in):

| Script                       | What it does                                       |
| ---------------------------- | -------------------------------------------------- |
| `/tmp/scan_all_chunks.py`    | Stride/count distribution across all 12,057 chunks |
| `/tmp/scan_for_transforms.py`| Find in-bbox 3-tuples in a single chunk            |
| `/tmp/dump_entry.py`         | Hex-dump 96B entries with u32/f32 interpretations  |
| `/tmp/dump_gap.py`           | Hex-dump bytes between Table A end and position table |
| `/tmp/probe_preamble.py`     | Hex-dump body header + post-Table-A bytes          |

The canonical sample for hand-verification is **`__R00G07014.pgeo`**:
- 7 sections, 22 Table-B records → wait, 7 → 7 records.
- Position table: 173 entries × 96B starting at `0x570`.
- Table B: 7 variable-length records between `0x2a8` and `0x570`.
- All entry positions fall inside the chunk's bbox.

For chunks **without** a position table, `__R00G07016.pgeo` (3 sec) and
`__R00G07353.pgeo` (4 sec) are good controls — `scan_for_transforms.py`
returns 0 in-bbox 3-tuples on both.

---

## 6. Implementation roadmap

Six concrete steps to lift the export from chunk-anchor approximation to
true per-instance placement.

### Step 1 — Table B parser

In `src/fh1_mapdecomp/pgeo_body/v42k7.py`, add `parse_table_b(buf, layout)`
returning `list[TableBRecord]` indexed by section. Each record exposes:

```python
@dataclass
class TableBRecord:
    cull_box:     tuple[float, float, float, float]   # x, y, z, w
    attr_floats:  tuple[float, float, float, float]   # leading 4 f32
    extra_floats: tuple[float, float, float, float]   # following 4 f32
    entry_range:  tuple[int, int]                     # (start, count) in pos table
    runtime_ptrs: tuple[int, int, int]                # LE u32, kept for sanity
```

Anchor the start of Table B at `(records_end + n_handles*4 + table_a_count*8)`
(already known from `parse_layout`). Walk forward, splitting on
`0xFFFFFFFF`. The hard part is decoding the u32 sequence to fill
`entry_range` — start by treating it as `(count_u32, start_u32, …)` pairs
and verify by checking that `sum(counts) == position_table_entry_count`.

### Step 2 — 96B entry parser

Same module, `parse_position_table(buf, layout)` returning
`np.ndarray` of shape `(N, 16)` (or a structured dtype): position xyz,
the chunk-constant 4-tuple, and the 4×3 rotation rows. Detect presence by
scanning the first row's 12 bytes — three BE f32 inside chunk bbox + 20m.
Skip chunks where the test fails (these are the ~6k single-instance chunks).

### Step 3 — Update the extractor

`src/fh1_mapdecomp/v42k7_inst.py` currently emits one transform per section
(chunk anchor + section local-bbox centre). Change it to:

1. Call `parse_table_b` and `parse_position_table` per chunk.
2. For each section, slice the position table by the section's
   `entry_range` and emit one `(world_pos, rotation_matrix)` transform per
   instance.
3. Fall back to the current chunk-anchor scheme for chunks without a
   position table (~6k of them).

`index.json` schema bump: per-section `instances` becomes a list of
`{"pos": [x,y,z], "rot": [3×3]}` instead of a single `offset`.

### Step 4 — Update the Blender importer

`src/fh1_mapdecomp/blender_scripts/import_world.py` currently sets
`obj.location` only. Change to also set `obj.rotation_euler` (or
`matrix_world`) from each transform. Keep the real-instancing pattern
(one `bpy.data.meshes`, many `bpy.data.objects`) — only the per-object
transform changes.

### Step 5 — Visual verification

Re-render `colorado_topdown_full.png`. Expectation: dense ribbons and
clusters where the existing render shows uniform field-of-cubes (e.g. the
festival site, the main town grid). Compare side-by-side with the
chunk-anchor render.

### Step 6 — Stretch goals

- Decode the chunk-wide 4-float at entry `0x10` — likely LOD/cull, may let
  us drop low-LOD instances precisely.
- Decode the 5% non-96 stride chunks (160 / 128 / 192). Sample one of each
  and see if the same Table-B + N-byte-stride pattern holds with a wider
  per-entry record.
- Validate the apparent "model radius" in the rotation rows by comparing
  to the rmb mesh's actual extent — if it matches, we get free per-instance
  scale.

---

## 7. Cross-references

- [`pgeo-body.md`](./pgeo-body.md) — full PGEO body format reference.
- [`rmb-bin.md`](./rmb-bin.md) — geometry pool that the v42k7 handles index into.
- [`roadmap-full-export.md`](./roadmap-full-export.md) — overall extraction plan;
  this document is the M3 (`v42k7` roads + buildings) deep dive.
- Auto-memory `project_v42k7_schema.md` — 64B section record (chunk-level).
- Auto-memory `project_v42k7_per_instance.md` — short summary of this file.
