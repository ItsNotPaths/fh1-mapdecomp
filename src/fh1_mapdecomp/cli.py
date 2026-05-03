"""Command-line interface for fh1-mapdecomp.

    fh1-mapdecomp list           --source <bin.zip | game dir> [--glob PAT]...
    fh1-mapdecomp extract        --source <bin.zip | game dir> --output <dir> [--glob PAT]...
    fh1-mapdecomp world          --source <bin.zip | game dir> --output <dir>
    fh1-mapdecomp blender        --source <bin.zip | game dir> --output <dir> [--blender PATH]
    fh1-mapdecomp render-topdown --output <dir> [--blender PATH]
    fh1-mapdecomp all            --source <bin.zip | game dir> --output <dir> [--blender PATH]

``--source`` accepts either a bin.zip path directly or a directory that
contains one (the extracted 4D5309C9 layout works). ``--output`` is the
target directory; ``all`` writes ``<output>/extracted/...``, ``<output>/world/ribbon_00.json``,
``<output>/meshes/*.npz`` (for variants with a decoder), and ``<output>/blender/colorado.blend``.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path

from fh1_mapdecomp import __version__
from fh1_mapdecomp.binzip import (
    Entry, list_entries, read_entry, resolve_binzip,
)
from fh1_mapdecomp.blender_run import (
    BlenderNotFound, blender_path, run_import, run_render,
)
from fh1_mapdecomp.collobjs import (
    extract_collobjs, summarise as collobjs_summarise,
)
from fh1_mapdecomp.lzx import DecompressionError
from fh1_mapdecomp.pvs_inst import (
    extract_pvs_instances, summarise as pvs_inst_summarise,
)
from fh1_mapdecomp.terrain_hi import extract_terrain_hi, summarise as terrain_hi_summarise
from fh1_mapdecomp.world import build_placement, summarise, write_placement


def _filter(entries: list[Entry], globs: list[str] | None, limit: int | None) -> list[Entry]:
    if globs:
        entries = [e for e in entries if any(fnmatch.fnmatch(e.filename, g) for g in globs)]
    if limit:
        entries = entries[:limit]
    return entries


def _unique_dst(dst: Path) -> Path:
    """bin.zip has duplicate filenames in some ribbons; disambiguate on write."""
    if not dst.exists():
        return dst
    base, ext = os.path.splitext(dst.name)
    n = 1
    while True:
        alt = dst.parent / f"{base}__dup{n}{ext}"
        if not alt.exists():
            return alt
        n += 1


def _progress(prefix: str):
    def fn(i: int, total: int) -> None:
        print(f"  [{prefix}] {i}/{total}", file=sys.stderr)
    return fn


# ---- commands ---------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    entries = _filter(list_entries(zip_path), args.glob, None)
    for e in entries:
        print(f"{e.method:3d}  {e.compressed_size:>10d} -> {e.uncompressed_size:>10d}  {e.filename}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = _filter(list_entries(zip_path), args.glob, args.limit)

    ok = fail = 0
    for i, e in enumerate(entries):
        if i % 2000 == 0 and i:
            print(f"  [extract] {i}/{len(entries)}", file=sys.stderr)
        dst = _unique_dst(out_dir / e.filename)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.write_bytes(read_entry(zip_path, e))
            ok += 1
            if args.verbose:
                print(f"OK  {e.filename} -> {dst}")
        except DecompressionError as ex:
            fail += 1
            print(f"FAIL {e.filename}: {ex}", file=sys.stderr)
            if args.stop_on_error:
                break
    print(f"extracted {ok}, failed {fail}", file=sys.stderr)
    return 0 if fail == 0 else 1


def cmd_world(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    out_dir = Path(args.output)
    meshes_dir: Path | None = None
    if not getattr(args, "no_meshes", False):
        meshes_dir = out_dir / "meshes"
    doc = build_placement(zip_path, meshes_dir=meshes_dir, progress=_progress("world"))
    out_file = out_dir / "world" / "ribbon_00.json"
    write_placement(doc, out_file)
    summarise(doc)
    print(f"wrote {out_file}", file=sys.stderr)
    if meshes_dir is not None:
        print(f"mesh cache: {meshes_dir} ({doc.get('meshes_written', 0)} files)", file=sys.stderr)
    return 0


def cmd_terrain_hi(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    out_dir = Path(args.output) / "terrain_hi"
    doc = extract_terrain_hi(
        zip_path, out_dir, lod=args.lod,
        include_middist=getattr(args, "include_middist", False),
        progress=_progress("terrain_hi"),
    )
    terrain_hi_summarise(doc)
    print(f"wrote {out_dir}/index.json", file=sys.stderr)
    return 0


def _resolve_ribbon_dir(source: Path, explicit: str | None) -> Path:
    """Locate the Ribbon_00 dir. Sits next to bin.zip in the extracted layout."""
    if explicit:
        return Path(explicit)
    zip_path = resolve_binzip(source)
    candidate = zip_path.parent / "Ribbon_00"
    if candidate.exists():
        return candidate
    for c in source.rglob("Ribbon_00"):
        if (c / "CollObjs.xml").exists():
            return c
    raise FileNotFoundError(
        f"could not find Ribbon_00/ near {zip_path}. "
        f"Pass --ribbon-dir explicitly."
    )


def cmd_pvs_inst(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    ribbon_dir = _resolve_ribbon_dir(Path(args.source), args.ribbon_dir)
    out_dir = Path(args.output) / "pvs_inst"
    doc = extract_pvs_instances(
        zip_path, ribbon_dir, out_dir,
        policy=args.policy,
        progress=_progress("pvs_inst"),
    )
    pvs_inst_summarise(doc)
    print(f"wrote {out_dir}/index.json", file=sys.stderr)
    return 0


def cmd_collobjs_inst(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    ribbon_dir = _resolve_ribbon_dir(Path(args.source), args.ribbon_dir)
    out_dir = Path(args.output) / "collobjs_inst"
    doc = extract_collobjs(
        zip_path, ribbon_dir, out_dir,
        policy=args.policy,
        include_all_lods=getattr(args, "all_lods", False),
        fuzzy=getattr(args, "fuzzy", False),
        progress=_progress("collobjs"),
    )
    collobjs_summarise(doc)
    print(f"wrote {out_dir}/index.json", file=sys.stderr)
    return 0


def cmd_blender(args: argparse.Namespace) -> int:
    out_dir = Path(args.output)
    json_path = out_dir / "world" / "ribbon_00.json"
    if not json_path.exists():
        print(
            f"ribbon JSON missing: {json_path}. Run `fh1-mapdecomp world` first.",
            file=sys.stderr,
        )
        return 2
    meshes_dir = out_dir / "meshes"
    if args.no_meshes or not meshes_dir.exists():
        meshes_dir = None
    terrain_hi_dir: Path | None = out_dir / "terrain_hi"
    if getattr(args, "no_terrain_hi", False) or not (terrain_hi_dir / "index.json").exists():
        terrain_hi_dir = None
    collobjs_dir: Path | None = out_dir / "collobjs_inst"
    if getattr(args, "no_collobjs", False) or not (collobjs_dir / "index.json").exists():
        collobjs_dir = None
    pvs_inst_dir: Path | None = out_dir / "pvs_inst"
    if getattr(args, "no_pvs_inst", False) or not (pvs_inst_dir / "index.json").exists():
        pvs_inst_dir = None
    out_blend = out_dir / "blender" / "colorado.blend"
    out_blend.parent.mkdir(parents=True, exist_ok=True)
    try:
        blender = blender_path(args.blender)
    except BlenderNotFound as ex:
        print(str(ex), file=sys.stderr)
        return 3
    print(f"[blender] using {blender}", file=sys.stderr)
    rc = run_import(
        blender=blender,
        json_path=json_path,
        out_blend=out_blend,
        meshes_dir=meshes_dir,
        terrain_hi_dir=terrain_hi_dir,
        collobjs_dir=collobjs_dir,
        pvs_inst_dir=pvs_inst_dir,
        limit=args.limit or 0,
        variants=args.variants or "",
        no_cubes=args.no_cubes,
    )
    if rc == 0:
        print(f"wrote {out_blend}", file=sys.stderr)
    return rc


def cmd_render(args: argparse.Namespace) -> int:
    out_dir = Path(args.output)
    in_blend = out_dir / "blender" / "colorado.blend"
    if not in_blend.exists():
        print(f"blend missing: {in_blend}. Run `fh1-mapdecomp blender` first.", file=sys.stderr)
        return 2
    out_png = out_dir / "blender" / "colorado_topdown.png"
    try:
        blender = blender_path(args.blender)
    except BlenderNotFound as ex:
        print(str(ex), file=sys.stderr)
        return 3
    rc = run_render(blender=blender, in_blend=in_blend, out_png=out_png)
    if rc == 0:
        print(f"wrote {out_png}", file=sys.stderr)
    return rc


def cmd_all(args: argparse.Namespace) -> int:
    out_dir = Path(args.output)

    extract_args = argparse.Namespace(
        source=args.source, output=str(out_dir / "extracted"),
        glob=args.glob, limit=args.limit,
        verbose=False, stop_on_error=False,
    )
    rc = cmd_extract(extract_args)
    if rc != 0 and not args.keep_going:
        return rc

    world_args = argparse.Namespace(
        source=args.source, output=str(out_dir),
        no_meshes=args.no_meshes,
    )
    rc = cmd_world(world_args)
    if rc != 0 and not args.keep_going:
        return rc

    if not args.no_terrain_hi:
        terrain_hi_args = argparse.Namespace(
            source=args.source, output=str(out_dir), lod="00",
        )
        rc = cmd_terrain_hi(terrain_hi_args)
        if rc != 0 and not args.keep_going:
            return rc

    # PVS is the authoritative authored-placement table for "draw this
    # mesh at these world transforms". CollObjs.xml is the static-prop
    # placement table — complementary to PVS (collision props like
    # OBJ_BarrierBrand have PVS pos = (0,0,0) and the real transforms
    # live in CollObjs).
    if not args.no_pvs_inst:
        pvs_args = argparse.Namespace(
            source=args.source, output=str(out_dir),
            ribbon_dir=getattr(args, "ribbon_dir", None),
            policy=getattr(args, "pvs_policy", "freeroam"),
        )
        try:
            rc = cmd_pvs_inst(pvs_args)
        except FileNotFoundError as ex:
            print(f"[pvs_inst] skipping: {ex}", file=sys.stderr)
            rc = 0
        if rc != 0 and not args.keep_going:
            return rc

    if not args.no_collobjs:
        collobjs_args = argparse.Namespace(
            source=args.source, output=str(out_dir),
            ribbon_dir=getattr(args, "ribbon_dir", None),
            policy=getattr(args, "collobjs_policy", "freeroam"),
            all_lods=False,
            fuzzy=getattr(args, "collobjs_fuzzy", False),
        )
        try:
            rc = cmd_collobjs_inst(collobjs_args)
        except FileNotFoundError as ex:
            print(f"[collobjs] skipping: {ex}", file=sys.stderr)
            rc = 0
        if rc != 0 and not args.keep_going:
            return rc

    # TODO: vegetation extractor goes here when implemented (PGEO grass
    # / vegetation / crowd variants — currently undecoded bodies).

    if args.no_blender:
        return rc

    blender_args = argparse.Namespace(
        output=str(out_dir),
        blender=args.blender,
        limit=0,
        variants="",
        no_cubes=False,
        no_meshes=args.no_meshes,
        no_terrain_hi=args.no_terrain_hi,
        no_collobjs=args.no_collobjs,
        no_pvs_inst=args.no_pvs_inst,
    )
    rc = cmd_blender(blender_args)
    if rc != 0 and not args.keep_going:
        return rc

    if not args.render:
        return rc

    render_args = argparse.Namespace(output=str(out_dir), blender=args.blender)
    return cmd_render(render_args)


# ---- parser -----------------------------------------------------------------

def _add_source(p: argparse.ArgumentParser, *, output: bool) -> None:
    p.add_argument("--source", required=True,
                   help="path to bin.zip or a directory containing one")
    if output:
        p.add_argument("--output", required=True, help="output directory")


def _add_blender_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--blender", help="path to blender (overrides $FH1_BLENDER, PATH)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fh1-mapdecomp",
        description="Extract and reconstruct the Forza Horizon 1 Colorado world map.",
    )
    p.add_argument("--version", action="version", version=f"fh1-mapdecomp {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    lp = sub.add_parser("list", help="list entries in bin.zip")
    _add_source(lp, output=False)
    lp.add_argument("--glob", action="append", help="glob filter (repeatable)")
    lp.set_defaults(func=cmd_list)

    ep = sub.add_parser("extract", help="extract bin.zip entries to disk")
    _add_source(ep, output=True)
    ep.add_argument("--glob", action="append", help="glob filter (repeatable)")
    ep.add_argument("--limit", type=int, help="stop after N matched entries")
    ep.add_argument("--verbose", action="store_true")
    ep.add_argument("--stop-on-error", action="store_true")
    ep.set_defaults(func=cmd_extract)

    wp = sub.add_parser("world", help="build placement JSON + per-chunk mesh cache")
    _add_source(wp, output=True)
    wp.add_argument("--no-meshes", action="store_true",
                    help="skip per-chunk mesh decoding; emit placement JSON only")
    wp.set_defaults(func=cmd_world)

    tp = sub.add_parser("terrain-hi",
                        help="extract hi-detail TERR meshes from rmb.bin pool")
    _add_source(tp, output=True)
    tp.add_argument("--lod", default="00",
                    help="LOD digits to extract ('00' = highest; '' = all)")
    tp.add_argument("--include-middist", action="store_true",
                    help="include MIDDIST (mid-distance) tiles (default: skip)")
    tp.set_defaults(func=cmd_terrain_hi)

    pip = sub.add_parser("pvs-inst",
                         help="extract PVS+PVSZ → rmb instance meshes "
                              "(authoritative authored placements)")
    _add_source(pip, output=True)
    pip.add_argument("--ribbon-dir",
                     help="path to Ribbon_NN/ (auto-detected next to bin.zip if omitted)")
    pip.add_argument("--policy", choices=("freeroam", "all"), default="freeroam",
                     help="freeroam drops race-event dressing (Festival_Area01..04, "
                          "Ambulance, Countdown, PROC_cars); all keeps every placement")
    pip.set_defaults(func=cmd_pvs_inst)

    cop = sub.add_parser("collobjs-inst",
                         help="extract Ribbon_00/CollObjs.xml → rmb instance meshes")
    _add_source(cop, output=True)
    cop.add_argument("--ribbon-dir",
                     help="path to Ribbon_00/ (auto-detected next to bin.zip if omitted)")
    cop.add_argument("--policy", choices=("freeroam", "all"), default="freeroam",
                     help="freeroam drops CO_FEST_*/OBJ_FEST_*; all keeps every placement")
    cop.add_argument("--all-lods", action="store_true",
                     help="include LOD01/LOD02 blobs (default: LOD0 only)")
    cop.add_argument("--fuzzy", action="store_true",
                     help="enable normalised-key fallback "
                          "(catches more placements but risks wrong-model "
                          "matches for bases missing from the rmb pool)")
    cop.set_defaults(func=cmd_collobjs_inst)

    bp = sub.add_parser("blender", help="build colorado.blend from placement JSON")
    bp.add_argument("--output", required=True, help="output directory (same as used for world)")
    _add_blender_args(bp)
    bp.add_argument("--limit", type=int, help="cap chunks (0=all)")
    bp.add_argument("--variants", help="comma-separated variant filter")
    bp.add_argument("--no-cubes", action="store_true",
                    help="use empties instead of bbox-cube meshes (faster viewport)")
    bp.add_argument("--no-meshes", action="store_true",
                    help="ignore out/meshes/ cache, force bbox-cube-only scene")
    bp.add_argument("--no-terrain-hi", action="store_true",
                    help="skip the rmb.bin TERR mesh collection")
    bp.add_argument("--no-collobjs", action="store_true",
                    help="skip the CollObjs.xml → rmb instance collection")
    bp.add_argument("--no-pvs-inst", action="store_true",
                    help="skip the PVS+PVSZ instance collection")
    bp.set_defaults(func=cmd_blender)

    rp = sub.add_parser("render-topdown", help="render orthographic top-down PNG of colorado.blend")
    rp.add_argument("--output", required=True, help="output directory")
    _add_blender_args(rp)
    rp.set_defaults(func=cmd_render)

    ap = sub.add_parser("all", help="extract + world + meshes + blender in one pass")
    _add_source(ap, output=True)
    ap.add_argument("--glob", action="append")
    ap.add_argument("--limit", type=int)
    _add_blender_args(ap)
    ap.add_argument("--no-meshes", action="store_true",
                    help="skip per-chunk mesh decoding and cache")
    ap.add_argument("--no-terrain-hi", action="store_true",
                    help="skip hi-detail TERR extraction from rmb.bin")
    ap.add_argument("--no-pvs-inst", action="store_true",
                    help="skip PVS+PVSZ → rmb instance extraction")
    ap.add_argument("--pvs-policy", choices=("freeroam", "all"),
                    default="freeroam",
                    help="PVS filter policy (default: freeroam)")
    ap.add_argument("--no-collobjs", action="store_true",
                    help="skip CollObjs.xml → rmb instance extraction")
    ap.add_argument("--ribbon-dir",
                    help="path to Ribbon_00/ for PVS+CollObjs (auto-detected if omitted)")
    ap.add_argument("--collobjs-policy", choices=("freeroam", "all"),
                    default="freeroam",
                    help="CollObjs filter policy (default: freeroam)")
    ap.add_argument("--collobjs-fuzzy", action="store_true",
                    help="enable CollObjs normalised-key fallback "
                         "(may place wrong meshes for bases missing from "
                         "the rmb pool)")
    ap.add_argument("--no-blender", action="store_true",
                    help="stop after placement JSON; do not invoke Blender")
    ap.add_argument("--render", action="store_true",
                    help="also render a top-down PNG after building the blend")
    ap.add_argument("--keep-going", action="store_true",
                    help="continue downstream steps even if an earlier one reports failures")
    ap.set_defaults(func=cmd_all)

    return p


CONF_NAME = "fh1-mapdecomp.conf"


def _conf_path() -> Path:
    """Default conf lives next to the executable (or at the repo root in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / CONF_NAME
    return Path(__file__).resolve().parent.parent.parent / CONF_NAME


def _load_conf_argv(path: Path) -> list[str]:
    """One CLI token per non-empty, non-comment line."""
    tokens: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens.append(line)
    return tokens


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) <= 1:
        conf = _conf_path()
        if conf.exists():
            argv = _load_conf_argv(conf)
            if argv:
                print(f"[conf] using args from {conf}", file=sys.stderr)
    args = build_parser().parse_args(argv)
    return args.func(args)
