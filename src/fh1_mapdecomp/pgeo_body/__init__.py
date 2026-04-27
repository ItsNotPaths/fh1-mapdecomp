"""Per-variant PGEO body decoders.

Each decoder is a ``Callable[[bytes, PgeoHeader], Optional[MeshData]]`` keyed
by the variant tag from ``pgeo.classify``. Variants without a registered
decoder return ``None``, and the Blender import path falls back to a bbox
cube for those chunks.

Add a new variant by dropping a module in this package and calling
``register()`` at import time. Nothing else in the pipeline changes.
"""
from __future__ import annotations

from typing import Callable, Optional

from fh1_mapdecomp.pgeo import PgeoHeader
from fh1_mapdecomp.pgeo_body.types import MeshData

Decoder = Callable[[bytes, PgeoHeader], Optional[MeshData]]

DECODERS: dict[str, Decoder] = {}


def register(variant: str, fn: Decoder) -> None:
    DECODERS[variant] = fn


def decode(variant: str, buf: bytes, header: PgeoHeader) -> Optional[MeshData]:
    fn = DECODERS.get(variant)
    if fn is None:
        return None
    try:
        return fn(buf, header)
    except Exception as ex:
        # A broken decoder must not kill the whole pipeline — the chunk
        # simply falls back to a bbox cube. The error is reported but
        # we keep iterating over the other 41k PGEOs.
        import sys
        print(f"[pgeo_body/{variant}] decode failed: {ex}", file=sys.stderr)
        return None


# Import variant modules so their register() side effects run.
# Each module is optional; a missing module just means that variant has no
# decoder yet and will show as a bbox cube.
def _autoload() -> None:
    import importlib
    import pkgutil
    from fh1_mapdecomp import pgeo_body as _self
    for m in pkgutil.iter_modules(_self.__path__):
        if m.name in ("types", "__init__"):
            continue
        try:
            importlib.import_module(f"fh1_mapdecomp.pgeo_body.{m.name}")
        except Exception as ex:
            import sys
            print(f"[pgeo_body] failed to load {m.name}: {ex}", file=sys.stderr)


_autoload()
