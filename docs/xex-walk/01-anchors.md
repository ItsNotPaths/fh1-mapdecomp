# Phase 1: anchor strings in default.bin

Locates string literals that high-value loader functions are likely
to reference. Each VA below feeds Phase 2 (callers).

Strings searched as raw bytes; first occurrence reported with its
virtual address. The file is loaded at base 0x82000000.

## PE sections

```
name           raw_off ..    raw_end    va_start ..     va_end  size  exec
.rdata             600..    366e00    82000600..  82366dfc  3565564  False
.pdata          366e00..    3d6800    82366e00..  823d6738  457016  False
.text           3d6800..   11e5000    823e0000..  831ee634  14738996  True
PAGELK         11e5000..   11e5a00    831ee800..  831ef18c    2444  True
_TEXT          11e5a00..   11e5e00    831ef200..  831ef59c     924  True
.data          11e5e00..   12d4e00    831f0000..  8350e3c8  3269576  False
.XBMOVIE       12d4e00..   12d5000    8350e400..  8350e40c      12  False
.NSPCH         12d5000..   12d5200    8350e600..  8350e604       4  False
.tls           12d5200..   12d8400    8350e800..  83511869   12393  False
.XEXID         12d8400..   12d8600    83511a00..  83511a04       4  False
.edata         12d8600..   12d8800    83520000..  835200f9     249  False
.idata         12d8800..   12d9000    83530000..  835306a8    1704  False
.XBLD          12d9000..   12d9200    83540000..  83540190     400  False
.reloc         12d9200..   1417200    83540200..  8367e0cc  1302220  False
```

## Anchor string findings

- ✓ `rmb_load_format_a` (b'%s\\bin\\%s.%05d.rmb.bin') — 1 hit(s)
  - first: file +0x2394e3, va=0x822394e3
  - context: `edia\tracks\%s\bin\%s.%05d.rmb.bin...game:\Med`
- ✓ `rmb_load_format_b` (b'\\%s\\bin\\%s.%05d.rmb.bin') — 1 hit(s)
  - first: file +0x2394e2, va=0x822394e2
  - context: `Media\tracks\%s\bin\%s.%05d.rmb.bin...game:\Med`
- ❌ `rmb_load_format_c` (b'game:\\Media\\tracks\\%s\\bin\\%s%05d.rmb.bin') — NOT FOUND
- ❌ `rmb_load_format_d` (b'%s\\bin\\%s\\%05d.rmb.bin') — NOT FOUND
- ✓ `pgeo_ext` (b'.pgeo') — 4 hit(s)
  - first: file +0xd24e, va=0x8200d24e
  - context: `r_Checkpoint.pgeo.ANIM_GPLY_L`
- ❌ `pgeo_format` (b'%s.pgeo') — NOT FOUND
- ✓ `sect_count_assert_a` (b'SectionCount > 0') — 2 hit(s)
  - first: file +0xba2e0, va=0x820ba2e0
  - context: `lder.cpp....SectionCount > 0....%s was n`
- ✓ `sect_count_assert_b` (b'SectionCount < MaximumSectionCount') — 2 hit(s)
  - first: file +0xba334, va=0x820ba334
  - context: `lder.cpp....SectionCount < MaximumSectionCount..%s was not`
- ✓ `oegp_magic` (b'OEGP') — 1 hit(s)
  - first: file +0x12b9bf0, va=0x832c3df0
  - context: `@C33?...>...OEGP...*...*...*`
- ✓ `collobjs_ref` (b'CollObjs') — 1 hit(s)
  - first: file +0x12290, va=0x82012290
  - context: `....FreePlayCollObjs........UPDA`
- ❌ `track_path` (b'Colorado_track_00') — NOT FOUND
- ❌ `ribbon_path` (b'Ribbon_00') — NOT FOUND
- ✓ `anim_hardcoded` (b'ANIM_GPLY_Laser_Checkpoint.pgeo') — 1 hit(s)
  - first: file +0xd234, va=0x8200d234
  - context: `atedObjects\ANIM_GPLY_Laser_Checkpoint.pgeo.ANIM_GPLY_L`
- ✓ `load_object_fn` (b'LoadObject') — 2 hit(s)
  - first: file +0x12b00c4, va=0x832ba2c4
  - context: `.....?AVCPreLoadObject@@......~...`
- ❌ `load_chunk_fn` (b'LoadChunk') — NOT FOUND
- ✓ `streaming_fn` (b'Streaming') — 40 hit(s)
  - first: file +0x7e3f6, va=0x8207e3f6
  - context: `ation.CClearStreamingBuffer...CCl`
- ❌ `world_chunk` (b'WorldChunk') — NOT FOUND

## Summary

- 10/17 anchor strings found
- Strongest candidates for chunk-load function entry:
  - `rmb_load_format_a` va=0x822394e3
  - `rmb_load_format_b` va=0x822394e2
  - `sect_count_assert_a` va=0x820ba2e0
  - `sect_count_assert_b` va=0x820ba334
  - `oegp_magic` va=0x832c3df0
  - `anim_hardcoded` va=0x8200d234

## Next: Phase 2

Phase 2 will scan the .text section for `lis rD, hi; addi rD, rD, lo`
PowerPC instruction pairs that compose any of these VAs. Each match
identifies a call site that references the string — likely from
inside the function we want to walk. We'll then identify the
enclosing function by walking backwards to find the prologue
(typical PPC: `mflr r12; stwu r1, -N(r1)` or `stwu r1, -N(r1)`).
