"""lzxd_helper discovery and invocation.

The helper is a small C program that links libmspack's internal lzxd_*
API and decodes the raw Turn 10 LZX framing used by FH1's bin.zip. See
``src/lzxd_helper.c`` and ``release.sh`` for how it is built.

Search order:
  1. $FH1_LZXD_HELPER (explicit override)
  2. sys._MEIPASS/lzxd_helper            (PyInstaller one-file bundle)
  3. <exe_dir>/lzxd_helper               (shipped next to the executable)
  4. <repo>/src/lzxd_helper              (dev: built in place by release.sh)

Decompression goes through a single long-running daemon process: one
``fork``/``exec`` at first use, then a binary stdin/stdout protocol per
request. Saves ~3 ms of fork+exec overhead per LZX entry — the main
cost driver when extracting tens of thousands of bin.zip entries.
"""
from __future__ import annotations

import atexit
import os
import struct
import subprocess
import sys
import threading
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


# ---- daemon client -------------------------------------------------------

_daemon_proc: subprocess.Popen | None = None
_daemon_lock = threading.Lock()
_HDR = struct.Struct(">II")


def _spawn_daemon() -> subprocess.Popen:
    helper = helper_path()
    proc = subprocess.Popen(
        [str(helper), "daemon"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,                # let the user see helper diagnostics
        bufsize=0,                  # we manage buffering ourselves
    )
    if proc.stdin is None or proc.stdout is None:
        raise DecompressionError("failed to attach pipes to lzxd_helper daemon")
    atexit.register(_shutdown_daemon)
    return proc


def _shutdown_daemon() -> None:
    global _daemon_proc
    p = _daemon_proc
    _daemon_proc = None
    if p is None:
        return
    try:
        if p.stdin and not p.stdin.closed:
            p.stdin.close()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _read_exact(fp, n: int) -> bytes:
    """Read exactly ``n`` bytes from a binary pipe; raise on short read."""
    if n == 0:
        return b""
    chunks: list[bytes] = []
    got = 0
    while got < n:
        b = fp.read(n - got)
        if not b:
            raise DecompressionError(
                f"lzxd_helper daemon closed pipe after {got}/{n} bytes"
            )
        chunks.append(b)
        got += len(b)
    return b"".join(chunks) if len(chunks) > 1 else chunks[0]


def decode_lzx(stream: bytes, out_len: int) -> bytes:
    """Decompress one stripped LZX bitstream via the helper daemon.

    The stream must already have its multi-chunk framing removed (use
    ``strip_chunk_headers`` for ``bin.zip`` entries). ``out_len`` is the
    expected uncompressed length and is required by libmspack's lzxd
    decoder.
    """
    global _daemon_proc
    with _daemon_lock:
        if _daemon_proc is None or _daemon_proc.poll() is not None:
            _daemon_proc = _spawn_daemon()
        proc = _daemon_proc

        try:
            proc.stdin.write(_HDR.pack(out_len, len(stream)))
            if stream:
                proc.stdin.write(stream)
            proc.stdin.flush()

            hdr = _read_exact(proc.stdout, _HDR.size)
            status, payload_len = _HDR.unpack(hdr)
            payload = _read_exact(proc.stdout, payload_len)
        except (BrokenPipeError, DecompressionError):
            # Helper crashed or closed. Reap and surface the error.
            try:
                proc.kill()
            except Exception:
                pass
            _daemon_proc = None
            raise DecompressionError("lzxd_helper daemon died mid-request")

        if status != 0:
            raise DecompressionError(
                f"lzxd_helper: {payload.decode('utf-8', errors='replace').strip()}"
            )
        if len(payload) != out_len:
            raise DecompressionError(
                f"lzxd output length {len(payload)} != expected {out_len}"
            )
        return payload
