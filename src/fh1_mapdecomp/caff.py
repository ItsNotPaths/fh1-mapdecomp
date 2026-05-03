"""CAFF (Common Asset File Format) reader for FH1 textures.

Decodes ``_0xHHHHHHHH.bin`` texture containers from ``bin.zip``.
Format spec from Doliman100's 010-Editor template (``CAFF.bt``) at
``forza-x360-io/src/forza_blender/forza/textures/CAFF.bt``; Python
port adapted from ``forza_blender/forza/textures/read_bin.py`` for
FH1 (CAFF version ``21.11.05.0034`` = "old layout", header
endianness byte at offset 76, no compression in retail Colorado).

Top-level API:

    decode_texture(bytes) -> TextureData
        Decode a texture-bearing CAFF blob (a ``_0x*.bin`` entry from
        ``bin.zip``) into raw linear pixel blocks plus metadata.

    write_dds(TextureData, path)
        Wrap the decoded blocks in a DDS container and write to disk.

The decoded pixel data is XG-tiled bytes that need
``deswizzle.XGUntileSurfaceToLinearTexture`` before they become a
useful image. ``decode_texture`` does this for you.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


_MAGIC = b"CAFF"
_VERSION = b"21.11.05.0034\x00\x00\x00"


# D3DFORMAT codes used in the wild (low 6 bits of the format bitfield).
# Mapping to DXGI BC indices for DDS DX10 wrapping.
class D3DFormat:
    L8                  = 671088898   # raw 8-bit luminance
    A8R8G8B8            = 405275014   # 32-bit RGBA
    X8R8G8B8            = 673710470   # 32-bit RGBX
    X8R8G8B8_LIN        = 673742470   # linear 32-bit RGBX
    X8R8G8B8_SRGB       = 673742726
    DXT1                = 438305106
    DXT1_SRGB           = 438337362
    DXT3_SRGB           = 438337363
    DXT5                = 438305108
    DXT5_SRGB           = 438337364
    DXT5A               = 438305147
    DXT5A_SRGB          = 438337403
    DXN                 = 438305137   # BC5 — typical normal map storage


# Block-format properties: (block_pixel_size, bytes_per_block).
# Indexed by the GPU format value (bits 0..5 of the format bitfield).
_GPU_FORMAT_BLOCKS = {
    2:  (1, 1),    # 8 (single channel)
    6:  (1, 4),    # 8_8_8_8 (RGBA)
    18: (4, 8),    # DXT1 (BC1)
    19: (4, 16),   # DXT3 (BC2)
    20: (4, 16),   # DXT5 (BC3)
    49: (4, 16),   # DXN (BC5)
    59: (4, 8),    # DXT5A (BC4)
}


@dataclass
class _AllocationBlock:
    name: str
    uncompressed_size: int
    address: int = -1   # filled in after parsing the header


@dataclass
class _SectionInfo:
    asset_index: int
    asset_offset: int
    asset_size: int
    allocation_block_index: int  # 1-based; -1 means 0


@dataclass
class CaffHeader:
    assets_length: int
    sections_length: int
    data_block_offset: int       # data_allocation_block_size from the header
    header_size: int
    allocation_blocks: list[_AllocationBlock]


@dataclass
class CaffAsset:
    name: str
    offset: int                  # absolute offset in the buffer
    size: int


@dataclass
class TextureData:
    """Decoded texture from a CAFF blob.

    `pixels` is a linear (non-tiled) byte stream of compressed blocks
    in the format named by `format` (e.g. 16 bytes per BC3 block).
    Use `write_dds` to wrap as a DDS file.
    """
    width: int
    height: int
    format: int                  # D3DFORMAT code (with endian/tile bits)
    levels: int
    pixels: bytes                # linear (untiled) block bytes


def _read_caff_header(buf: bytes) -> CaffHeader:
    if len(buf) < 0x190:
        raise ValueError("buffer too short for CAFF header")
    if buf[:4] != _MAGIC:
        raise ValueError(f"not a CAFF blob (magic={buf[:4]!r})")
    if buf[4:20] != _VERSION:
        # Other versions are decodable too but we don't ship support
        # for them. FH1 retail is uniformly 21.11.05.0034.
        raise ValueError(
            f"unsupported CAFF version {buf[4:20]!r} (expected {_VERSION!r})"
        )
    # offsets per FM3 reference + CAFF.bt for the "old layout"
    assets_length = struct.unpack_from(">I", buf, 0x18)[0]
    sections_length = struct.unpack_from(">I", buf, 0x1C)[0]
    # skip HeaderUnknown1 unk_1 + unk_2 (32 bytes total) + info_unk1_length (4)
    data_block_offset = struct.unpack_from(">I", buf, 0x44)[0]
    header_size = struct.unpack_from(">I", buf, 0x48)[0]
    # 0x4C = endianness byte, 0x4D = allocation_blocks_length, 0x4E = compression
    allocation_blocks_length = buf[0x4D]
    compression = buf[0x4E]
    if compression != 0:
        raise ValueError(
            f"compressed CAFF (compression={compression}) not supported"
        )
    blocks: list[_AllocationBlock] = []
    pos = 0x50
    for _ in range(allocation_blocks_length):
        if pos + 40 > len(buf):
            raise ValueError("AllocationBlockInfo overruns buffer")
        # name = 11 bytes ASCII, NUL-padded
        name_bytes = buf[pos : pos + 11]
        name = name_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        # skip alignment (1) + base_section_index (4) = 5 bytes after name
        uncompressed_size = struct.unpack_from(">I", buf, pos + 16)[0]
        # skip data_ptr/data_offset/data_overlap/data_size/compressed_size = 20
        blocks.append(_AllocationBlock(
            name=name, uncompressed_size=uncompressed_size,
        ))
        pos += 40
    # Compute allocation block start addresses (after the header)
    addr = header_size
    for b in blocks:
        b.address = addr
        addr += b.uncompressed_size
    return CaffHeader(
        assets_length=assets_length,
        sections_length=sections_length,
        data_block_offset=data_block_offset,
        header_size=header_size,
        allocation_blocks=blocks,
    )


def _find_data_block(header: CaffHeader) -> tuple[int, _AllocationBlock]:
    for i, b in enumerate(header.allocation_blocks):
        if b.name == ".data":
            return i, b
    raise ValueError("no .data allocation block in CAFF")


def _read_table0(buf: bytes, header: CaffHeader,
                 ) -> tuple[list[CaffAsset], list[_SectionInfo]]:
    """Parse the section/asset directory at the start of the .data block."""
    data_idx, data_block = _find_data_block(header)
    pos = data_block.address + header.data_block_offset
    if pos + 8 > len(buf):
        raise ValueError("Table 0 starts past end of buffer")
    # asset names
    assets_names_size = struct.unpack_from(">I", buf, pos)[0]
    pos += 4
    if pos + 4 * header.assets_length > len(buf):
        raise ValueError("asset name offsets overrun buffer")
    asset_name_offsets = list(struct.unpack_from(
        f">{header.assets_length}I", buf, pos,
    ))
    pos += 4 * header.assets_length
    if pos + assets_names_size > len(buf):
        raise ValueError("asset names overrun buffer")
    name_pool = buf[pos : pos + assets_names_size]
    pos += assets_names_size

    asset_names: list[str] = []
    for off in asset_name_offsets:
        end = name_pool.find(b"\x00", off)
        s = name_pool[off:end] if end >= 0 else name_pool[off:]
        asset_names.append(s.decode("ascii", errors="replace"))

    # unknown extra names
    unk_names_size = struct.unpack_from(">I", buf, pos)[0]
    pos += 4 + unk_names_size

    # sections_info: 14 bytes each
    sections: list[_SectionInfo] = []
    for _ in range(header.sections_length):
        if pos + 14 > len(buf):
            raise ValueError("sections_info overruns buffer")
        asset_index = struct.unpack_from(">I", buf, pos)[0]
        asset_offset = struct.unpack_from(">I", buf, pos + 4)[0]
        asset_size = struct.unpack_from(">I", buf, pos + 8)[0]
        allocation_block_index = buf[pos + 12]
        sections.append(_SectionInfo(
            asset_index=asset_index,
            asset_offset=asset_offset,
            asset_size=asset_size,
            allocation_block_index=allocation_block_index,
        ))
        pos += 14

    # Resolve asset offsets in the buffer (for sections in the .data block).
    # An asset's data lives in the allocation block named by its
    # section's allocation_block_index (1-based); the offset within the
    # block is asset_offset.
    assets: dict[int, CaffAsset] = {}
    for sec in sections:
        if sec.allocation_block_index < 1 or sec.allocation_block_index > len(header.allocation_blocks):
            continue
        block = header.allocation_blocks[sec.allocation_block_index - 1]
        if block.name != ".data":
            # Only the .data-block sections are addressable as "assets"
            # in the FM3 reader. Keep this strict for now.
            continue
        if 0 <= sec.asset_index - 1 < len(asset_names):
            name = asset_names[sec.asset_index - 1]
            assets[sec.asset_index - 1] = CaffAsset(
                name=name,
                offset=block.address + sec.asset_offset,
                size=sec.asset_size,
            )
    return list(assets.values()), sections


def _decode_texture_asset(buf: bytes, asset: CaffAsset, header: CaffHeader,
                          ) -> Optional[TextureData]:
    """Decode a single TextureAsset's header and pull pixel data."""
    if asset.size < 0x40:
        return None
    base = asset.offset
    # TextureAsset layout (FM3-derived):
    fmt = struct.unpack_from(">I", buf, base + 0x18)[0]
    tex_type = struct.unpack_from(">I", buf, base + 0x1C)[0]
    width = struct.unpack_from(">H", buf, base + 0x24)[0]
    height = struct.unpack_from(">H", buf, base + 0x26)[0]
    base_offset_ptr = struct.unpack_from(">I", buf, base + 0x28)[0]
    levels = buf[base + 0x30]

    if tex_type != 0:  # only TEX_DIRECT supported for now (skip cube/array)
        return None
    if width == 0 or height == 0:
        return None
    gpu_format = fmt & 0x3F
    if gpu_format not in _GPU_FORMAT_BLOCKS:
        return None

    # base_offset_ptr is an offset within the .gpu allocation block (or
    # actually just an address within the file post-header-fixup).
    # In FM3 reference's reader this gets resolved via TableUnknownB
    # fixups; for the simple texture-only case we can just treat it as
    # an offset into the .gpu block.
    gpu_block = next(
        (b for b in header.allocation_blocks if b.name == ".gpu"),
        None,
    )
    if gpu_block is None:
        return None

    pixel_size = _calc_texture_size(fmt, width, height)
    pixel_offset = gpu_block.address + base_offset_ptr
    if pixel_offset + pixel_size > len(buf):
        return None
    pixels_raw = np.frombuffer(
        buf, dtype=np.uint8, count=pixel_size, offset=pixel_offset,
    )

    # GPU endian flip per format — must happen BEFORE detiling, since
    # block bytes are stored in GPU-local-endian order on disk.
    endian = (fmt >> 6) & 0x3
    if endian == 1:   # 8IN16
        pixels_raw = _flip_byte_order(pixels_raw, 2)
    elif endian == 2:  # 8IN32
        pixels_raw = _flip_byte_order(pixels_raw, 4)

    pixels_linear = _untile_to_linear(pixels_raw, width, height, fmt, levels)

    return TextureData(
        width=width,
        height=height,
        format=fmt,
        levels=levels,
        pixels=pixels_linear.tobytes(),
    )


