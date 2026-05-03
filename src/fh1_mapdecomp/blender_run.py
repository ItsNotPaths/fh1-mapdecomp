"""Blender executable + helper-script discovery, and headless invocation.

Search order for the Blender binary (first hit wins):
  1. ``$FH1_BLENDER``
  2. explicit ``--blender`` path passed in
  3. ``shutil.which("blender")``
  4. ``/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender``
     (the project's known install; harmless when absent)

Search order for the bundled helper scripts (``import_world.py`` et al):
  1. ``sys._MEIPASS/fh1_mapdecomp/blender_scripts/<name>`` (PyInstaller bundle)
  2. ``<package>/blender_scripts/<name>`` (installed/dev layout)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence


STEAM_BLENDER_FALLBACK = Path(
    "/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender"
)


class BlenderNotFound(FileNotFoundError):
    pass


def blender_path(explicit: Optional[str | Path] = None) -> Path:
    env = os.environ.get("FH1_BLENDER")
    if env:
        p = Path(env)
        if not p.exists():
            raise BlenderNotFound(f"$FH1_BLENDER points at {p} which does not exist")
        return p
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise BlenderNotFound(f"--blender points at {p} which does not exist")
        return p
    on_path = shutil.which("blender")
    if on_path:
        return Path(on_path)
    if STEAM_BLENDER_FALLBACK.exists():
        return STEAM_BLENDER_FALLBACK
    raise BlenderNotFound(
        "blender not found. Set $FH1_BLENDER, pass --blender, "
        "install it on PATH, or place it at the Steam fallback path."
    )


@lru_cache(maxsize=None)
def _script_path(name: str) -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "fh1_mapdecomp" / "blender_scripts" / name
        if p.exists():
            return p
    here = Path(__file__).resolve().parent
    p = here / "blender_scripts" / name
    if p.exists():
        return p
    raise FileNotFoundError(f"blender helper script missing: {name}")


def import_script_path() -> Path:
    return _script_path("import_world.py")


def render_script_path() -> Path:
    return _script_path("render_topdown.py")


def _run(blender: Path, script: Path, script_args: Sequence[str]) -> int:
    argv = [str(blender), "--background", "--python", str(script), "--", *script_args]
    r = subprocess.run(argv)
    return r.returncode


def run_import(
    *,
    blender: Path,
    json_path: Path,
    out_blend: Path,
    meshes_dir: Optional[Path] = None,
    terrain_hi_dir: Optional[Path] = None,
    collobjs_dir: Optional[Path] = None,
    pvs_inst_dir: Optional[Path] = None,
    crowd_inst_dir: Optional[Path] = None,
    grass_inst_dir: Optional[Path] = None,
    limit: int = 0,
    variants: str = "",
    no_cubes: bool = False,
) -> int:
    script = import_script_path()
    args = ["--json", str(json_path), "--out", str(out_blend)]
    if meshes_dir is not None:
        args += ["--meshes", str(meshes_dir)]
    if terrain_hi_dir is not None:
        args += ["--terrain-hi", str(terrain_hi_dir)]
    if collobjs_dir is not None:
        args += ["--collobjs", str(collobjs_dir)]
    if pvs_inst_dir is not None:
        args += ["--pvs-inst", str(pvs_inst_dir)]
    if crowd_inst_dir is not None:
        args += ["--crowd-inst", str(crowd_inst_dir)]
    if grass_inst_dir is not None:
        args += ["--grass-inst", str(grass_inst_dir)]
    if limit:
        args += ["--limit", str(limit)]
    if variants:
        args += ["--variants", variants]
    if no_cubes:
        args += ["--no-cubes"]
    return _run(blender, script, args)


def run_render(*, blender: Path, in_blend: Path, out_png: Path) -> int:
    script = render_script_path()
    return _run(blender, script, [str(in_blend), str(out_png)])
