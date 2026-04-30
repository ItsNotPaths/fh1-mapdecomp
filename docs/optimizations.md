# Export pipeline — optimization targets

Where the time actually goes when running
`fh1-mapdecomp all --source <game-dir> --output ./out`. Vertex
byte-decoding is **not** the bottleneck — `np.frombuffer` on f32 BE
arrays runs at ~2 GB/s. The real time goes into thousands of OS-level
calls and a few inner loops still stuck in pure Python.

Ranked by likely impact, with file:line pointers and concrete fixes.

---

## 1. LZX subprocess fork/exec overhead — probably #1 cost

`src/fh1_mapdecomp/lzx.py:114` — every LZX-compressed bin.zip entry
triggers `subprocess.run([helper, ...])`. Each fork+exec is ~1-5 ms of
pure OS overhead before any decompression starts. The pipeline reads
tens of thousands of compressed entries (102k rmb + the ones we hit
in pgeo / pvsz / fiz lookups), so:

- **~50 k entries × ~3 ms fork overhead ≈ 150 s** of pure IPC, not
  counting the actual decompression
- The decode itself (libmspack in C) is fast; it's amortized over a
  tiny per-entry payload, so fork overhead dominates

**Fix.** Make `lzxd_helper` a long-running daemon — one fork at
startup, then read `[length, bytes]` from stdin / write decoded bytes
to stdout per request. Or wrap libmspack via ctypes to skip
subprocess entirely. Either should drop the LZX phase from minutes
to seconds.

---

## 2. Pure-Python triangle-strip decode

`src/fh1_mapdecomp/rmb.py:328` — `_strip_to_triangles` walks every
u16 index in pure Python (`for v in idx.tolist(): ...`), builds tuples
per triangle, appends to a list. With 24 k world-placed blobs averaging
hundreds of triangles per section and multiple sections each, that's
**tens of millions of Python iterations**. Single biggest pure-Python
hotspot in the codebase.

**Fix.** Vectorize with numpy:

- Split the index stream on `0xFFFF` once
- For each strip build (n-2, 3) triangle arrays via `np.column_stack`
  of rolled views
- Drop degenerate triangles with a single boolean mask
- Alternate winding via `[::2]` slicing on even/odd strip positions

Expected ~100× speedup on real loads.

---

## 3. Redundant tobytes() copies in vertex decode

`src/fh1_mapdecomp/rmb.py:267-268` — for each blob the position
decoder does:

    positions[:, i] = np.frombuffer(records[:, i*4:i*4+4].tobytes(), dtype=">f4")

The `.tobytes()` allocates and copies just to feed `frombuffer`.
Per-blob cost is small (~µs) but × 102 k blobs adds up.

**Fix.** Read positions as one strided view:

    np.frombuffer(buf, dtype='>f4', count=vc*3, offset=voff).reshape(vc, 3)

with explicit stride handling for vbufs where stride > 12, via
`numpy.lib.stride_tricks.as_strided` on the records buffer. One
buffer copy eliminated per blob.

---

## 4. `np.savez_compressed` × ~50 k files

`src/fh1_mapdecomp/rmb_world.py:378`,
`src/fh1_mapdecomp/v42k7_inst.py:461,525`,
`src/fh1_mapdecomp/terrain_hi.py:67`,
`src/fh1_mapdecomp/collobjs.py:349`.

Every blob saves to its own zlib-compressed `.npz`. For tens of
thousands of small files the zlib level-6 compression + filesystem
operations dominate. Reading them back from Blender re-pays the
same cost.

**Fix options (any of):**

- Switch to `np.savez` (uncompressed). Disk-space cost is small for
  vertex data; write speed jumps 5–10× and Blender-side read jumps
  the same.
- Batch many blobs into one archive (`.npz` per zone or per category,
  not per blob) and have the Blender importer index into it.
- Skip the save entirely for blobs that haven't changed since last
  run (file mtime check).

---

## 5. Blender bulk import

The Blender headless step starts up (~2 s) and creates 50 k+ meshes
via `bpy.data.meshes.new` + `from_pydata`. Per-mesh the dependency
graph updates and Blender's name-uniqueness check (linear scan over
existing names) get slow as the count climbs.

**Fix.** In `src/fh1_mapdecomp/blender_scripts/import_world.py`:

- Mute depsgraph updates during bulk import — single
  `bpy.context.view_layer.update()` at the end
- Pre-allocate object names without uniqueness checks (the importer
  already controls names)
- If per-blob picking in the outliner isn't needed, merge geometry
  into per-zone or per-category collections at import time (10–100×
  fewer Blender objects, much smaller .blend file)

---

## What is NOT slow (don't bother)

- **Vertex byte-decoding.** `np.frombuffer` is C-speed. A gigabyte
  of f32 BE positions decodes in well under a second.
- **bin.zip central-directory walk.** `binzip.list_entries` parses
  230 k entries in a few seconds, one-time at startup.
- **rmb header parse.** ~µs per entry; × 102 k = a few seconds total.
- **Sub-blob discovery.** `bytes.find()` is C-optimized; ~ms per entry.
- **HEXY / pvsz / col header parse.** Each file is small and read once.

---

## Suggested attack order

1. **LZX daemon** — biggest single win, smallest code change (the
   helper already takes one job per invocation on argv; just loop,
   reading job sizes from stdin).
2. **Vectorized `_strip_to_triangles`** — second-biggest pure-Python
   hotspot; one function rewrite.
3. **Uncompressed `np.savez`** — one-line change, immediate
   disk-write speedup; gates the next optimization.
4. **Blender bulk-import** — only worth tackling after upstream
   stages are fast, since they currently dominate.

After steps 1–3 the export should drop from ~minutes to ~tens of
seconds for the full Colorado pipeline.