def _calc_texture_size(fmt: int, width: int, height: int) -> int:
    """XG-tiled storage size in bytes (matches FM3 deswizzle.calc_texture_size)."""
    block_size, texel_pitch = _GPU_FORMAT_BLOCKS[fmt & 0x3F]
    width_in_blocks = (width + block_size - 1) // block_size
    height_in_blocks = (height + block_size - 1) // block_size
    tiled = (fmt & 0x100) >> 8
    if tiled:
        width_alignment = 32
    else:
        width_alignment = max(256 // texel_pitch, 32)
    aligned_width = (width_in_blocks + width_alignment - 1) & ~(width_alignment - 1)
    aligned_height = (height_in_blocks + 31) & ~31
    size = aligned_width * aligned_height * texel_pitch
    return (size + 4095) & ~4095


def _untile_to_linear(data: np.ndarray, width: int, height: int,
                      fmt: int, levels: int) -> np.ndarray:
    """XG untile → linear blocks (FM3 deswizzle.XGUntileSurfaceToLinearTexture)."""
    block_size, texel_pitch = _GPU_FORMAT_BLOCKS[fmt & 0x3F]
    offset_x = 0
    offset_y = 0
    if levels != 1 and (width <= 16 or height <= 16):
        if width <= height:
            offset_x = 16 // block_size
        else:
            offset_y = 16 // block_size
    width_in_blocks = (width + block_size - 1) // block_size
    height_in_blocks = (height + block_size - 1) // block_size
    x, y = np.meshgrid(
        np.arange(offset_x, offset_x + width_in_blocks),
        np.arange(offset_y, offset_y + height_in_blocks),
    )
    tiled = (fmt & 0x100) >> 8
    if tiled:
        src_offset = _xg_address_2d_tiled(x, y, width_in_blocks, texel_pitch)
        data = data.reshape((-1, texel_pitch))
        return data[src_offset]
    else:
        width_alignment = max(256 // texel_pitch, 32)
        aligned_width = (width_in_blocks + width_alignment - 1) & ~(width_alignment - 1)
        # Ensure data is large enough
        needed_rows = (offset_y + height_in_blocks)
        needed = needed_rows * aligned_width * texel_pitch
        if data.size < needed:
            # Pad with zeros so reshape doesn't fail
            data = np.concatenate([data, np.zeros(needed - data.size, dtype=np.uint8)])
        data = data[: needed_rows * aligned_width * texel_pitch].reshape(
            (-1, aligned_width, texel_pitch),
        )
        return data[y, x]


def _xg_address_2d_tiled(x, y, width: int, texel_pitch: int):
    """Return the linear block index for a tiled (x, y) block coord.

    Direct port of Microsoft's XGAddress2DTiledOffset from xgraphics.h.
    """
    aligned_width = (width + 31) & ~31
    log_bpp = (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))
    macro = ((x >> 5) + (y >> 5) * (aligned_width >> 5)) << (log_bpp + 7)
    micro = (((x & 7) + ((y & 6) << 2)) << log_bpp)
    offset = (macro + ((micro & ~15) << 1) + (micro & 15)
              + ((y & 8) << (3 + log_bpp)) + ((y & 1) << 4))
    return (((offset & ~511) << 3) + ((offset & 448) << 2) + (offset & 63)
            + ((y & 16) << 7)
            + (((((y & 8) >> 2) + (x >> 3)) & 3) << 6)) >> log_bpp


def _flip_byte_order(data: np.ndarray, group_size: int) -> np.ndarray:
    if data.size % group_size != 0:
        # Trim remainder to keep reshape clean.
        n_keep = (data.size // group_size) * group_size
        data = data[:n_keep]
    if group_size == 2:
        return data.view(np.uint16).byteswap().view(np.uint8)
    elif group_size == 4:
        return data.view(np.uint32).byteswap().view(np.uint8)
    raise ValueError(f"unsupported group size {group_size}")


# -- DDS wrapping -------------------------------------------------------------

# DXGI format codes (D3D11) used in the DX10 DDS header
class DXGI:
    R8_UNORM         = 61
    BC1_UNORM        = 71
    BC2_UNORM        = 74
    BC3_UNORM        = 77
    BC4_UNORM        = 80
    BC5_UNORM        = 83
    B8G8R8A8_UNORM   = 87
    B8G8R8X8_UNORM   = 88


_FORMAT_TO_DXGI = {
    D3DFormat.L8:           DXGI.R8_UNORM,
    D3DFormat.A8R8G8B8:     DXGI.B8G8R8A8_UNORM,
    D3DFormat.X8R8G8B8:     DXGI.B8G8R8X8_UNORM,
    D3DFormat.X8R8G8B8_LIN: DXGI.B8G8R8X8_UNORM,
    D3DFormat.X8R8G8B8_SRGB: DXGI.B8G8R8X8_UNORM,
    D3DFormat.DXT1:         DXGI.BC1_UNORM,
    D3DFormat.DXT1_SRGB:    DXGI.BC1_UNORM,
    D3DFormat.DXT3_SRGB:    DXGI.BC2_UNORM,
    D3DFormat.DXT5:         DXGI.BC3_UNORM,
    D3DFormat.DXT5_SRGB:    DXGI.BC3_UNORM,
    D3DFormat.DXT5A:        DXGI.BC4_UNORM,
    D3DFormat.DXT5A_SRGB:   DXGI.BC4_UNORM,
    D3DFormat.DXN:          DXGI.BC5_UNORM,
}


def _dds_dx10(blocks: bytes, width: int, height: int, dxgi_fmt: int) -> bytes:
    """Wrap pre-detiled blocks in a DDS DX10 container."""
    DDS_MAGIC = b"DDS "
    DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH = 0x1, 0x2, 0x4
    DDSD_PIXELFORMAT, DDSD_LINEARSIZE = 0x1000, 0x80000
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    header = struct.pack(
        "<I I I I I I I 11I",
        124, flags, height, width, len(blocks), 0, 1, *([0] * 11),
    )
    pixelfmt = struct.pack("<I I 4s I I I I I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
    caps = struct.pack("<I I I I", 0x1000, 0, 0, 0)  # DDSCAPS_TEXTURE
    reserved2 = b"\x00\x00\x00\x00"
    # DDS_HEADER_DXT10: format, dimension=TEXTURE2D(3), miscFlag=0, arraySize=1, miscFlag2=0
    dxt10 = struct.pack("<I I I I I", dxgi_fmt, 3, 0, 1, 0)
    return DDS_MAGIC + header + pixelfmt + caps + reserved2 + dxt10 + blocks


def to_dds(tex: TextureData) -> Optional[bytes]:
    """Wrap a decoded texture as a DDS DX10 file. None if format unsupported."""
    fmt_no_meta = tex.format
    # Strip the bits that aren't part of the D3DFORMAT identity (keep low 6
    # bits + sign bits for D3DFORMAT lookup, but our lookup table uses the
    # full encoded value as in FM3). Try direct lookup first.
    dxgi = _FORMAT_TO_DXGI.get(fmt_no_meta)
    if dxgi is None:
        return None
    return _dds_dx10(tex.pixels, tex.width, tex.height, dxgi)


# -- top-level entrypoints ---------------------------------------------------

def decode_caff(buf: bytes) -> tuple[CaffHeader, list[CaffAsset]]:
    """Parse a CAFF blob's header and asset directory."""
    header = _read_caff_header(buf)
    assets, _sections = _read_table0(buf, header)
    return header, assets


def decode_texture(buf: bytes) -> Optional[TextureData]:
    """Decode the first texture asset in a CAFF blob.

    Returns ``None`` if the blob isn't a texture or the texture format
    isn't supported.
    """
    header, assets = decode_caff(buf)
    for asset in assets:
        if not asset.name.endswith(".bin"):
            continue
        td = _decode_texture_asset(buf, asset, header)
        if td is not None:
            return td
    return None


def write_dds(tex: TextureData, path) -> bool:
    """Write a decoded texture to ``path`` as DDS. Returns False if format unsupported."""
    dds = to_dds(tex)
    if dds is None:
        return False
    from pathlib import Path
    Path(path).write_bytes(dds)
    return True
