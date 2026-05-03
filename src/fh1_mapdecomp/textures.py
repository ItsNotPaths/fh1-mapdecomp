"""In-memory texture decoder for FH1.

The Blender import script calls into this module to decode textures on
demand from ``bin.zip`` — no PNG/DDS files are written to disk. The
decoded RGBA pixel data is fed into Blender Image objects via
``image.pixels.foreach_set()`` and embedded into ``colorado.blend``
via ``image.pack()``.

Indexing is by integer hash. The hash is the integer value parsed
from the source filename (e.g. ``_0x00001F08.bix`` → ``0x00001F08``).
PVS texture entries reference this same integer via
``PvsTexture.texture_file_name``, so the binding chain just needs an
int → ``TextureData`` resolver.

bin.zip ships duplicate entries with different filename casing
(e.g. ``_0x00001F08_B.bix`` AND ``_0x00001f08_b.bix``). We dedupe
case-insensitively — the integer hash is the canonical key.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import numpy as np

from fh1_mapdecomp import binzip, bix, caff


_HASH_RE = re.compile(r"_0x([0-9A-Fa-f]+)")


def _hash_from_name(name: str) -> Optional[int]:
    m = _HASH_RE.search(name)
    if not m:
        return None
    return int(m.group(1), 16)


class TextureBank:
    """Lazy texture decoder, indexed by integer hash.

    Construct once with the path to ``bin.zip``; first access to a
    given hash decodes the underlying CAFF or BIX blob and caches the
    result. Subsequent accesses are dictionary lookups.

    Both ``_0xHHHH.bin`` (CAFF) and ``_0xHHHH.bix`` (BIX, with paired
    ``_B.bix``) sources are supported. CAFF takes precedence when both
    exist for the same hash (rare).
    """

    def __init__(self, zip_path: Path) -> None:
        self.zip_path = Path(zip_path)
        # Hash → (kind, entry_or_pair). kind is "caff" | "bix".
        self._index: dict[int, tuple[str, object]] = {}
        # Hash → decoded TextureData (or None if decode failed/unsupported).
        self._cache: dict[int, Optional[caff.TextureData]] = {}
        self._build_index()

    def _build_index(self) -> None:
        # Case-insensitive dedup
        by_lower: dict[str, binzip.Entry] = {}
        for e in binzip.list_entries(self.zip_path):
            by_lower[e.filename.lower()] = e
        bin_entries: dict[int, binzip.Entry] = {}
        bix_pairs: dict[int, tuple[binzip.Entry, binzip.Entry]] = {}
        for lname, e in by_lower.items():
            h = _hash_from_name(lname)
            if h is None:
                continue
            if lname.endswith(".bin"):
                bin_entries[h] = e
            elif lname.endswith(".bix") and not lname.endswith("_b.bix"):
                b_lname = lname[:-4] + "_b.bix"
                b_e = by_lower.get(b_lname)
                if b_e is not None:
                    bix_pairs[h] = (e, b_e)
        # CAFF wins on conflict (it's the indexed/atlas case typical
        # for hero textures); BIX fills in the rest.
        for h, e in bin_entries.items():
            self._index[h] = ("caff", e)
        for h, pair in bix_pairs.items():
            if h not in self._index:
                self._index[h] = ("bix", pair)

    def has(self, h: int) -> bool:
        return h in self._index

    def __len__(self) -> int:
        return len(self._index)

    def hashes(self) -> list[int]:
        return list(self._index.keys())

    def get(self, h: int) -> Optional[caff.TextureData]:
        """Decode and return the texture for hash ``h``.

        Returns ``None`` if the hash isn't present, the underlying
        format isn't supported (e.g. cube maps), or decoding raised.
        Caches all outcomes.
        """
        if h in self._cache:
            return self._cache[h]
        loc = self._index.get(h)
        if loc is None:
            self._cache[h] = None
            return None
        kind, payload = loc
        try:
            if kind == "caff":
                buf = binzip.read_entry(self.zip_path, payload)
                tex = caff.decode_texture(buf)
            elif kind == "bix":
                e_a, e_b = payload
                buf_a = binzip.read_entry(self.zip_path, e_a)
                buf_b = binzip.read_entry(self.zip_path, e_b)
                tex = bix.decode_texture(buf_a, buf_b)
            else:
                tex = None
        except Exception:
            tex = None
        self._cache[h] = tex
        return tex

    def get_rgba(self, h: int) -> Optional[tuple[int, int, np.ndarray]]:
        """Decode and return the texture as ``(width, height, rgba)``.

        ``rgba`` is shape (height, width, 4) uint8 — ready to feed to
        Blender as ``image.pixels.foreach_set(rgba.astype(float32) / 255.0)``.

        Returns ``None`` if the texture can't be decoded to RGBA. We
        rely on PIL to do the BC1/BC3/BC4/BC5 decompression (PIL has
        DDS read support for these formats in modern Pillow builds).
        """
        tex = self.get(h)
        if tex is None:
            return None
        dds_bytes = caff.to_dds(tex)
        if dds_bytes is None:
            return None
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(dds_bytes)).convert("RGBA")
            arr = np.array(img, dtype=np.uint8)  # (H, W, 4)
            return (img.width, img.height, arr)
        except Exception:
            return None
