"""Parser for FH1 PVS + per-zone PVSZ placement files.

The master ``Ribbon_NN/<Track>_NN.pvs`` (magic ``FPVS``, version 50 for
FH1 retail) carries the full per-ribbon placement description: a
``PVSModel[]`` table (each entry = textures + shaders the model uses)
and a ``PVSModelInstance[]`` table (placement slots: ``model_index``,
flags, texture-ref). For FH the slot's transform is left empty in the
master and streamed in via per-zone ``__R{ribbon:02d}Z{i:05d}.pvsz``
files (3,502 in Colorado's bin.zip across all ribbons; 1,434 for
ribbon_00 alone).

Each PVSZ ends with a ``model_instance_details[]`` array; the file
opens with a ``model_instance_indexes[]`` array. The two are zipped
position-wise to fill in ``PVSModelInstance.transform[index]``.
``ModelInstanceDetails`` carries:
  * 3× f32 BE world translation
  * 3×3 f16 BE rotation
  * f32 material data + a small variable tail

Mesh resolution is by filename: ``{prefix}.{model_index:05d}.rmb.bin``.

This implementation mirrors the Doliman100 / austinbaccus FH1 fork
(``read_pvs.py`` from ``Forza-X360-IO GPLv3``), trimmed to the
game_series=2 (Horizon) branch and verified against retail FH1 v50
(Colorado: 62,173 models_instances, 14,560 models, 1,434 streamed
zones).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


_MAGIC_BE = b"FPVS"
_MAGIC_LE = b"SVPF"


class PvsError(RuntimeError):
    pass


# -- minimal big-endian binary stream ----------------------------------------

class _Stream:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def tell(self) -> int:
        return self.pos

    def skip(self, n: int) -> None:
        self.pos += n

    def remaining(self) -> int:
        return len(self.buf) - self.pos

    def read_u8(self) -> int:
        v = self.buf[self.pos]; self.pos += 1; return v

    def read_u16(self) -> int:
        v, = struct.unpack_from(">H", self.buf, self.pos); self.pos += 2; return v

    def read_s16(self) -> int:
        v, = struct.unpack_from(">h", self.buf, self.pos); self.pos += 2; return v

    def read_u32(self) -> int:
        v, = struct.unpack_from(">I", self.buf, self.pos); self.pos += 4; return v

    def read_f16(self) -> float:
        v, = struct.unpack_from(">e", self.buf, self.pos); self.pos += 2; return float(v)

    def read_f32(self) -> float:
        v, = struct.unpack_from(">f", self.buf, self.pos); self.pos += 4; return float(v)

    def read_string(self) -> str:
        n = self.read_u32()
        s = self.buf[self.pos:self.pos + n].decode("ascii", errors="replace")
        self.pos += n
        return s


# -- typed records ------------------------------------------------------------

@dataclass
class PvsHeader:
    version: int
    cpu_data_size: int


@dataclass
class PvsTexture:
    """Master-PVS texture entry. Only the first 24 bytes are decoded; the
    addon discards the trailing 4 bytes (28 total). UV scale/translate
    are atlas remap params; ``texture_file_name`` is the u32 hash that
    matches a ``_0xHHHHHHHH.bin`` (or .stx index when high bit set)."""
    texture_file_name: int
    index_in_stx_bin: int
    u_scale: float
    v_scale: float
    u_translate: float
    v_translate: float


@dataclass
class PvsModel:
    """A model = handle to an ``.rmb.bin`` (via ``model_index`` →
    ``{prefix}.{NNNNN}.rmb.bin``) plus the texture refs and shader refs
    the engine binds when drawing it."""
    model_index: int
    textures: list[int] = field(default_factory=list)
    shaders: list[int] = field(default_factory=list)


@dataclass
class PvsModelInstance:
    """One placement slot. ``transform`` and ``material_data`` start as
    ``None`` (placeholder is the addon's identity-ish 5000-y matrix);
    they're filled in by ``hydrate_transforms`` from per-zone PVSZ data.
    Slots that no PVSZ ever references are unplaced."""
    model_index: int
    parent_lod_offset: Optional[int]
    flags: int
    texture: int
    transform: Optional[list[list[float]]] = None
    material_data: Optional[float] = None


@dataclass
class Pvs:
    header: PvsHeader
    ribbon_index: int
    streamed_zones_length: int
    textures: list[PvsTexture]
    shader_paths: list[str]
    models: list[PvsModel]
    models_instances: list[PvsModelInstance]
    lone_models_instances: list[PvsModelInstance]
    prefix: str


@dataclass
class PvszZone:
    indexes: list[int]
    details: list[tuple[list[list[float]], float]]   # (4x4 transform, material_data)


# -- shared body reader (used for master-PVS zones AND per-zone PVSZ files) --

def _read_zone_body(s: _Stream, version: int, tools_based_zone_unioning: int) -> PvszZone:
    """Read one ``PVSZone`` record. Same shape in both contexts; in master
    PVS the details list is always empty (transforms are streamed)."""
    n_idx = s.read_u32()
    indexes = [s.read_u32() for _ in range(n_idx)]
    if tools_based_zone_unioning:
        n = s.read_u32(); s.skip(2 * n)
        n = s.read_u32(); s.skip(16 * n)
        n = s.read_u32(); s.skip(4 * n)
        n = s.read_u32(); s.skip(n)
    n = s.read_u32(); s.skip(2 * n)            # unk2 (u16 list)
    n = s.read_u32(); s.skip(4 * n)            # textures_references
    n = s.read_u32(); s.skip(n)                # textures_use (u8 list)
    n = s.read_u32(); s.skip(16 * n)           # game_series=2 extra A
    n = s.read_u32(); s.skip(n)                # game_series=2 extra B
    n_det = s.read_u32()
    details = [_read_model_instance_details(s, version) for _ in range(n_det)]
    return PvszZone(indexes=indexes, details=details)


def _read_model_instance_details(
    s: _Stream, version: int,
) -> tuple[list[list[float]], float]:
    s.skip(6)
    tx, ty, tz = s.read_f32(), s.read_f32(), s.read_f32()
    r = [[s.read_f16() for _ in range(3)] for _ in range(3)]
    transform = [
        [r[0][0], r[1][0], r[2][0], tx],
        [r[0][1], r[1][1], r[2][1], ty],
        [r[0][2], r[1][2], r[2][2], tz],
        [0.0, 0.0, 0.0, 1.0],
    ]
    material_data = s.read_f32()
    s.skip(12)
    use_dynamic_3d_data = s.read_u8()
    if use_dynamic_3d_data:
        s.skip(4)
        routes_length = s.read_u8()
        if version >= 51:
            s.skip(2 * routes_length)
        else:
            s.skip(routes_length)
        s.skip(32)
    return transform, material_data


# -- header / texture / model / instance readers (Horizon, v45..51) ----------

def _read_header(s: _Stream) -> PvsHeader:
    magic = s.buf[:4]
    s.skip(4)
    if magic == _MAGIC_LE:
        raise PvsError("PVS magic is little-endian; only Xbox 360 BE is supported.")
    if magic != _MAGIC_BE:
        raise PvsError(f"bad PVS magic {magic!r}")
    version = s.read_u32()
    if version < 45 or version > 51:
        raise PvsError(f"unsupported PVS version {version} (expected 45..51 for Horizon)")
    cpu_data_size = s.read_u32()
    s.skip(4)                                  # version >= 25 trailer (always for Horizon)
    return PvsHeader(version=version, cpu_data_size=cpu_data_size)


def _read_texture(s: _Stream) -> PvsTexture:
    tex = PvsTexture(
        texture_file_name=s.read_u32(),
        index_in_stx_bin=s.read_u32(),
        u_scale=s.read_f32(),
        v_scale=s.read_f32(),
        u_translate=s.read_f32(),
        v_translate=s.read_f32(),
    )
    s.skip(4)
    return tex


def _read_model_instance(s: _Stream, version: int) -> PvsModelInstance:
    model_index = s.read_u16()
    parent_lod_offset = s.read_s16() if version >= 49 else None
    flags = s.read_u32()
    texture = s.read_u32()
    s.skip(4)
    s.skip(2)                                  # game_series=2 trailer
    return PvsModelInstance(
        model_index=model_index,
        parent_lod_offset=parent_lod_offset,
        flags=flags,
        texture=texture,
    )


def _read_model(s: _Stream, model_index: int) -> PvsModel:
    n_tex = s.read_u32()
    textures = [s.read_u32() for _ in range(n_tex)]
    n_sh = s.read_u32()
    shaders = [s.read_u32() for _ in range(n_sh)]
    s.skip(12 * 4 + 12)                        # game_series=2 trailer
    return PvsModel(model_index=model_index, textures=textures, shaders=shaders)


# -- top-level entrypoints ---------------------------------------------------

def parse_pvs(buf: bytes) -> tuple[Pvs, int]:
    """Parse a master ``.pvs`` file. Returns ``(pvs, tools_based_zone_unioning)``."""
    s = _Stream(buf)
    header = _read_header(s)
    version = header.version

    ribbon_index = s.read_u16()
    s.skip(4 + 1)                              # game_series=2
    tools_based_zone_unioning = s.read_u8() if version >= 47 else 0
    s.skip(4)
    if version >= 46:
        s.skip(4)
    if version >= 48:
        n = s.read_u32(); s.skip(4 * n)
    s.skip(1)
    s.skip(2)

    n_zones = s.read_u32()
    for _ in range(n_zones):
        _read_zone_body(s, version, tools_based_zone_unioning)   # discard

    n_tex = s.read_u32()
    textures = [_read_texture(s) for _ in range(n_tex)]

    n_sh = s.read_u32()
    shader_paths = [s.read_string() for _ in range(n_sh)]

    n_inst = s.read_u32()
    models_instances = [_read_model_instance(s, version) for _ in range(n_inst)]

    n_models = s.read_u32()
    models = [_read_model(s, idx) for idx in range(n_models)]

    # version >= 26: three small post-models arrays (sky-related in older builds)
    n0 = s.read_u32(); s.skip(4 * n0)          # game_series=2 (else 2*n0)
    n1 = s.read_u32(); s.skip(2 * n1)
    n2 = s.read_u32(); s.skip(2 * n2)

    n_lone = s.read_u32()
    lone_models_instances = [_read_model_instance(s, version) for _ in range(n_lone)]

    prefix = s.read_string()

    s.skip(2)                                  # game_series=2 trailer
    n_obj_id = s.read_u32()
    for _ in range(n_obj_id):
        ln = s.read_u32(); s.skip(ln)
    n_zone_vis = s.read_u32()
    if n_zone_vis != 0:
        raise PvsError(f"unsupported PVS variant: zone_visibility_length={n_zone_vis}")
    s.skip(1)
    streamed_zones_length = s.read_u32()

    return (
        Pvs(
            header=header,
            ribbon_index=ribbon_index,
            streamed_zones_length=streamed_zones_length,
            textures=textures,
            shader_paths=shader_paths,
            models=models,
            models_instances=models_instances,
            lone_models_instances=lone_models_instances,
            prefix=prefix,
        ),
        tools_based_zone_unioning,
    )


def parse_pvsz(buf: bytes, *, version: int, tools_based_zone_unioning: int) -> PvszZone:
    """Decode one ``__R##Z#####.pvsz`` payload."""
    return _read_zone_body(_Stream(buf), version, tools_based_zone_unioning)


def hydrate_transforms(
    pvs: Pvs,
    *,
    tools_based_zone_unioning: int,
    pvsz_reader,
) -> int:
    """Walk every streamed PVSZ for the ribbon, fill in instance transforms.

    ``pvsz_reader(zone_index) -> bytes`` returns the raw PVSZ payload.
    Raises ``KeyError`` if the requested zone is missing — that zone is
    skipped. Returns the number of instances that received a transform.

    First-write-wins: an index that appears in multiple zones keeps the
    transform from the first PVSZ to mention it. Mirrors the addon's
    ``model_data is None`` gate.
    """
    filled = 0
    for zi in range(pvs.streamed_zones_length):
        try:
            buf = pvsz_reader(zi)
        except KeyError:
            continue
        zone = parse_pvsz(
            buf, version=pvs.header.version,
            tools_based_zone_unioning=tools_based_zone_unioning,
        )
        for index_bitfield, (transform, mat) in zip(zone.indexes, zone.details):
            idx = index_bitfield & 0x7FFFFFFF
            if idx >= len(pvs.models_instances):
                continue
            inst = pvs.models_instances[idx]
            if inst.material_data is None:
                inst.material_data = mat
                inst.transform = transform
                filled += 1
    return filled


# -- file-system helpers -----------------------------------------------------

def find_pvs(ribbon_dir: Path) -> Path:
    """Pick the master ``*.pvs`` from a Ribbon_NN directory."""
    matches = sorted(ribbon_dir.glob("*.pvs"))
    if not matches:
        raise FileNotFoundError(f"no *.pvs under {ribbon_dir}")
    return matches[0]


def make_disk_pvsz_reader(pvsz_dir: Path, ribbon_index: int):
    """Reader for the loose-files layout (PVSZ extracted on disk)."""
    def read(zi: int) -> bytes:
        path = pvsz_dir / f"__R{ribbon_index:02d}Z{zi:05d}.pvsz"
        if not path.exists():
            raise KeyError(str(path))
        return path.read_bytes()
    return read


def make_binzip_pvsz_reader(zip_path: Path, ribbon_index: int):
    """Reader for PVSZ entries living inside ``bin.zip`` (retail Colorado)."""
    from fh1_mapdecomp.binzip import list_entries, read_entry

    entries = {e.filename: e for e in list_entries(zip_path)
               if e.filename.lower().endswith(".pvsz")}

    def read(zi: int) -> bytes:
        name = f"__R{ribbon_index:02d}Z{zi:05d}.pvsz"
        e = entries.get(name)
        if e is None:
            raise KeyError(name)
        return read_entry(zip_path, e)
    return read
