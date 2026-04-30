# Phase 2: callers of anchor strings — DEAD END (debug-only strings)

## Summary

Every anchor string from Phase 1 is **physically present in `.rdata`** but
**zero code references** any of them — neither via `lis+addi` immediate
construction (adjacent OR up to 64-instruction-lookahead), nor via a u32
BE pointer-table entry anywhere in the binary, nor via `lis+ori`.

```
For va=0x822394e3  (rmb_load_format printf "%s\\bin\\%s.%05d.rmb.bin"):
  lis r*, 0x8224  instructions in .text:  2,257
  matching addi r*,r*,0x94e3:                 0
  literal bytes 82 23 94 e3 (BE u32 anywhere): 0

For va=0x4F454750  (OEGP magic constant):
  lis r*, 0x4F45  instructions in .text:      0   (constant never built immediate)

For va=0x832c3df0  ("OEGP" string in .data):
  addi r*,r*,0x3df0:                          4   (no adjacent matching lis)
```

The printf format strings are present because the C source mentions them in
`assert(...)` macros / `printf(...)` debug calls. In a retail build, the
asserts are `#define`d away; the strings remain in `.rdata` as dead data
because the linker doesn't know they were only referenced from preprocessor-
killed code paths.

This rules out the anchor-string approach for finding the chunk-load
function. The strings can't lead us anywhere because no code uses them.

## Side-finding: complete function table available

The `.pdata` section is the standard PE function-descriptor table:

- File offset: 0x366e00
- 57,127 × 8-byte entries
- 57,122 of them have `begin_addr` in `.text` VA range (0x823e0000..0x831ee634)
- Each entry: `(begin_addr u32 BE, info u32 BE)` where info encodes prolog
  length, function length in words, and flags

**This means we have an exhaustive enumeration of every function in the
binary.** We can disassemble any subset of them.

First 12 entries (sample):

```
[   0]  begin=0x823e0040
[   1]  begin=0x823e0198
[   2]  begin=0x823e02a0
[   3]  begin=0x823e0310
[   4]  begin=0x823e0360
[   5]  begin=0x823e0770
[   6]  begin=0x823e08d0
[   7]  begin=0x823e09a0
[   8]  begin=0x823e0a40
[   9]  begin=0x823e0bd8
[  10]  begin=0x823e1840
[  11]  begin=0x823e1960
```

(Note: the "length" interpretation in info is encoded — the field's exact
bit layout per Xenon ABI is needed for accurate sizes. The begin addresses
are clearly correct because of strict ordering.)

## What this leaves us

Without working string anchors, finding the chunk-load function requires
either:

### Option A: Live anchor identification

Find a string we KNOW is referenced from runtime code (not asserts). Good
candidates:

- A vtable RTTI name like `?AVCPreLoadObject@@` (we found this at va=0x832ba2c4)
- A specific filename hardcoded in production: `ANIM_GPLY_Laser_Checkpoint.pgeo`
  IS referenced (it's a real ANIM file the game loads)
- A debug overlay string the game DOES print at runtime
- The XAUDIO/XAS streaming entrypoint name strings

We haven't tested whether `ANIM_GPLY_Laser_Checkpoint.pgeo` (va=0x8200d234)
has live references. If it does, the function loading it might be near the
chunk-loader.

### Option B: Reverse from data structures

We know the in-memory layout the engine uses (rmb pool entries, v42k7 chunks,
etc.). If we find global pointers or arrays in `.data` that look like
"current loaded chunks list" or "rmb pool array", the code that writes to
them is the loader.

### Option C: Function-shape heuristic search

Disassemble all 57k functions and score each by how "loader-shaped" it
looks: takes a filename/handle parameter, calls file-read functions, parses
4-byte u32 magic, has a switch on chunk class. Probably needs > 10 minutes
of compute and ~50 candidate inspections.

### Option D: Accept the wall

Static analysis without RTTI / debug symbols / Ghidra GUI is genuinely
limited. Practical alternative: **use the rmb_world + collobjs +
animatedobjects pipeline and add a hardcoded annotation file** for the
specific assets we know are missing (golf cart, haybale, autoshow tent,
grandstand, BarrierBrand). Per truth POI the user provides, manually place
those landmarks. ~10-20 entries total — tractable.

## Recommendation

Option **D** delivers a usable Blender world fastest. Options A/B/C are
worth pursuing only if the user wants 100% completeness and is willing to
accept multi-day investigation with high uncertainty of payoff.

## Files written

- `_callers.json` — empty (no hits found), kept for reproducibility
- This document

## Next phase (if continuing)

If the user picks A: implement live-anchor scan with longer string
candidates. If B: dump `.data` u32 pointer table and find what's a
loader-shaped function. If C: write the function-shape scorer.
