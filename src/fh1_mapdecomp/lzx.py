"""lzxd_helper discovery and invocation.

The helper is a small C program that links libmspack's internal lzxd_* API
and decodes the raw Turn 10 LZX framing used by FH1's bin.zip. See
``src/lzxd_helper.c`` and ``release.sh`` for how it is built.

Search order:
  1. $FH1_LZXD_HELPER (explicit override)
  2. sys._MEIPASS/lzxd_helper            (PyInstaller one-file bundle)
  3. <exe_dir>/lzxd_helper               (shipped next to the executable)
  4. <repo>/src/lzxd_helper              (dev: built in place by release.sh)
"""
from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


LZX_WINDOW_BITS = 17
LZX_RESET_INTERVAL = 0
LZX_CHUNK_USIZE = 32768
LZX_TRAILER = 5


class DecompressionError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def helper_path() -> Path:
    override = os.environ.get("FH1_LZXD_HELPER")
    if override:
        p = Path(override)
        if not p.exists():
            raise FileNotFoundError(f"FH1_LZXD_HELPER points at {p} which does not exist")
        return p

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "lzxd_helper"
        if p.exists():
            return p

    exe_dir = Path(sys.executable).resolve().parent
    here = Path(__file__).resolve()
    src_dir = here.parent.parent                # .../src
    candidates = [
        exe_dir / "lzxd_helper",
        src_dir / "lzxd_helper",
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    raise FileNotFoundError(
        "lzxd_helper not found. Run release.sh --local or set FH1_LZXD_HELPER."
    )


def strip_chunk_headers(blob: bytes, uncomp_size: int) -> bytes:
    """Parse Turn 10's multi-chunk LZX framing into a continuous bitstream.

    Single-chunk entries: ``FF [u16 BE uncomp] [u16 BE comp] <stream> [5-byte trailer]``.
    Multi-chunk entries: each non-last chunk is prefixed with ``u16 BE csize``;
    the last chunk uses the FF-prelude + trailer form.
    """
    out = bytearray()
    pos = 0
    remaining = uncomp_size
    while remaining > 0:
        last = remaining <= LZX_CHUNK_USIZE
        if last:
            if pos + 5 > len(blob):
                raise DecompressionError(
                    f"truncated final chunk header at pos={pos} (need 5, have {len(blob)-pos})"
                )
            if blob[pos] != 0xFF:
                raise DecompressionError(
                    f"expected 0xFF at final-chunk pos={pos}, got 0x{blob[pos]:02x}"
                )
            u = int.from_bytes(blob[pos + 1:pos + 3], "big")
            c = int.from_bytes(blob[pos + 3:pos + 5], "big")
            pos += 5
            end = pos + c
            if end + LZX_TRAILER > len(blob):
                raise DecompressionError(
                    f"truncated final chunk body: comp={c} trailer={LZX_TRAILER} "
                    f"pos={pos} len={len(blob)}"
                )
            out.extend(blob[pos:end])
            pos = end + LZX_TRAILER
            step = u
        else:
            if pos + 2 > len(blob):
                raise DecompressionError(f"truncated chunk header at pos={pos}")
            c = int.from_bytes(blob[pos:pos + 2], "big")
            pos += 2
            end = pos + c
            if end > len(blob):
                raise DecompressionError(
                    f"truncated chunk body: csize={c} pos={pos} len={len(blob)}"
                )
            out.extend(blob[pos:end])
            pos = end
            step = LZX_CHUNK_USIZE
        remaining -= step
    if pos != len(blob):
        raise DecompressionError(f"framing drift: ended at pos={pos}, blob len={len(blob)}")
    return bytes(out)


def decode_lzx(stream: bytes, out_len: int) -> bytes:
    helper = helper_path()
    r = subprocess.run(
        [str(helper), "single", str(LZX_WINDOW_BITS), str(LZX_RESET_INTERVAL), str(out_len)],
        input=stream, capture_output=True, timeout=120,
    )
    if r.returncode != 0:
        raise DecompressionError(
            f"lzxd_helper rc={r.returncode}: {r.stderr.decode(errors='replace').strip()}"
        )
    if len(r.stdout) != out_len:
        raise DecompressionError(
            f"lzxd output length {len(r.stdout)} != expected {out_len}"
        )
    return r.stdout
