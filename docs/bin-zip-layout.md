# bin.zip — extension map and pool structure

Source: `4D5309C9/00007000/2DC7007B/media/tracks/colorado/bin.zip`.
Surveyed 2026-04-17 by `src/scout_suffixes.py` (artifacts in
`out/probe/scout/`).

## 1. Top-level inventory

| ext         |   count |  uncomp MiB | notes |
| ----------- | ------: | ----------: | ----- |
| .bin        | 115,200 |       4,734 | dominated by `coloradoout.NNNNN.rmb.bin` — the geometry pool |
| .bix        |  42,657 |       1,281 | `BIX1` container (textures / GPU-resident blobs) |
| .pgeo       |  41,587 |         422 | PGEO metadata + placement chunks (this project's primary input) |
| .fiz        |  16,934 |         595 | numeric-named `0.fiz`..`3200.fiz`, likely foliage tiles |
| .sh         |   4,768 |         134 | spherical-harmonics tiles; filenames encode world coords |
| .soundscape |   4,675 |          75 | audio zone descriptors |
| .pvsz       |   3,502 |         366 | `__R00Z*.pvsz` — Potentially Visible Set data |
| .bundle     |     429 |         850 | `_0xHHHHHHHH.bundle` — big container files (material/template bundles?) |
| .fxobj      |     173 |           6 | shader object blobs under `shaders/track/…` |
| .fev / .fsb |      66 |      0.7/56 | FMOD event / sound banks |

Total entries: **230,057**.

## 2. Naming conventions

| suffix        | pattern                           | population | interpretation                       |
| ------------- | --------------------------------- | ---------: | ------------------------------------ |
| `.pgeo`       | `__R<ribbon>G<gen>.pgeo`          |     41,587 | ribbon/generation-indexed            |
| `.pvsz`       | `__R<ribbon>Z<idx>.pvsz`          |      3,502 | ribbon-indexed PVS data              |
| `.bix`        | `_0xHHHHHHHH.bix`                 |     37,423 | hash-keyed, magic `BIX1`             |
| `.bix`        | `_0xHHHHHHHH_B.bix`               |      5,234 | hash-keyed, no BIX1 magic (GPU-packed texture tiers 8 KiB / 128 KiB) |
| `.bin`        | `_0xHHHHHHHH.bin`                 |     13,187 | hash-keyed misc                      |
| `.bin`        | `coloradoout.NNNNN.rmb.bin`       |    102,012 | **geometry pool** (see §3)           |
| `.bundle`     | `_0xHHHHHHHH.bundle`              |        429 | hash-keyed big bundles               |
| `.fiz`        | `N.fiz` (N = 0..3200)             |     16,934 | numeric, dense range                 |
| `.sh`         | `Colorado__shdata__n2550x_n1645z.sh` etc. | 4,768 | coord-keyed SH lighting          |
| `.fev/.fsb`   | category-named (`AMB_Reservoir`)  |         66 | FMOD banks                           |

There are **no sibling file pairings** — no entry of any extension
shares a stem with any `.pgeo`. PGEOs are self-contained against the
filename namespace; cross-file references go through numeric indices
or hashes embedded in body content, not through filename stems.

## 3. The `rmb.bin` geometry pool

`coloradoout.NNNNN.rmb.bin` (N = 0..14,559 unique, 14,291 populated)
is by count and bytes the largest single bucket in bin.zip:

- **102,012 entries**, but only **14,291 unique NNNNN indices** — most
  indices appear multiple times (median 7; max 33 copies of index
  774). This is **pseudo-zip duplication**: the central directory
  contains many entries with the same path pointing at different
  `header_offset` blobs (same pattern Turn 10 use for ribbon PGEO
  duplicates).
- Size range: 1.2 KiB .. 3.0 MiB per blob; average ~45 KiB. Total
  uncompressed ≈ 4.4 GiB.
- First 32 bytes of every sample start with `00 00 00 06` followed by
  3× f32 BE world-coord triplets. The `06` is a version field; no
  top-level ASCII magic but there is an ASCII **tag** (e.g.
  `TERR_CLRD_Redstone_Area05_LOD00_14`) at roughly `0xb0`.
- **Partially RE'd** for `TERR_*` blobs — see `docs/rmb-bin.md`. Envelope,
  vertex block (f32 BE world positions, stride 28 or 32) and tag
  decoded; index + material section (triangle strips with `0xFFFF`
  restart, interleaved `Material__NN` strings) still unparsed. Non-TERR
  blobs have not been probed.
- `TERR_*_LOD00_*` alone yields 2,569 tiles / 5.06M vertices covering
  the drivable Colorado surface with real elevation (Y = -39 .. 838 m).
  Consumed via `fh1-mapdecomp terrain-hi`.

## 4. PGEO → external-resource references

In-PGEO "hash" columns (the count+hash table starting at body ~0xa4)
are **not** bin.zip asset hashes. Across 304 unique u32 values pulled
from 2,035 empty-vbuf v42k7 files, **zero** match any
`_0xHHHHHHHH[_B]?.bix` or any bin.zip filename as a substring.

The values instead form arithmetic sequences:

```
0xa0794404, 0xa4794404, 0xa8794404, 0xac794404, …
0x50d46a37, 0x54d46a37, 0x58d46a37, 0x5cd46a37, …
```

Low 24 bits constant; high byte advances by 4 per row. This is a
**GPU memory-pointer** signature (Xbox 360 D3D9 memory-export or
push-buffer address), not a content hash — Turn 10 baked the
VRAM/system-RAM offsets of each render-block into the PGEO at
compile time, and the runtime recomputes them against the live
resource base.

That means:

1. The "hashes" cannot be used as bin.zip lookup keys.
2. Whatever bin.zip entry holds the referenced geometry must be
   located by a **different** key (the numeric `NNNNN` in `rmb.bin`
   filenames, or a per-PGEO section table we haven't finished
   parsing).
3. If real triangle indices exist inside bin.zip, they plausibly live
   in `rmb.bin` and must be matched by size/shape, not by hash text.

See `docs/pgeo-body.md §2` for the in-PGEO offsets of the count+hash
table, and `out/probe/scout/REPORT.md` for the full evidence.
