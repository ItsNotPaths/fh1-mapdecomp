"""Parser for ``coloradoout.NNNNN.rmb.bin`` — the shared geometry pool.

Each rmb.bin entry holds **one primary blob followed by zero or more
sub-blobs**. The primary blob's header begins at offset 0 of the entry
and is the canonical one (full transform/centroid metadata). Sub-blobs
share the same section grammar but have a stripped-down header and live
sequentially after the primary's section block. ``parse_blob`` returns
the primary blob with its sub-blobs attached as ``RmbBlob.sub_blobs``.

Section grammar (validated against 325k sections, 99.77% match):

    section_block := [u32 1][u32 N_sections] section{N_sections} trailer
    section       := [u32 5 if not first]
                     [u32 1][u32 2]              # streams=1, type=2 (string)
                     [u32 name_len][bytes×name_len name]
                     [4 NUL bytes pad]
                     [u32 = 6 or 4]              # version
                     [u32 section_idx]           # 0..N-1
                     [u32 = 1]                   # flag, always 1 in samples
                     [16 NUL bytes]
                     [4×f32 scale]               # always (1,1,1,1)
                     [4×f32 anchor]              # per-section, repeats
                                                 # identically across siblings
                                                 # of the same landmark
                     [u32 = 4][u32 = 0]          # index stream marker
                     [u32 idx_count][u32 = 2]    # elem_size = u16
                     [u16 BE × idx_count]        # tri-strip with 0xFFFF restart

The 60-byte metadata block (version through anchor inclusive) is the
deterministic anchor we use to walk sections — find the index-stream
marker, walk back 60 bytes to the version field, walk back further to
locate the streams-type marker `[1][2]` and decode the section name.

Sibling-instance convention (the "_NNN world-baked sibling" insight):
sections whose name ends in `_NNN` (1-4 ASCII digits, optionally after a
LOD suffix) are world-baked instance siblings rather than material/LOD
sub-meshes. A section like ``MOUN_DAM_BLDG_MainDam_003`` inside an
``Object010_LOD00`` wrapper is a placement of the dam asset; the
wrapper is a per-chunk visibility aggregator. ``RmbSection.nnn_index``
and ``RmbSection.is_nnn_sibling`` surface this directly.

Trailer: every section block is followed by a per-section material/
shader binding table which encodes Xbox 360 GPU register assignments
plus length-prefixed shader file paths (``shaders\\track\\...fx``).
The parser captures the raw bytes plus a best-effort list of shader
paths; the binding bytecode is not currently decoded.

--- Primary blob layout (all u32 BE unless noted, offsets from blob start):

    0x00  u32 BE   version (=6)
    0x04  3×f32    centroid
    0x10  pad u32
    0x14  3×f32    centroid (duplicate)
    0x20  16×f32   4×4 transform (identity in observed samples)
    0x60  u32      ?            (=4 in samples)
    0x64  u32      ?            (=4)
    0x68  u32      ?            (=4)
    0x6c  u32      pad 0
    0x70  3×u32    1, 1, 1
    0x7c  3×f32    blob centre (world)
    0x88  pad u32
    0x8c  3×f32    bbox_min (world)
    0x98  pad u32
    0x9c  3×f32    bbox_max (world)
    0xa8  pad u32
    0xac  u32      tag string count (=1)
    0xb0  u32      tag string length (T)
    0xb4  bytes×T  tag (ASCII, no padding)
    --    u32      stream block count (=3)
    --    u32      vertex count
    --    u32      vertex stride (16, 20, 24, 28, 32, 36 observed)
    --    u32      pad 0
    --    bytes    vertex data (vcount × stride)
    --    bytes    section data (one or more material/index blocks)

Sub-blob layout (compact 0x40-byte header, no transform field):

    0x00  u32 BE   marker (=5)
    0x04  u32 BE   ?       (=1 in all observed samples)
    0x08  4×f32    centroid + 4-byte pad
    0x18  4×f32    bbox_min + pad
    0x28  4×f32    bbox_max + pad
    0x38  u32      tag string count (=1)
    0x3C  u32      tag string length (T)
    0x40  bytes×T  tag
    --    u32      stream block count (=3)
    --    u32      vertex count
    --    u32      vertex stride
    --    u32      pad 0
    --    bytes    vertex data
    --    bytes    section data

Sub-blobs always start with the byte sequence ``00 00 00 05 00 00 00 01``.
The same byte sequence prefixes a section continuation marker (``[05]``
between sections within a single blob), but in that case the next u32 is
``0x00000002`` (the string-stream type code). For a real sub-blob the
next u32 is the first dword of a world-space float, which is never
``0x00000002`` in any plausible value range. That's the discriminator.

Vertex records always start with a world-space position as 3×f32 BE in
bytes 0..11; the remaining bytes (4..24 depending on stride) carry
per-record normal/UV/colour and are not decoded yet.

Index stream: triangle-strip indices are u16 BE with ``0xFFFF`` restart,
triangulated into a face list with the standard even/odd orientation
alternation.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from fh1_mapdecomp.binzip import Entry, list_entries, read_entry


VALID_STRIDES = (16, 20, 24, 28, 32, 36)
LOD_RE = re.compile(r"LOD(\d+)")
INDEX_STREAM_MARKER = b"\x00\x00\x00\x04\x00\x00\x00\x00"
SUB_BLOB_PREFIX = b"\x00\x00\x00\x05\x00\x00\x00\x01"
SECTION_CONT_TYPE = 2  # u32 BE that follows SUB_BLOB_PREFIX for a section continuation
STREAMS_TYPE_STRING = b"\x00\x00\x00\x01\x00\x00\x00\x02"  # streams=1, type=2 (string)
SECTION_META_SIZE = 60  # bytes of fixed-size metadata before each idx-stream marker
NAME_PAD_BYTES = 4      # NUL bytes between the section name and the metadata block

# `_NNN` suffix: 2+ trailing digits, optionally after a `_LODxx` qualifier (so
# `MainDam_003`, `VisitorCentre_04`, `Gondola_LOD00_001` all match). We require
# at least 2 digits because `_1`/`_2` ambiguously match per-section material
# variants (`grass_to_mud_1`, `dry_grass_3`) that aren't sibling instances.
# The trailing digit group is captured as the sibling instance index.
NNN_SUFFIX_RE = re.compile(r"(?:_LOD\d+)?_(\d{2,4})$")
# Length-prefixed `shaders\...` path embedded in the trailer. Captures: u32 BE
# length immediately before the literal `shaders\`, then `length` bytes.
SHADER_PATH_PREFIX = b"shaders\\"


@dataclass
class RmbMaterial:
    """One material entry in the trailer's MaterialSet (FM3 grammar).

    The ``texture_sampler_indices`` are the key for texture binding —
    each entry is an index into ``PvsModel.textures`` (which in turn
    indexes ``Pvs.textures[].texture_file_name`` = the integer naming
    of ``_0xHHHHHHHH.bin``). ``-2`` means "sampler unbound"; ``-1``
    means "instancer override" (per FM3, may differ on FH1).
    """
    fx_filename_index: int                          # into RmbTrailer.shader_paths
    pixel_shader_constants: np.ndarray              # (psc_length, 4) f32
    texture_sampler_indices: np.ndarray             # (tsi_length,) s32


@dataclass
class RmbSection:
    name: str
    indices: np.ndarray   # (K,) uint16, 0xFFFF = strip restart
    triangles: np.ndarray  # (M, 3) uint32 (triangulated from strips)
    section_idx: int = 0                 # 0..N-1 within the section block
    version: int = 6                     # observed: 6 (default), 4 (rare)
    flag: int = 1                        # 3rd u32 of metadata; always 1 in samples
    scale: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    anchor: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    is_nnn_sibling: bool = False         # name ends in `_NNN` (instance suffix)
    nnn_index: Optional[int] = None      # captured digit group (e.g. 3 for `_003`)
    shader_path: Optional[str] = None    # populated post-trailer-parse if known
    material: Optional[RmbMaterial] = None  # populated when material set parses


@dataclass
class RmbTrailer:
    """The per-blob trailer that follows the last section's index buffer.

    Carries Xbox 360 GPU register binding bytecode plus the shader file
    paths each section uses. We capture the raw bytes (so downstream
    callers can re-parse if/when we learn more about the binding bytes)
    and the extracted shader path list.
    """
    raw: bytes                                # full trailer bytes
    offset: int                               # absolute offset of trailer start
    shader_paths: list[str] = field(default_factory=list)
    materials: list[RmbMaterial] = field(default_factory=list)


@dataclass
class RmbSubBlob:
    """A non-primary blob packed into the same rmb.bin entry.

    Sub-blobs share the section grammar of the primary blob but carry a
    compact 0x40-byte header (no transform). They are common: roughly
    every other rmb.bin entry has at least one. A typical use is to pack
    multiple LODs or material variants of a related mesh into one entry
    (e.g. ``TERR_..._LOD02_06`` through ``_LOD02_09`` share an entry).
    """
    tag: str
    lod: str
    centroid: tuple[float, float, float]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    vcount: int
    stride: int
    voff: int                                # vertex bytes start within entry
    positions: np.ndarray                    # (N, 3) f32 game-space (Y-up)
    uvs: np.ndarray = field(                 # (N, 2) f32 in [0,1]; (0,0) if absent
        default_factory=lambda: np.zeros((0, 2), dtype=np.float32),
    )
    sections: list[RmbSection] = field(default_factory=list)
    trailer: Optional[RmbTrailer] = None


@dataclass
class RmbBlob:
    tag: str
    lod: str                                 # digits from "LOD\d+" or ""
    centroid: tuple[float, float, float]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    vcount: int
    stride: int
    voff: int                                # vertex bytes start within blob
    positions: np.ndarray                    # (N, 3) f32 game-space (Y-up)
    uvs: np.ndarray = field(                 # (N, 2) f32 in [0,1]; (0,0) if absent
        default_factory=lambda: np.zeros((0, 2), dtype=np.float32),
    )
    sections: list[RmbSection] = field(default_factory=list)
    sub_blobs: list[RmbSubBlob] = field(default_factory=list)
    trailer: Optional[RmbTrailer] = None
    entry: Optional[Entry] = None            # populated by iter_blobs


# -- header parse -------------------------------------------------------------

def _parse_header(buf: bytes) -> tuple[str, int, int, int, int,
                                       tuple[float, float, float],
                                       tuple[float, float, float],
                                       tuple[float, float, float]]:
    """Return (tag, vbuf_off, vcount, stride, post_off, centroid, bmin, bmax).

    Raises if the blob does not look like a valid rmb.bin primary blob.
    """
    if len(buf) < 0xc0:
        raise ValueError("blob too short for header")
    if struct.unpack_from(">I", buf, 0x00)[0] != 6:
        raise ValueError("version != 6")
    centroid = struct.unpack_from(">fff", buf, 0x7c)
    bmin = struct.unpack_from(">fff", buf, 0x8c)
    bmax = struct.unpack_from(">fff", buf, 0x9c)
    cnt = struct.unpack_from(">I", buf, 0xac)[0]
    tlen = struct.unpack_from(">I", buf, 0xb0)[0]
    if cnt != 1 or not (1 <= tlen <= 128):
        raise ValueError(f"bad tag header: cnt={cnt} tlen={tlen}")
    if 0xb4 + tlen + 16 > len(buf):
        raise ValueError("tag/stream-header overflows buffer")
    tag_bytes = buf[0xb4:0xb4 + tlen]
    if not all(0x20 <= b < 0x7f for b in tag_bytes):
        raise ValueError("non-ASCII tag")
    tag = tag_bytes.decode("ascii")
    tag_end = 0xb4 + tlen
    sc, vc, st, pad = struct.unpack_from(">IIII", buf, tag_end)
    if sc != 3:
        raise ValueError(f"stream block count {sc} != 3")
    if st not in VALID_STRIDES:
        raise ValueError(f"unsupported stride {st}")
    if not (0 <= vc <= 500_000):
        raise ValueError(f"vcount out of range: {vc}")
    voff = tag_end + 16
    post = voff + vc * st
    if post > len(buf):
        raise ValueError("vbuf overflows entry")
    return tag, voff, vc, st, post, centroid, bmin, bmax


def _parse_subblob_header(buf: bytes, off: int) -> tuple[str, int, int, int, int,
                                                         tuple[float, float, float],
                                                         tuple[float, float, float],
                                                         tuple[float, float, float]]:
    """Parse a sub-blob's compact 0x40-byte header at ``off``.

    Returns (tag, voff, vcount, stride, post_off, centroid, bbox_min, bbox_max).
    Raises if the header doesn't match the sub-blob shape.
    """
    if off + 0x50 > len(buf):
        raise ValueError("sub-blob header overflows buffer")
    if buf[off:off + 8] != SUB_BLOB_PREFIX:
        raise ValueError("missing sub-blob marker")
    centroid = struct.unpack_from(">fff", buf, off + 0x08)
    bmin = struct.unpack_from(">fff", buf, off + 0x18)
    bmax = struct.unpack_from(">fff", buf, off + 0x28)
    cnt, tlen = struct.unpack_from(">II", buf, off + 0x38)
    if cnt != 1 or not (1 <= tlen <= 128):
        raise ValueError(f"bad sub-blob tag header: cnt={cnt} tlen={tlen}")
    tag_off = off + 0x40
    if tag_off + tlen + 16 > len(buf):
        raise ValueError("sub-blob tag/stream-header overflows buffer")
    tag_bytes = buf[tag_off:tag_off + tlen]
    if not all(0x20 <= b < 0x7f for b in tag_bytes):
        raise ValueError("non-ASCII sub-blob tag")
    tag = tag_bytes.decode("ascii")
    after_tag = tag_off + tlen
    sc, vc, st, pad = struct.unpack_from(">IIII", buf, after_tag)
    if sc != 3:
        raise ValueError(f"sub-blob stream block count {sc} != 3")
    if st not in VALID_STRIDES:
        raise ValueError(f"sub-blob unsupported stride {st}")
    if not (0 <= vc <= 500_000):
        raise ValueError(f"sub-blob vcount out of range: {vc}")
    voff = after_tag + 16
    post = voff + vc * st
    if post > len(buf):
        raise ValueError("sub-blob vbuf overflows entry")
    return tag, voff, vc, st, post, centroid, bmin, bmax


def _find_subblob_starts(buf: bytes, search_from: int) -> list[int]:
    """Locate all sub-blob start offsets in ``buf[search_from:]``.

    Sub-blobs share their 8-byte prefix with section continuation
    markers; they're distinguished by the third u32 (continuation has
    type=2, sub-blob has the first dword of a centroid float). We require
    the candidate to also pass ``_parse_subblob_header`` to filter the
    rare false positive.
    """
    out: list[int] = []
    pos = search_from
    while pos < len(buf):
        i = buf.find(SUB_BLOB_PREFIX, pos)
        if i < 0:
            break
        if i + 12 > len(buf):
            break
        next_u32 = struct.unpack_from(">I", buf, i + 8)[0]
        if next_u32 != SECTION_CONT_TYPE:
            try:
                _parse_subblob_header(buf, i)
                out.append(i)
            except ValueError:
                pass
        pos = i + 8
    return out


def _decode_positions(buf: bytes, voff: int, vcount: int, stride: int) -> np.ndarray:
    if vcount == 0:
        return np.zeros((0, 3), dtype=np.float32)
    view = np.frombuffer(buf, dtype=np.uint8, count=vcount * stride, offset=voff)
    records = view.reshape(vcount, stride)
    positions = np.empty((vcount, 3), dtype=np.float32)
    for i in range(3):
        positions[:, i] = np.frombuffer(
            records[:, i * 4:i * 4 + 4].tobytes(), dtype=">f4",
        )
    return positions


# Vertex layout per stride, derived from .fxobj VertexDeclaration scan
# (see probes/probe_fxobj_vdecl.py + probe_uv_validate.py, 2026-05-03):
#
#   stride 16: pos(12) + UV0(4)                               UV0 @ +12
#   stride 20: pos(12) + normal_dec4n(4) + UV0(4)             UV0 @ +16
#   stride 24+: pos(12) + normal(4) + UV0(4) + ...            UV0 @ +16
#
# UVs are stored as USHORT2N (2x u16 BE normalized to /65535).
def _decode_uv0(buf: bytes, voff: int, vcount: int, stride: int) -> np.ndarray:
    if vcount == 0 or stride not in VALID_STRIDES:
        return np.zeros((0, 2), dtype=np.float32)
    uv_off = 12 if stride == 16 else 16
    if uv_off + 4 > stride:
        return np.zeros((0, 2), dtype=np.float32)
    records = np.frombuffer(
        buf, dtype=np.uint8, count=vcount * stride, offset=voff,
    ).reshape(vcount, stride)
    raw = records[:, uv_off : uv_off + 4]
    uv16 = np.ascontiguousarray(raw).view(">u2").reshape(vcount, 2)
    return uv16.astype(np.float32) / 65535.0


# -- section parse ------------------------------------------------------------

def _scan_index_streams(buf: bytes, post: int, end: Optional[int] = None) -> list[tuple[int, int]]:
    """Find every plausible (header_offset, index_count) in [post, end).

    Anchor: 16-byte header `[0x00000004, 0x00000000, count, 0x00000002]`,
    followed by ``count`` u16 values that do not overrun the buffer.

    If ``end`` is None, scans to ``len(buf)``. Pass an explicit ``end`` to
    constrain the scan to a single blob's section region (so sub-blob
    sections aren't attributed to the primary blob).
    """
    if end is None:
        end = len(buf)
    out: list[tuple[int, int]] = []
    pos = post
    while pos + 16 <= end:
        i = buf.find(INDEX_STREAM_MARKER, pos, end)
        if i < 0:
            break
        if i + 16 > end:
            break
        count, esz = struct.unpack_from(">II", buf, i + 8)
        if esz == 2 and 0 < count <= 200_000 and i + 16 + count * 2 <= end:
            out.append((i, count))
            pos = i + 16 + count * 2
        else:
            pos = i + 4
    return out


def _decode_section_meta(buf: bytes, idx_hdr_off: int):
    """Decode the 60-byte metadata block immediately preceding the index
    stream marker at ``idx_hdr_off``.

    Returns ``(version, section_idx, flag, scale, anchor)`` or ``None`` if
    the layout doesn't validate (version not in {4, 6}, or the 16-byte
    zero region isn't NUL).
    """
    meta_off = idx_hdr_off - SECTION_META_SIZE
    if meta_off < 0:
        return None
    ver, section_idx, flag = struct.unpack_from(">III", buf, meta_off)
    if ver not in (4, 6):
        return None
    if buf[meta_off + 12:meta_off + 28] != b"\x00" * 16:
        return None
    scale = struct.unpack_from(">4f", buf, meta_off + 28)
    anchor = struct.unpack_from(">4f", buf, meta_off + 44)
    return ver, section_idx, flag, scale, anchor


def _decode_section_name(buf: bytes, idx_hdr_off: int) -> Optional[str]:
    """Walk back from the index stream marker at ``idx_hdr_off`` to find
    the section name.

    The 60-byte metadata block sits immediately before ``idx_hdr_off``;
    before that is the streams-type marker ``[1][2]``, the name length,
    the name bytes, and a 4-byte NUL pad. We scan backwards within a
    256-byte window for the streams-type marker (deterministic anchor)
    then read ``[u32 name_len][name_len bytes ASCII]``.
    """
    meta_off = idx_hdr_off - SECTION_META_SIZE
    if meta_off < 16:
        return None
    win_start = max(0, meta_off - 0x100)
    p = buf.rfind(STREAMS_TYPE_STRING, win_start, meta_off)
    if p < 0:
        return None
    name_len_off = p + len(STREAMS_TYPE_STRING)
    name_len = struct.unpack_from(">I", buf, name_len_off)[0]
    if not (1 <= name_len <= 128):
        return None
    name_off = name_len_off + 4
    name_end = name_off + name_len
    if name_end + NAME_PAD_BYTES > meta_off:
        # Pad is short — accept if at least 1 NUL separator. Some entries
        # may use shorter padding; we don't enforce a minimum.
        if name_end > meta_off:
            return None
    name_bytes = buf[name_off:name_end]
    if not all(0x20 <= b < 0x7f for b in name_bytes):
        return None
    return name_bytes.decode("ascii")


def _annotate_nnn_sibling(name: str) -> tuple[bool, Optional[int]]:
    """Detect whether ``name`` ends in the `_NNN` instance suffix.

    Returns ``(is_sibling, nnn_index)``. The match accepts an optional
    `_LODxx` qualifier between the asset name and the digit group, so
    ``Gondola_LOD00_001`` and ``MOUN_DAM_BLDG_MainDam_003`` both count.
    Generic material names like ``Material__187`` match the regex but
    we exclude them — they're per-section material handles, not
    placement instances.
    """
    if name.startswith("Material_") or name.startswith("material_"):
        return False, None
    m = NNN_SUFFIX_RE.search(name)
    if not m:
        return False, None
    return True, int(m.group(1))


def _strip_to_triangles(idx: np.ndarray) -> np.ndarray:
    """Convert a triangle-strip with 0xFFFF restart into (M, 3) uint32.

    Standard winding alternation: at even strip-position we emit
    ``(a, b, c)``, at odd we emit ``(a, c, b)`` to keep triangles
    consistently oriented. Degenerate triangles (any two indices equal)
    are dropped.

    Implemented via a single numpy pass: stack consecutive triples
    ``[i-2, i-1, i]`` for every position ``i >= 2``, mark restart
    boundaries (a window that overlaps a 0xFFFF index) as invalid, swap
    the middle two columns at odd parity, and filter degenerates with a
    boolean mask. Two orders of magnitude faster than the equivalent
    Python loop on the 24k+ multi-section world-placed pool.
    """
    n = idx.shape[0]
    if n < 3:
        return np.zeros((0, 3), dtype=np.uint32)
    a32 = idx.astype(np.uint32, copy=False)
    # Three rolling columns of consecutive triples (rows = strip positions
    # 2..n-1). Length = n-2.
    a = a32[:-2]
    b = a32[1:-1]
    c = a32[2:]

    # Per-row strip parity. We need the 0-based offset of each row from
    # the most recent strip start. Compute strip-start positions (after
    # a 0xFFFF restart, OR at index 0) and run a forward fill so each
    # row knows its strip-relative position.
    positions = np.arange(n, dtype=np.int64)
    is_restart = (idx == 0xFFFF)
    # Strip starts the index AFTER each restart, plus index 0.
    strip_start = np.where(is_restart, positions + 1, 0)
    np.maximum.accumulate(strip_start, out=strip_start)
    rel = positions - strip_start  # 0-based offset within current strip

    # A valid triangle window starts when rel[i] >= 2 AND none of
    # idx[i-2:i+1] is 0xFFFF.
    rel_window = rel[2:]
    no_restart = ~(is_restart[:-2] | is_restart[1:-1] | is_restart[2:])
    valid = (rel_window >= 2) & no_restart

    # Drop degenerates (any two of the three indices equal).
    valid &= (a != b) & (b != c) & (a != c)

    if not valid.any():
        return np.zeros((0, 3), dtype=np.uint32)

    a = a[valid]
    b = b[valid]
    c = c[valid]
    parity_odd = (rel_window[valid] & 1).astype(bool)

    tris = np.empty((a.shape[0], 3), dtype=np.uint32)
    tris[:, 0] = a
    # Even parity → (a, b, c); odd parity → (a, c, b).
    tris[:, 1] = np.where(parity_odd, c, b)
    tris[:, 2] = np.where(parity_odd, b, c)
    return tris


def _parse_sections(buf: bytes, post: int, vcount: int,
                    end: Optional[int] = None) -> tuple[list[RmbSection], int]:
    """Parse all index streams in ``buf[post:end)`` whose indices fit
    ``vcount`` (i.e. that reference *this* blob's vertex buffer).

    Returns ``(sections, last_idx_buf_end)`` so callers know where the
    trailer (or next sub-blob) begins.
    """
    streams = _scan_index_streams(buf, post, end)
    sections: list[RmbSection] = []
    last_idx_buf_end = post
    for header_off, count in streams:
        ibuf_off = header_off + 16
        ibuf_end = ibuf_off + count * 2
        idx_view = np.frombuffer(buf[ibuf_off:ibuf_end], dtype=">u2")
        valid_mask = idx_view != 0xFFFF
        if not valid_mask.any():
            continue
        # Sections whose indices exceed this blob's vbuf belong to a
        # different blob (a sub-blob with its own vbuf); skip them here.
        if int(idx_view[valid_mask].max()) >= vcount:
            continue
        idx = np.ascontiguousarray(idx_view, dtype=np.uint16)
        tris = _strip_to_triangles(idx)
        name = _decode_section_name(buf, header_off) or ""
        meta = _decode_section_meta(buf, header_off)
        if meta is not None:
            ver, section_idx, flag, scale, anchor = meta
        else:
            ver, section_idx, flag = 6, len(sections), 1
            scale, anchor = (1.0, 1.0, 1.0, 1.0), (0.0, 0.0, 0.0, 0.0)
        is_sib, nnn_idx = _annotate_nnn_sibling(name)
        sections.append(RmbSection(
            name=name, indices=idx, triangles=tris,
            section_idx=section_idx, version=ver, flag=flag,
            scale=scale, anchor=anchor,
            is_nnn_sibling=is_sib, nnn_index=nnn_idx,
        ))
        last_idx_buf_end = max(last_idx_buf_end, ibuf_end)
    return sections, last_idx_buf_end


def _extract_shader_paths(buf: bytes, start: int, end: int) -> list[str]:
    """Best-effort scan of the trailer for length-prefixed `shaders\\…`
    paths.

    Each shader path is stored as ``[u32 BE length][length bytes ASCII]``.
    We anchor on the literal `shaders\\` prefix (8 bytes) and read the
    u32 BE length 4 bytes earlier; any candidate whose length matches a
    plausible path of printable ASCII is captured.
    """
    paths: list[str] = []
    pos = start
    while pos < end:
        i = buf.find(SHADER_PATH_PREFIX, pos, end)
        if i < 0:
            break
        if i < start + 4:
            pos = i + 1
            continue
        length = struct.unpack_from(">I", buf, i - 4)[0]
        if not (8 <= length <= 256):
            pos = i + 1
            continue
        path_end = i + length
        if path_end > end:
            pos = i + 1
            continue
        path_bytes = buf[i:path_end]
        if not all(0x20 <= b < 0x7f for b in path_bytes):
            pos = i + 1
            continue
        paths.append(path_bytes.decode("ascii"))
        pos = path_end
    return paths


def _parse_material_set(buf: bytes, trailer_start: int, trailer_end: int,
                        n_sections: int,
                        ) -> tuple[list[RmbMaterial], int]:
    """Parse the MaterialSet at the start of the trailer.

    Grammar (FM3-derived, validated on FH1 2026-05-03):

        u32 trailer_version          (5 or 6)
        u32 ?                        (always 1)
        u32 ?                        (always 1)
        u32 ?                        (always 1)
        u32 ?                        (always 1)
        u32 materials_length         (== n_sections)
        per material:
          u32 material_version       (3)
          u32 fx_filename_index
          u32 technique_index        (0)
          u32 vsc_version            (1)
          u32 vsc_length
          [16 * vsc_length] bytes    (4 floats per entry; we skip)
          u32 psc_version            (1)
          u32 psc_length
          [16 * psc_length] bytes    (psc_length * vec4)
          u32 tsi_version            (1)
          u32 tsi_length
          [4 * tsi_length] s32       (texture_sampler_indices)

    Returns ``(materials, end_offset)``. Returns ``([], trailer_start)``
    if the structure doesn't validate (caller falls back to bytes-only
    trailer).
    """
    if trailer_end - trailer_start < 0x18:
        return [], trailer_start
    try:
        materials_length = struct.unpack_from(
            ">I", buf, trailer_start + 0x14,
        )[0]
        if materials_length != n_sections or not (1 <= materials_length <= 64):
            return [], trailer_start
        pos = trailer_start + 0x18
        materials: list[RmbMaterial] = []
        for _ in range(materials_length):
            if pos + 36 > trailer_end:
                return [], trailer_start
            # skip material_version (1 u32)
            pos += 4
            fx_filename_index = struct.unpack_from(">I", buf, pos)[0]
            pos += 4
            # skip technique_index
            pos += 4
            # VSC: skip ver, read length, skip 16*length
            pos += 4
            vsc_length = struct.unpack_from(">I", buf, pos)[0]
            pos += 4
            if vsc_length > 256 or pos + 16 * vsc_length > trailer_end:
                return [], trailer_start
            pos += 16 * vsc_length
            # PSC: skip ver, read length, read 16*length bytes (4*length f32)
            if pos + 8 > trailer_end:
                return [], trailer_start
            pos += 4
            psc_length = struct.unpack_from(">I", buf, pos)[0]
            pos += 4
            if psc_length > 256 or pos + 16 * psc_length > trailer_end:
                return [], trailer_start
            psc = np.frombuffer(
                buf, dtype=">f4", count=4 * psc_length, offset=pos,
            ).reshape(-1, 4).astype(np.float32, copy=True)
            pos += 16 * psc_length
            # TSI: skip ver, read length, read 4*length s32
            if pos + 8 > trailer_end:
                return [], trailer_start
            pos += 4
            tsi_length = struct.unpack_from(">I", buf, pos)[0]
            pos += 4
            if tsi_length > 256 or pos + 4 * tsi_length > trailer_end:
                return [], trailer_start
            tsi = np.frombuffer(
                buf, dtype=">i4", count=tsi_length, offset=pos,
            ).astype(np.int32, copy=True)
            pos += 4 * tsi_length
            materials.append(RmbMaterial(
                fx_filename_index=fx_filename_index,
                pixel_shader_constants=psc,
                texture_sampler_indices=tsi,
            ))
        return materials, pos
    except (struct.error, ValueError):
        return [], trailer_start


def _parse_trailer(buf: bytes, trailer_start: int, trailer_end: int,
                   *, n_sections: int = 0,
                   ) -> Optional[RmbTrailer]:
    """Capture the bytes between the last index buffer and the next
    sub-blob (or EOF), and surface any shader paths embedded in it.

    The trailer's full grammar (per-section material/binding records,
    Xbox 360 GPU register binding bytecode, shader paths) is only
    partly understood; this function captures what's reliably
    decodable plus the raw bytes for downstream re-parsing.
    """
    if trailer_end <= trailer_start:
        return None
    raw = bytes(buf[trailer_start:trailer_end])
    paths = _extract_shader_paths(buf, trailer_start, trailer_end)
    materials, _ = _parse_material_set(
        buf, trailer_start, trailer_end, n_sections,
    )
    return RmbTrailer(
        raw=raw, offset=trailer_start, shader_paths=paths, materials=materials,
    )


def _attribute_materials(sections: list[RmbSection],
                         materials: list[RmbMaterial],
                         shader_paths: list[str]) -> None:
    """Attach materials to sections in section_idx order.

    Material 0 binds to section 0, material 1 to section 1, etc.
    Also sets ``section.shader_path`` from the material's
    ``fx_filename_index`` so the shader path is consistent with the
    binding (replaces the older 1:1 path-list attribution which
    silently dropped paths when N_paths != N_sections).
    """
    if not materials or len(materials) != len(sections):
        return
    by_idx = sorted(sections, key=lambda s: s.section_idx)
    for s, m in zip(by_idx, materials):
        s.material = m
        if 0 <= m.fx_filename_index < len(shader_paths):
            s.shader_path = shader_paths[m.fx_filename_index]


def _attribute_shader_paths(sections: list[RmbSection], paths: list[str]) -> None:
    """Attribute trailer shader paths to sections by section_idx.

    The shader path table appears once per section block in
    ``section_idx`` order. When the path count equals the section count
    we attribute 1:1; otherwise we leave ``shader_path`` as ``None``
    rather than guess.

    Used as a fallback when the material set didn't parse — when
    materials parse, ``_attribute_materials`` sets shader_path via
    each material's ``fx_filename_index`` and is more accurate.
    """
    if not paths or len(paths) != len(sections):
        return
    by_idx = sorted(sections, key=lambda s: s.section_idx)
    for s, p in zip(by_idx, paths):
        s.shader_path = p


# -- public API ---------------------------------------------------------------

def parse_blob(buf: bytes) -> Optional[RmbBlob]:
    """Parse one rmb.bin entry: primary blob + any sub-blobs.

    Returns ``None`` if the primary header doesn't validate. Sub-blobs
    that fail to parse are skipped silently (the primary is still
    returned).
    """
    try:
        tag, voff, vc, st, post, centroid, bmin, bmax = _parse_header(buf)
    except Exception:
        return None

    # Locate sub-blob starts so we can partition the entry into
    # [primary section region] + [sub-blob 0 section region] + ...
    # The first sub-blob start (if any) is the upper bound for primary
    # sections.
    sub_starts = _find_subblob_starts(buf, post)
    primary_end = sub_starts[0] if sub_starts else len(buf)

    positions = _decode_positions(buf, voff, vc, st)
    uvs = _decode_uv0(buf, voff, vc, st)
    sections, primary_idx_end = _parse_sections(buf, post, vc, end=primary_end)
    primary_trailer = _parse_trailer(
        buf, primary_idx_end, primary_end, n_sections=len(sections),
    )
    if primary_trailer is not None:
        if primary_trailer.materials:
            _attribute_materials(
                sections, primary_trailer.materials, primary_trailer.shader_paths,
            )
        else:
            _attribute_shader_paths(sections, primary_trailer.shader_paths)
    lod_match = LOD_RE.search(tag)
    blob = RmbBlob(
        tag=tag,
        lod=lod_match.group(1) if lod_match else "",
        centroid=centroid,
        bbox_min=bmin,
        bbox_max=bmax,
        vcount=vc,
        stride=st,
        voff=voff,
        positions=positions,
        uvs=uvs,
        sections=sections,
        trailer=primary_trailer,
    )

    # Walk sub-blobs sequentially. Each sub-blob's section region runs
    # from its post (= vbuf end) to the next sub-blob start (or EOF).
    for i, sub_off in enumerate(sub_starts):
        try:
            (s_tag, s_voff, s_vc, s_st, s_post,
             s_cen, s_bmin, s_bmax) = _parse_subblob_header(buf, sub_off)
        except ValueError:
            continue
        s_end = sub_starts[i + 1] if i + 1 < len(sub_starts) else len(buf)
        s_positions = _decode_positions(buf, s_voff, s_vc, s_st)
        s_uvs = _decode_uv0(buf, s_voff, s_vc, s_st)
        s_sections, s_idx_end = _parse_sections(buf, s_post, s_vc, end=s_end)
        s_trailer = _parse_trailer(
            buf, s_idx_end, s_end, n_sections=len(s_sections),
        )
        if s_trailer is not None:
            if s_trailer.materials:
                _attribute_materials(
                    s_sections, s_trailer.materials, s_trailer.shader_paths,
                )
            else:
                _attribute_shader_paths(s_sections, s_trailer.shader_paths)
        s_lod = LOD_RE.search(s_tag)
        blob.sub_blobs.append(RmbSubBlob(
            tag=s_tag,
            lod=s_lod.group(1) if s_lod else "",
            centroid=s_cen,
            bbox_min=s_bmin,
            bbox_max=s_bmax,
            vcount=s_vc,
            stride=s_st,
            voff=s_voff,
            positions=s_positions,
            uvs=s_uvs,
            sections=s_sections,
            trailer=s_trailer,
        ))
    return blob


def iter_blobs(
    zip_path: Path,
    *,
    tag_prefix: Optional[str | tuple[str, ...]] = None,
    lod: Optional[str] = None,
) -> Iterator[tuple[Entry, bytes, RmbBlob]]:
    """Yield (entry, raw bytes, blob) for every rmb.bin entry whose primary
    blob parses; optionally filter by tag prefix and/or LOD digits.

    ``tag_prefix`` accepts either a single prefix string or a tuple of
    prefixes (matched via ``str.startswith`` semantics — any prefix in the
    tuple matches).

    Iteration order follows ``list_entries`` (= bin.zip central directory
    order); the index of an entry within this iteration is its global
    rmb-handle as referenced by v42k7. Failed parses are skipped silently.
    """
    for e in list_entries(zip_path):
        if "rmb.bin" not in e.filename:
            continue
        try:
            d = read_entry(zip_path, e)
        except Exception:
            continue
        blob = parse_blob(d)
        if blob is None:
            continue
        if tag_prefix and not blob.tag.startswith(tag_prefix):
            continue
        if lod is not None and blob.lod != lod:
            continue
        blob.entry = e
        yield e, d, blob


def list_rmb_entries(zip_path: Path) -> list[Entry]:
    """Return the flat list of rmb.bin entries in bin.zip order.

    The index of an entry in this list is its v42k7 ``rmb-handle``: a
    u32 BE handle ``H`` in a v42k7 prolog table resolves to
    ``list_rmb_entries(zip)[H]``. Iterates in the same order as
    ``list_entries`` (= central directory order).
    """
    return [e for e in list_entries(zip_path) if "rmb.bin" in e.filename]


# -- backward-compat shims for terrain_hi.py ---------------------------------

@dataclass
class TerrBlob:
    """Legacy lightweight TERR descriptor used by ``terrain_hi.py``."""
    entry: Entry
    tag: str
    lod: str
    vcount: int
    stride: int
    voff: int


# Terrain-area tag prefixes that ship world-baked vertex positions in
# the rmb pool. Confirmed via probe `probes/probe_missing_terrain.py`
# (2026-05-03) — the conservative add list excludes families with mixed
# terrain/building content (`MT_`, `Maintown_`, `MAINTOWN_`, `Redstone_`,
# `Rocks_`) until a sub-prefix filter can disambiguate them. Adding any
# of these brings ~13k more terrain tiles / ~13M vertices into the
# Blender export — see `docs/world-architecture.md §2.3`.
TERRAIN_TAG_PREFIXES: tuple[str, ...] = (
    "TERR_",
    "Plains_",
    "Mountains_",
    "Foothills_",
    "RedRock_",
    "Reservoir_",
    "Blends_",
    "CLRD_",
    "ROAD_",
    "road_",
)


def iter_terr_blobs(
    zip_path: Path,
    *,
    lod: str = "00",
    prefixes: tuple[str, ...] = TERRAIN_TAG_PREFIXES,
) -> Iterator[tuple[Entry, bytes, TerrBlob]]:
    """Iterator over terrain rmb blobs.

    Defaults to the full terrain-area prefix list (`TERRAIN_TAG_PREFIXES`)
    so terrain_hi catches Plains/Mountains/Foothills/RedRock/Reservoir
    tiles — historically the filter was `TERR_*` only and ~13k terrain
    tiles silently dropped. Pass a narrower tuple to restrict.

    ``lod=""`` returns every LOD (including blobs whose tag contains no
    LOD token).
    """
    for entry, data, blob in iter_blobs(
        zip_path, tag_prefix=prefixes, lod=(lod if lod else None),
    ):
        yield entry, data, TerrBlob(
            entry=entry, tag=blob.tag, lod=blob.lod,
            vcount=blob.vcount, stride=blob.stride, voff=blob.voff,
        )


def decode_positions(buf: bytes, blob: TerrBlob) -> np.ndarray:
    """Decode positions for a legacy ``TerrBlob`` descriptor."""
    return _decode_positions(buf, blob.voff, blob.vcount, blob.stride)
