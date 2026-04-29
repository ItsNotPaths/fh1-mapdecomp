"""Decoder for v42k7 PGEO bodies — "Models_Ungrouped_*" instance groups.

Two layers live here:

1. ``decode()`` — the existing per-vertex point-cloud preview, kept so
   that the Blender "bbox + dots" path keeps working unchanged.
2. ``parse_layout()`` — the real instance-group structure: a list of
   sections, each pointing at 1..3 rmb-pool handles plus a 6-float
   record (local bbox or anchor) and a separate ``table_a`` of globally
   shared rmb handles. The export pipeline uses this to resolve the
   per-section geometry from ``rmb.bin``.

# Body layout (file-absolute offsets, validated on LARGE 14-section and
# small 1-section samples)

    0x00..0x34   OEGP header (handled by pgeo.parse_header)
    0x34..0x60   body header — fixed fields:
        0x3c  u32  n_minor / Table-B count
        0x40  u32  Table-A count       (number of "outer" rmb handles)
        0x48  u32  total_size = file_len
        0x4c  u32  sec_offset (often 0)
        0x50  u32  vbuf_size
        0x54  u32  section_count
    0x60..0x88   Name field (C-string in fixed 40B slot, includes
                 trailing null + ~12-19B preamble that's not yet
                 understood; the records always begin at 0x88).
    0x88..       section_count × 64B records (see _RECORD_STRIDE).

A 64-byte record:
    0x00..0x18   6 BE floats — currently exposed as ``floats``. The
                 (negative, 0, positive, positive, positive, 0) shape
                 across all sampled records suggests
                 (min_x, min_y, max_x, max_y, max_z, min_z); this is a
                 LOCAL-space bbox per section, not a transform — all
                 sections share the chunk-level world origin in
                 ``PgeoHeader.bbox_min/max``.
    0x18..0x1c   u32 padding (always 0).
    0x1c..0x34   up to three (count_u32=1, ptr_u32) pairs. Each non-zero
                 pair points into the post-records "section handle"
                 array. ``count==0`` marks an unused slot.
    0x34..0x40   u32 padding (always 0).

After the records:
    Section-handle array — N u32 BE rmb-pool handles, where N is the
    number of distinct pointer values across all section records (= sum
    of active pairs). Pointers are resolved as
        idx = (ptr - first_ptr) / 4
        handle = section_handles[idx]
    The two arrays overlap: indices >= len(section_handles) read into
    the start of ``Table A`` (whose layout is (handle, 0) pairs).
    Table A then sits as ``table_a_count`` × 8B records.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from fh1_mapdecomp.pgeo import PgeoHeader
from fh1_mapdecomp.pgeo_body import register
from fh1_mapdecomp.pgeo_body.types import MeshData


# --- legacy point-cloud decoder (keep working) -------------------------------

VBUF_SIZE_OFFSET = 0x50
VBUF_TRAILER = 4
RECORD_STRIDE = 40
ANCHOR_AT = 32
ANCHOR = b"\xe3\xd0"


def decode(buf: bytes, header: PgeoHeader) -> MeshData | None:
    if len(buf) < VBUF_SIZE_OFFSET + 4:
        return None
    vbuf_size = struct.unpack_from(">I", buf, VBUF_SIZE_OFFSET)[0]
    if vbuf_size == 0 or vbuf_size % RECORD_STRIDE != 0:
        return None

    vbuf_start = len(buf) - vbuf_size - VBUF_TRAILER
    if vbuf_start < 0x60:
        return None

    n = vbuf_size // RECORD_STRIDE

    for i in (0, 1 if n > 1 else 0, n - 1):
        off = vbuf_start + i * RECORD_STRIDE + ANCHOR_AT
        if buf[off:off + 2] != ANCHOR:
            return None

    bmin = np.array(header.bbox_min, dtype=np.float64)
    bmax = np.array(header.bbox_max, dtype=np.float64)
    rng = bmax - bmin

    view = np.frombuffer(buf, dtype=np.uint8, count=vbuf_size, offset=vbuf_start)
    records = view.reshape(n, RECORD_STRIDE)
    pos_bytes = records[:, 4:8].copy()

    packed = (
        pos_bytes[:, 0].astype(np.uint32) << 24
        | pos_bytes[:, 1].astype(np.uint32) << 16
        | pos_bytes[:, 2].astype(np.uint32) << 8
        | pos_bytes[:, 3].astype(np.uint32)
    )
    x10 = (packed & 0x3FF).astype(np.float64)
    y10 = ((packed >> 10) & 0x3FF).astype(np.float64)
    z10 = ((packed >> 20) & 0x3FF).astype(np.float64)

    positions = np.empty((n, 3), dtype=np.float32)
    positions[:, 0] = (bmin[0] + x10 / 1023.0 * rng[0]).astype(np.float32)
    positions[:, 1] = (bmin[1] + y10 / 1023.0 * rng[1]).astype(np.float32)
    positions[:, 2] = (bmin[2] + z10 / 1023.0 * rng[2]).astype(np.float32)

    return MeshData(positions=positions, faces=np.zeros((0, 3), dtype=np.uint32))


register("v42k7", decode)


# --- instance-group layout parser --------------------------------------------

RECORDS_OFFSET = 0x88
SECTION_RECORD_SIZE = 64

# Body-header u32 fields (file-absolute offsets).
_HDR_TABLE_B_COUNT = 0x3C
_HDR_TABLE_A_COUNT = 0x40
_HDR_TOTAL_SIZE    = 0x48
_HDR_SEC_OFFSET    = 0x4C
_HDR_VBUF_SIZE     = 0x50
_HDR_SECTION_COUNT = 0x54

# Proc subvariant: ``models_proc_clrd_*`` chunks. Distinguished by
# ``0xFFFFFFFF`` sentinel at file offset 0x60 and a longer name field
# starting at 0x98 (instead of 0x60). Records share the same 64-byte
# stride but use a 4-aligned bbox layout (16+16) — pair slots shift to
# offsets 0x24/0x2c/0x34 within the record (instead of 0x1c/0x24/0x2c).
_PROC_SENTINEL_OFFSET = 0x60
_PROC_SENTINEL = 0xFFFFFFFF
_PROC_NAME_OFFSET     = 0x98
_PROC_PAIR_OFFSETS    = (0x24, 0x2c, 0x34)
_REGULAR_PAIR_OFFSETS = (0x1c, 0x24, 0x2c)


@dataclass
class V42k7Section:
    """One section of a v42k7 instance group.

    ``rmb_handles`` lists the 1..3 global rmb-pool handles this section
    references — as resolved by direct (unshifted) index lookup into
    ``section_handles``. ``handle_indices`` carries the underlying raw
    indices so callers can apply a per-section shift (e.g. u0=0 sections
    in some chunks index the "metadata half" of the array and the engine
    renders the corresponding "render half" entry at idx + table_b_count
    - 1). ``floats`` is the raw 6-float prefix (LOCAL bbox).
    """
    floats: tuple[float, float, float, float, float, float]
    rmb_handles: list[int]
    handle_indices: list[int] = field(default_factory=list)


@dataclass
class V42k7Layout:
    """Parsed v42k7 body — section list plus shared Table A handles."""
    name: str
    section_count: int
    table_a_count: int
    table_b_count: int
    vbuf_size: int
    sections: list[V42k7Section] = field(default_factory=list)
    table_a: list[int] = field(default_factory=list)
    # The flat handle array that section pointers index into. Useful for
    # debugging; downstream consumers should prefer ``section.rmb_handles``.
    section_handles: list[int] = field(default_factory=list)
    # File offset where Table A ends / Table B begins. Populated after
    # parse_layout completes.
    table_b_offset: int = 0


@dataclass
class V42k7TableBRecord:
    """One Table B entry describing a section's slot in the position table.

    The 5-u32 "seq" word is interpreted as:
        u0   type flag (1 for all but the last section)
        u1   section index
        u2   instance count (the "A" field — drives position-table slicing)
        u3   secondary count/index (unused by placement)
        u4   separator multiplier C — the bytes between this section's
             instance block and the next section are 32 * C

    ``slot_skip_mask`` is the trailing u32 at +0x54 of the 88-byte record
    (BE u32). It is the per-section "skip mask" the engine uses to pick
    WHICH of the 1-3 handle slots actually render: bit N set = SKIP
    slot N (do not render this slot). Examples (validated):
        chunk 1463 sec[5] [Decals393, BarrierMetal_LOD00, BarrierMetal_LOD01]
            mask = 0x05 = 0b101 → skip slots 0 and 2 → render slot 1
            (BarrierMetal_LOD00, the canonical festival barrier)
        chunk 1463 sec[1] [TERR_CUBE, Countdown_Left]
            mask = 0x02 = 0b010 → skip slot 1 → render slot 0
            (Countdown_Left is race-only, not in freeroam)
        chunk 1463 sec[4] [Road31_LOD01, Decals08]
            mask = 0x05 → skip 0,2 → render slot 1 (Decals08)
    For 1-slot sections the field is occasionally re-purposed (sometimes
    holds a float ~1.0); callers should mask only the bits within the
    section's actual slot count.
    """
    section_index: int
    instance_count: int
    separator_mult: int
    u0: int
    u3: int
    attr_floats: tuple[float, float, float, float]
    extra_floats: tuple[float, float, float, float]
    cull_box: tuple[float, float, float, float]
    runtime_ptrs: tuple[int, int, int]
    slot_skip_mask: int = 0


@dataclass
class V42k7Instance:
    """A single per-instance transform read from the 96B position table."""
    position: tuple[float, float, float]
    rotation: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    up: tuple[float, float, float]
    section_constant: tuple[float, float, float, float]


def _is_proc_subvariant(buf: bytes) -> bool:
    if len(buf) < _PROC_SENTINEL_OFFSET + 4:
        return False
    return struct.unpack_from(">I", buf, _PROC_SENTINEL_OFFSET)[0] == _PROC_SENTINEL


def _proc_records_offset(buf: bytes) -> int:
    """Records start at the next 16-byte boundary after the name's
    null terminator. Name lives in 0x98 onward and may extend past 0xd0.
    """
    null_pos = buf.find(b"\x00", _PROC_NAME_OFFSET)
    if null_pos < 0:
        return -1
    return (null_pos + 16) & ~15


def parse_layout(buf: bytes, header: PgeoHeader | None = None) -> V42k7Layout | None:
    """Parse a v42k7 PGEO body into its instance-group structure.

    Handles both the regular ``Models_Ungrouped_*`` form and the proc
    subvariant (``models_proc_clrd_*`` — sentinel ``0xFFFFFFFF`` at
    0x60, name at 0x98, 4-aligned bbox layout in section records).

    Returns ``None`` for samples that don't match the section-record
    schema (e.g. anim/crowd proc chunks with no sections).
    """
    if len(buf) < RECORDS_OFFSET + SECTION_RECORD_SIZE:
        return None

    table_b_count = struct.unpack_from(">I", buf, _HDR_TABLE_B_COUNT)[0]
    table_a_count = struct.unpack_from(">I", buf, _HDR_TABLE_A_COUNT)[0]
    total_size    = struct.unpack_from(">I", buf, _HDR_TOTAL_SIZE)[0]
    vbuf_size     = struct.unpack_from(">I", buf, _HDR_VBUF_SIZE)[0]
    section_count = struct.unpack_from(">I", buf, _HDR_SECTION_COUNT)[0]

    if total_size != len(buf):
        return None
    if section_count == 0 or section_count > 256:
        return None

    is_proc = _is_proc_subvariant(buf)
    if is_proc:
        records_offset = _proc_records_offset(buf)
        if records_offset < 0:
            return None
        pair_offsets = _PROC_PAIR_OFFSETS
        name = buf[_PROC_NAME_OFFSET:buf.find(b"\x00", _PROC_NAME_OFFSET)].decode(
            "ascii", errors="replace",
        )
    else:
        records_offset = RECORDS_OFFSET
        pair_offsets = _REGULAR_PAIR_OFFSETS
        # Regular: name field is a C-string starting at 0x60; max 40 bytes.
        name_end = buf.find(b"\x00", 0x60, 0x88)
        name = buf[0x60:(name_end if name_end != -1 else 0x88)].decode(
            "ascii", errors="replace",
        )

    records_end = records_offset + section_count * SECTION_RECORD_SIZE
    if records_end > len(buf):
        return None

    # First pass: pull (floats, raw_ptrs[]) per record and gather the global
    # base = min(non-zero ptr) so we can resolve indices.
    # The (count, ptr) pairs are stored with BE counts but LE pointers —
    # the pointer field is raw runtime memory of an LE host, while the
    # rest of the file is BE. Read counts as BE and pointers as LE.
    raw_records: list[tuple[tuple[float, ...], list[int]]] = []
    all_ptrs: list[int] = []
    for i in range(section_count):
        off = records_offset + i * SECTION_RECORD_SIZE
        if is_proc:
            # Proc records: bbox_min vec3 + pad at +0x00, bbox_max vec3 + pad
            # at +0x10. Expose as 6 floats matching the regular `floats`
            # field so downstream interpretation stays uniform.
            bmin = struct.unpack_from(">3f", buf, off + 0x00)
            bmax = struct.unpack_from(">3f", buf, off + 0x10)
            f6 = (bmin[0], bmin[1], bmin[2], bmax[0], bmax[1], bmax[2])
        else:
            f6 = struct.unpack_from(">6f", buf, off)
        ptrs = []
        for pair_off_in_rec in pair_offsets:
            pair_off = off + pair_off_in_rec
            cnt = struct.unpack_from(">I", buf, pair_off)[0]
            ptr = struct.unpack_from("<I", buf, pair_off + 4)[0]
            if cnt == 1 and ptr != 0:
                ptrs.append(ptr)
        raw_records.append((f6, ptrs))
        all_ptrs.extend(ptrs)

    if not all_ptrs:
        return None

    base_ptr = min(all_ptrs)
    max_ptr  = max(all_ptrs)
    n_handles = (max_ptr - base_ptr) // 4 + 1

    # The handle array usually starts immediately after the records (the
    # standard 64B-aligned layout used for multi-section samples). For
    # small/degenerate samples the records may be packed more tightly; in
    # that case scan a small window around records_end for the first BE
    # u32 that looks like a valid rmb handle (< 0x40000) followed by
    # plausible handle-looking neighbours.
    def _looks_like_handle_block(off: int) -> bool:
        if off < 0 or off + n_handles * 4 > len(buf):
            return False
        for k in range(n_handles):
            v = struct.unpack_from(">I", buf, off + k * 4)[0]
            # 0 is the legitimate "null" value seen in real LARGE samples
            # for sentinel sections; anything else must fit the handle range.
            if v != 0 and not (0 < v < 0x40000):
                return False
        # Require at least one non-zero entry so we don't pick an all-zero
        # padding window.
        return any(struct.unpack_from(">I", buf, off + k * 4)[0]
                   for k in range(n_handles))

    handles_off = -1
    for delta in (0, -4, -8, -12, -16, 4, 8, 12, 16):
        if _looks_like_handle_block(records_end + delta):
            handles_off = records_end + delta
            break
    if handles_off < 0:
        return None

    section_handles = [
        struct.unpack_from(">I", buf, handles_off + k * 4)[0]
        for k in range(n_handles)
    ]

    # Table A's 8B (handle, pad) records start right after the dedicated
    # handle array — but in LARGE-style samples the pointer range
    # overlaps the first table-A entry, so the canonical Table-A start
    # is `handles_off + n_dedicated * 4` where ``n_dedicated`` is the
    # number of contiguous in-range handles before any pad-shaped slot.
    # Locate it by scanning forward until we see ``table_a_count``
    # consecutive (handle != 0, pad == 0) records.
    table_a: list[int] = []
    probe = handles_off
    while probe + 8 <= len(buf) and len(table_a) < table_a_count:
        h, pad = struct.unpack_from(">II", buf, probe)
        if pad == 0 and 0 < h < 0x40000:
            table_a.append(h)
            probe += 8
        else:
            probe += 4

    # Direct resolution (no shift). Callers that need a shifted lookup
    # (e.g. u0=0 sections in certain chunks) can apply it via
    # handle_indices + a known shift.
    sections: list[V42k7Section] = []
    for f6, ptrs in raw_records:
        handles: list[int] = []
        indices: list[int] = []
        for p in ptrs:
            idx = (p - base_ptr) // 4
            if 0 <= idx < n_handles:
                handles.append(section_handles[idx])
                indices.append(idx)
        sections.append(V42k7Section(
            floats=f6, rmb_handles=handles, handle_indices=indices,
        ))

    return V42k7Layout(
        name=name,
        section_count=section_count,
        table_a_count=table_a_count,
        table_b_count=table_b_count,
        vbuf_size=vbuf_size,
        sections=sections,
        table_a=table_a,
        section_handles=section_handles,
        table_b_offset=probe,
    )


# --- Table B + position-table parsers ----------------------------------------

_TABLE_B_HEADER_SIZE = 16
_TABLE_B_RECORD_SIZE = 88   # payload bytes (FF separator is additional)
_FF_MARKER = b"\xff\xff\xff\xff"
_POSITION_ENTRY_SIZE = 96


def parse_table_b(buf: bytes, layout: V42k7Layout) -> list[V42k7TableBRecord] | None:
    """Decode the variable-length Table B records, one per section.

    Layout:
        [ 16B header: (0, section_count, vbuf_size, 0) ][ FFFFFFFF ]
        [ N records, each 88 payload bytes + FFFFFFFF separator ]
        (last record has no trailing FFFFFFFF; the position table follows.)

    Each 88-byte record payload:
        0x00..0x10   4 f32   attr
        0x10..0x20   4 f32   extra
        0x20..0x34   5 u32   seq = (u0, section_idx, count, u3, sep_mult)
        0x34..0x44   4 f32   cull box
        0x44..0x48   u32     pad (0)
        0x48..0x54   3 u32   LE runtime pointers
        0x54..0x58   u32     trailing
    """
    # parse_layout's probe can stop a few bytes short of Table B when the
    # post-Table-A region has small padding/sentinel entries. Locate Table B
    # by finding the first FFFFFFFF marker after the probe and walking back
    # by the 16B header size.
    marker_pos = buf.find(_FF_MARKER, layout.table_b_offset, layout.table_b_offset + 64)
    if marker_pos < 0:
        return None
    off = marker_pos - _TABLE_B_HEADER_SIZE
    if off < layout.table_b_offset - 64:
        return None
    # The header's 2nd and 3rd u32 must equal section_count and vbuf_size
    # respectively — this is what pins down the start.
    _, sc, vs, _ = struct.unpack_from(">4I", buf, off)
    if sc != layout.section_count or vs != layout.vbuf_size:
        return None
    # Cache the true Table B offset for downstream parse_position_table.
    layout.table_b_offset = off

    records: list[V42k7TableBRecord] = []
    cursor = off + _TABLE_B_HEADER_SIZE + 4  # past header + FF
    for i in range(layout.section_count):
        rec_end = cursor + _TABLE_B_RECORD_SIZE
        if rec_end > len(buf):
            return None
        attr = struct.unpack_from(">4f", buf, cursor + 0x00)
        extra = struct.unpack_from(">4f", buf, cursor + 0x10)
        seq = struct.unpack_from(">5I", buf, cursor + 0x20)
        cull = struct.unpack_from(">4f", buf, cursor + 0x34)
        ptrs = struct.unpack_from("<3I", buf, cursor + 0x48)
        slot_skip_mask = struct.unpack_from(">I", buf, cursor + 0x54)[0]
        records.append(V42k7TableBRecord(
            section_index=seq[1],
            instance_count=seq[2],
            separator_mult=seq[4],
            u0=seq[0],
            u3=seq[3],
            attr_floats=attr,
            extra_floats=extra,
            cull_box=cull,
            runtime_ptrs=ptrs,
            slot_skip_mask=slot_skip_mask,
        ))
        cursor = rec_end
        # All records except the last are followed by FFFFFFFF.
        if i < layout.section_count - 1:
            if buf[cursor:cursor + 4] != _FF_MARKER:
                return None
            cursor += 4

    return records


def parse_position_table(
    buf: bytes,
    layout: V42k7Layout,
    records: list[V42k7TableBRecord] | None = None,
    *,
    keep_all_rotations: bool = False,
) -> list[list[V42k7Instance]] | None:
    """Read per-section instance transforms from the 96B position table.

    Returns a list-per-section of ``V42k7Instance``. Returns ``None`` if the
    chunk has no multi-instance position table (single-instance chunks).

    ``keep_all_rotations``: regular chunks include a trailing per-section
    96B metadata record whose rotation rows hold cull/fade distance
    constants — those get dropped by the placement filter. For proc
    subvariant chunks ALL entries' rotation rows are cull metadata (not
    real rotations), so set this True to keep every entry; the caller
    must substitute identity rotation downstream.

    The table layout repeats per section:
        [ instance_count × 96B entries ]
        [ 32 * separator_mult bytes of separator/footer ]
    Last section has no trailing separator.

    Each 96B entry:
        +0x00   3 f32 BE  world position
        +0x0c   f32 BE    w (== 1.0)
        +0x10   4 f32 BE  section constant (LOD/cull attrs — not used here)
        +0x20   3 f32 BE  up vector
        +0x30   3 f32 BE  row 0 of rotation
        +0x40   3 f32 BE  row 1 of rotation
        +0x50   3 f32 BE  row 2 of rotation
    """
    if records is None:
        records = parse_table_b(buf, layout)
        if records is None:
            return None

    total_instances = sum(r.instance_count for r in records)
    if total_instances == 0:
        return None

    # Position table starts after the last Table B record. Most chunks
    # insert a small alignment/footer block (seen as ~52 bytes holding a
    # 1.0 scalar + 3 rotation-like rows), so scan forward for the first
    # 4-byte aligned offset where w (+0x0c) reads as 1.0 — that's the
    # first instance of the first non-empty section.
    table_b_end = (
        layout.table_b_offset
        + _TABLE_B_HEADER_SIZE + 4                       # header + FF
        + layout.section_count * _TABLE_B_RECORD_SIZE    # record payloads
        + (layout.section_count - 1) * 4                 # FF between records
    )
    first_nonempty = next(
        (i for i, r in enumerate(records) if r.instance_count > 0), -1
    )
    if first_nonempty < 0:
        return None
    # w is stored as exactly 1.0 (0x3f800000) in canonical entries. Most
    # chunks put the position table within ~256B of table_b_end, but a
    # minority tuck it up to ~8KB further out (behind a larger pre-table
    # footer). Scan the wider window; to keep false positives out, also
    # require the position triple at +0x00 to be finite-valued.
    w_marker = b"\x3f\x80\x00\x00"
    first_real = -1
    scan_end = min(table_b_end + 8192, len(buf) - _POSITION_ENTRY_SIZE) + 1
    for off in range(table_b_end, scan_end, 4):
        if buf[off + 0x0c:off + 0x10] != w_marker:
            continue
        px, py, pz = struct.unpack_from(">3f", buf, off)
        if not (abs(px) < 1e7 and abs(py) < 1e7 and abs(pz) < 1e7):
            continue
        # When the first non-empty section has more than one instance,
        # the next 96B must also start with w=1.0 — anchor on the repeat
        # rather than a lone float. For count==1 sections the bytes at
        # off+96 are separator (typically 32*sep_mult), not another
        # entry, so skip this check.
        if records[first_nonempty].instance_count > 1:
            second = off + _POSITION_ENTRY_SIZE
            if second + 0x10 > len(buf):
                continue
            if buf[second + 0x0c:second + 0x10] != w_marker:
                continue
        first_real = off
        break
    if first_real < 0:
        return None
    # Back up by the separator bytes consumed by any empty sections that
    # precede the first non-empty one, so the cursor walk lands correctly.
    # For chunks with empty leading sections, pos_table_start can fall
    # slightly before table_b_end — that's fine, the w-anchor confirmed
    # the walker lands correctly on real entries.
    leading_sep = sum(
        32 * records[j].separator_mult for j in range(first_nonempty)
    )
    pos_table_start = first_real - leading_sep

    # Small chunks occasionally truncate the final entry on disk (the
    # engine tolerates a partial read — pos/w are always present, but
    # rotation rows at +0x30..+0x60 may run past the file end). Work
    # from a zero-padded view so struct.unpack_from never overruns.
    padded = buf + b"\x00" * _POSITION_ENTRY_SIZE

    per_section: list[list[V42k7Instance]] = []
    cursor = pos_table_start
    for i, rec in enumerate(records):
        raw_instances: list[V42k7Instance] = []
        for _ in range(rec.instance_count):
            if cursor < 0 or cursor + 0x10 > len(buf):
                return None
            pos = struct.unpack_from(">3f", padded, cursor + 0x00)
            sec_const = struct.unpack_from(">4f", padded, cursor + 0x10)
            up = struct.unpack_from(">3f", padded, cursor + 0x20)
            r0 = struct.unpack_from(">3f", padded, cursor + 0x30)
            r1 = struct.unpack_from(">3f", padded, cursor + 0x40)
            r2 = struct.unpack_from(">3f", padded, cursor + 0x50)
            raw_instances.append(V42k7Instance(
                position=pos,
                rotation=(r0, r1, r2),
                up=up,
                section_constant=sec_const,
            ))
            cursor += _POSITION_ENTRY_SIZE
        # Each section ends with a 96B metadata record whose rotation
        # rows hold cull/fade distance constants (e.g. (300, 2500, 0) and
        # (50, 100, 220)) rather than a real rotation. Drop any entry
        # whose rotation rows exceed the plausible model-radius range.
        if keep_all_rotations:
            section_instances = raw_instances
        else:
            section_instances = [
                inst for inst in raw_instances if _rot_is_placement(inst.rotation)
            ]
        per_section.append(section_instances)
        # Advance past the separator between sections (none after the last).
        if i < len(records) - 1:
            cursor += 32 * rec.separator_mult

    return per_section


_MAX_PLACEMENT_ROT_MAG2 = 100.0  # 10x unit vector, well above real model radii


def _rot_is_placement(rot) -> bool:
    """Return True if the 3x3 rotation looks like a real per-instance
    placement (all row magnitudes within ~10 units), False if it looks
    like the section-trailing cull/fade metadata record."""
    for row in rot:
        m2 = row[0] * row[0] + row[1] * row[1] + row[2] * row[2]
        if m2 > _MAX_PLACEMENT_ROT_MAG2:
            return False
    return True


# --- proc subvariant inline vertex buffer ------------------------------------

def parse_proc_inline_positions(
    buf: bytes, header: PgeoHeader,
) -> np.ndarray | None:
    """Decode the trailing 40B-stride packed-position vertex buffer found
    in proc subvariant chunks (``models_proc_clrd_trees_*``).

    Layout (file-tail):
        [ N × 40B records ][ 4B trailer ]
    Each record carries one packed position at offset +4..+8 as a BE
    u32 with the 10:10:10:2 layout (X|Y|Z|w2) — the same encoding used
    by the regular subvariant's per-vertex point cloud, but here the
    positions are world-space placements (one tree per record). Anchor
    bytes ``e3 d0`` sit at +32..+34 of every record and are validated
    on the first/middle/last entry to reject false positives.

    Returns a ``(N, 3) float32`` array of world-space positions
    (Y-up game frame, same as ``parse_position_table``), or ``None``
    when the chunk has no inline vbuf (most ``models_proc_clrd_*``
    chunks: ``vbuf_size == 0`` in the body header).

    The mapping from packed integer to world coords is a linear
    interpolation between ``header.bbox_min`` and ``header.bbox_max``
    (per-axis 10-bit fraction of the chunk's local extent).
    """
    if len(buf) < VBUF_SIZE_OFFSET + 4:
        return None
    vbuf_size = struct.unpack_from(">I", buf, VBUF_SIZE_OFFSET)[0]
    if vbuf_size == 0 or vbuf_size % RECORD_STRIDE != 0:
        return None

    vbuf_start = len(buf) - vbuf_size - VBUF_TRAILER
    if vbuf_start < 0x60:
        return None

    n = vbuf_size // RECORD_STRIDE
    for i in (0, 1 if n > 1 else 0, n - 1):
        off = vbuf_start + i * RECORD_STRIDE + ANCHOR_AT
        if buf[off:off + 2] != ANCHOR:
            return None

    bmin = np.array(header.bbox_min, dtype=np.float64)
    bmax = np.array(header.bbox_max, dtype=np.float64)
    rng = bmax - bmin

    view = np.frombuffer(buf, dtype=np.uint8, count=vbuf_size, offset=vbuf_start)
    records = view.reshape(n, RECORD_STRIDE)
    pos_bytes = records[:, 4:8].copy()

    packed = (
        pos_bytes[:, 0].astype(np.uint32) << 24
        | pos_bytes[:, 1].astype(np.uint32) << 16
        | pos_bytes[:, 2].astype(np.uint32) << 8
        | pos_bytes[:, 3].astype(np.uint32)
    )
    x10 = (packed & 0x3FF).astype(np.float64)
    y10 = ((packed >> 10) & 0x3FF).astype(np.float64)
    z10 = ((packed >> 20) & 0x3FF).astype(np.float64)

    positions = np.empty((n, 3), dtype=np.float32)
    positions[:, 0] = (bmin[0] + x10 / 1023.0 * rng[0]).astype(np.float32)
    positions[:, 1] = (bmin[1] + y10 / 1023.0 * rng[1]).astype(np.float32)
    positions[:, 2] = (bmin[2] + z10 / 1023.0 * rng[2]).astype(np.float32)
    return positions
