"""Parser for ``coloradoout.NNNNN.rmb.bin`` — the shared geometry pool.

Each rmb.bin entry holds **one primary blob followed by zero or more
sub-blobs**. The primary blob's header begins at offset 0 of the entry
and is the canonical one (full transform/centroid metadata). Sub-blobs
share the same section grammar but have a stripped-down header and live
sequentially after the primary's section block. ``parse_blob`` returns
the primary blob with its sub-blobs attached as ``RmbBlob.sub_blobs``.

Primary blob layout (all u32 BE unless noted, offsets from blob start):

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

Section grammar (post-vbuf):

    section_block := u32 1; u32 N_sections; section{N_sections}
    section       := [u32 5 if not first];          # continuation marker
                     u32 1; u32 2;                   # streams=1, type=2 (string)
                     u32 name_len; bytes name;
                     <transform/bbox metadata, ~0x4c bytes>
                     u32 4; u32 0;                   # stream type=4 (indices)
                     u32 index_count; u32 elem_size=2;
                     u16 BE × index_count            # triangle-strip indices

The metadata block is a fixed pattern (zeros, version=6, four 1.0 floats,
four f32 bbox-ish floats) but its exact shape is not material here — the
parser anchors on the index-stream marker `00 00 00 04 00 00 00 00` to
locate the index buffer for each section. Indices are u16 BE with
``0xFFFF`` restart, and we triangulate into a face list with the standard
even/odd orientation alternation.
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


@dataclass
class RmbSection:
    name: str
    indices: np.ndarray   # (K,) uint16, 0xFFFF = strip restart
    triangles: np.ndarray  # (M, 3) uint32 (triangulated from strips)


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
    sections: list[RmbSection] = field(default_factory=list)


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
    sections: list[RmbSection] = field(default_factory=list)
    sub_blobs: list[RmbSubBlob] = field(default_factory=list)
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


def _name_at(buf: bytes, header_off: int) -> Optional[str]:
    """Walk back from a `[u32 1][u32 2][u32 N]<ascii>` section header to
    extract the material/section name, if present in the metadata leading
    up to the index stream at ``header_off``.
    """
    # The string preamble is `01 00 00 00 02 00 00 00 LL <ascii*LL>`. We
    # scan backwards within a window for `00 00 00 01 00 00 00 02` and read
    # the length following.
    needle = b"\x00\x00\x00\x01\x00\x00\x00\x02"
    window_start = max(0, header_off - 0x200)
    seg = buf[window_start:header_off]
    p = seg.rfind(needle)
    if p < 0:
        return None
    name_off = window_start + p + len(needle) + 4
    name_len = struct.unpack_from(">I", buf, window_start + p + len(needle))[0]
    if not (1 <= name_len <= 128) or name_off + name_len > header_off:
        return None
    name_bytes = buf[name_off:name_off + name_len]
    if not all(0x20 <= b < 0x7f for b in name_bytes):
        return None
    return name_bytes.decode("ascii")


def _strip_to_triangles(idx: np.ndarray) -> np.ndarray:
    """Convert a triangle-strip with 0xFFFF restart into (M, 3) uint32.

    Standard winding alternation: even strip-position emits (a, b, c),
    odd emits (a, c, b) to keep triangles consistently oriented.
    Degenerate triangles (any two indices equal) are dropped.
    """
    tris: list[tuple[int, int, int]] = []
    a = b = -1
    n = 0  # count of valid verts in current strip
    for v in idx.tolist():
        if v == 0xFFFF:
            n = 0
            a = b = -1
            continue
        if n >= 2:
            c = v
            if a != b and b != c and a != c:
                if (n - 2) % 2 == 0:
                    tris.append((a, b, c))
                else:
                    tris.append((a, c, b))
            a, b = b, c
        elif n == 0:
            a = v
        else:  # n == 1
            b = v
        n += 1
    if not tris:
        return np.zeros((0, 3), dtype=np.uint32)
    return np.asarray(tris, dtype=np.uint32)


def _parse_sections(buf: bytes, post: int, vcount: int,
                    end: Optional[int] = None) -> list[RmbSection]:
    """Parse all index streams in ``buf[post:end]`` whose indices fit
    ``vcount`` (i.e. that reference *this* blob's vertex buffer).
    """
    streams = _scan_index_streams(buf, post, end)
    sections: list[RmbSection] = []
    for header_off, count in streams:
        ibuf_off = header_off + 16
        idx_view = np.frombuffer(
            buf[ibuf_off:ibuf_off + count * 2], dtype=">u2",
        )
        valid_mask = idx_view != 0xFFFF
        if not valid_mask.any():
            continue
        # Sections whose indices exceed this blob's vbuf belong to a
        # different blob (a sub-blob with its own vbuf); skip them here.
        if int(idx_view[valid_mask].max()) >= vcount:
            continue
        idx = np.ascontiguousarray(idx_view, dtype=np.uint16)
        tris = _strip_to_triangles(idx)
        name = _name_at(buf, header_off) or ""
        sections.append(RmbSection(name=name, indices=idx, triangles=tris))
    return sections


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
    sections = _parse_sections(buf, post, vc, end=primary_end)
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
        sections=sections,
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
        s_sections = _parse_sections(buf, s_post, s_vc, end=s_end)
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
            sections=s_sections,
        ))
    return blob


def iter_blobs(
    zip_path: Path,
    *,
    tag_prefix: Optional[str] = None,
    lod: Optional[str] = None,
) -> Iterator[tuple[Entry, bytes, RmbBlob]]:
    """Yield (entry, raw bytes, blob) for every rmb.bin entry whose primary
    blob parses; optionally filter by tag prefix and/or LOD digits.

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


def iter_terr_blobs(
    zip_path: Path, *, lod: str = "00",
) -> Iterator[tuple[Entry, bytes, TerrBlob]]:
    """Backward-compat iterator for the existing terrain_hi pipeline.

    Filters to ``TERR_*`` blobs only. ``lod=""`` returns every LOD
    (including blobs whose tag contains no LOD token).
    """
    for entry, data, blob in iter_blobs(
        zip_path, tag_prefix="TERR_", lod=(lod if lod else None),
    ):
        yield entry, data, TerrBlob(
            entry=entry, tag=blob.tag, lod=blob.lod,
            vcount=blob.vcount, stride=blob.stride, voff=blob.voff,
        )


def decode_positions(buf: bytes, blob: TerrBlob) -> np.ndarray:
    """Decode positions for a legacy ``TerrBlob`` descriptor."""
    return _decode_positions(buf, blob.voff, blob.vcount, blob.stride)
