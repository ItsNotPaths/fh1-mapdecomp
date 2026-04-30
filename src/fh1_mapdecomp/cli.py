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
from fh1_mapdecomp.rmb_world import (
    extract_rmb_world, summarise as rmb_world_summarise,
)
from fh1_mapdecomp.terrain_hi import extract_terrain_hi, summarise as terrain_hi_summarise
from fh1_mapdecomp.v42k7_inst import (
    extract_v42k7_instances, summarise as v42k7_inst_summarise,
)
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


def cmd_rmb_world(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    out_dir = Path(args.output) / "rmb_world"
    doc = extract_rmb_world(
        zip_path, out_dir,
        policy=getattr(args, "policy", "freeroam"),
        include_all_lods=getattr(args, "all_lods", False),
        progress=_progress("rmb_world"),
    )
    rmb_world_summarise(doc)
    print(f"wrote {out_dir}/index.json", file=sys.stderr)
    return 0


def cmd_v42k7_inst(args: argparse.Namespace) -> int:
    zip_path = resolve_binzip(Path(args.source))
    out_dir = Path(args.output) / "v42k7_inst"

    # Cross-source dedup: rmb_world owns landmark/persistent placements
    # via its wrapper-rmb path (Smelter, cables, MT_Area decals,
    # Pavementcaps, Object NNN, etc.). v42k7 references those same
    # handles as streaming/visibility hints — emitting both produces
    # rings of duplicates around chunks that load each landmark.
    # If rmb_world has already been extracted, load its tag set and
    # exclude any v42k7 blob whose stripped tag matches. Also load
    # the per-family LOD floor so the LOD filter is consistent across
    # pipelines (some families have only LOD01 in the pool, no LOD00).
    exclude_tag_keys: set[str] | None = None
    family_min_lod: dict | None = None
    rmb_world_index = Path(args.output) / "rmb_world" / "index.json"
    if rmb_world_index.exists():
        try:
            from fh1_mapdecomp.v42k7_inst import _strip_lod_suffix
            rw = json.loads(rmb_world_index.read_text())
            # Include both ``tag`` (the per-section asset name like
            # ``PLA_SMELTER_BLDG_BlastFurnaceLow_001``) and ``wrapper_tag``
            # (the source rmb's name like
            # ``PLA_SMELTER_BLDG_BlastFurnaceLow_LOD00``). v42k7 handles
            # carry the wrapper-tag form, so without including it the
            # cross-source dedup misses the smelter and similar landmarks.
            exclude_tag_keys = set()
            for b in rw.get("blobs", []):
                exclude_tag_keys.add(_strip_lod_suffix(b.get("tag", "")))
                w = b.get("wrapper_tag", "")
                if w and w != b.get("tag"):
                    exclude_tag_keys.add(_strip_lod_suffix(w))
            exclude_tag_keys.discard("")
            family_min_lod = rw.get("family_min_lod")
            print(f"[v42k7_inst] dedup vs rmb_world: "
                  f"{len(exclude_tag_keys)} tag keys, "
                  f"family_min_lod: {len(family_min_lod or {})} families",
                  file=sys.stderr)
        except Exception as ex:
            print(f"[v42k7_inst] could not load rmb_world tags ({ex}); "
                  f"running without dedup", file=sys.stderr)

    doc = extract_v42k7_instances(
        zip_path, out_dir,
        include_all_lods=getattr(args, "all_lods", False),
        policy=getattr(args, "policy", "freeroam"),
        progress=_progress("v42k7_inst"),
        exclude_tag_keys=exclude_tag_keys,
        family_min_lod=family_min_lod,
    )
    v42k7_inst_summarise(doc)
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


def cmd_barriers_inst(args: argparse.Namespace) -> int:
    """Filter an existing v42k7_inst output to a barriers-only subset.

    The v42k7 within-section RENDER mask is known good for barrier-tagged
    sections (see docs/world-architecture.md §5.2 and the
    project_v42k7_within_section_selector_solved memory). The wider v42k7
    output has unsolved slot-picker indirection for buildings, so we
    can't ship that as-is — but barriers, which DO place correctly,
    can be peeled off into their own collection.
    """
    import re
    import shutil

    out_dir = Path(args.output)
    in_dir = out_dir / "v42k7_inst"
    bar_dir = out_dir / "v42k7_barriers"
    in_index = in_dir / "index.json"
    if not in_index.exists():
        print(f"[barriers-inst] no source: {in_index} "
              f"(run `fh1-mapdecomp v42k7-inst` first)", file=sys.stderr)
        return 2

    # Anchored full-tag patterns. Each must match the tag from start; we
    # explicitly drop _LOD01/_LOD02/_LOD03/MIDDIST forms because those
    # are streaming impostors the engine paints across many chunk slot
    # lists as visibility hints — not real placements (placing them
    # produces the "scattered under-ground Barrier_023_Armco_LOD01"
    # leak). LOD00 + NOLOD are the only render-correct variants.
    keep_patterns = (
        r"OBJ_BarrierBrand_LOD00(?:_|$)",
        r"Barrier_\d+_Armco_LOD00(?:_|$)",
        r"Barrier_\d+_Steel_LOD00(?:_|$)",
        r"BarrierMetal_LOD00(?:_|$)",
        r"Maintown_WallSmall_.*Railings_\d+M_NOLOD$",
        r"Maintown_WallSmall_.*EndPiece_NOLOD$",
        r"OBJ_CLRD_FenceF_LOD00(?:_|$)",
        r"O_CO_CLRD_FenceF$",
        r"CO_CLRD_FenceF$",
    )
    keep_re = re.compile("|".join(f"(?:^{p})" for p in keep_patterns))

    doc = json.loads(in_index.read_text())
    src_blobs = doc["blobs"]
    keep_blobs = [b for b in src_blobs if keep_re.match(b.get("tag", ""))]
    keep_handles = {b["handle"] for b in keep_blobs}
    if not keep_blobs:
        print("[barriers-inst] no barrier-tagged blobs found in source",
              file=sys.stderr)
        return 1

    out_chunks: list[dict] = []
    inst_total = 0
    for c in doc["chunks"]:
        new_secs = []
        for s in c["sections"]:
            kept = [h for h in s["handles"] if h in keep_handles]
            if not kept:
                continue
            sec = {"handles": kept, "instances": s["instances"]}
            if "u0" in s:
                sec["u0"] = s["u0"]
            new_secs.append(sec)
            inst_total += len(s["instances"])
        if new_secs:
            out_chunks.append({
                "filename": c["filename"], "name": c["name"],
                "origin": c["origin"], "sections": new_secs,
            })

    bar_dir.mkdir(parents=True, exist_ok=True)
    (bar_dir / "blobs").mkdir(exist_ok=True)
    for b in keep_blobs:
        src = in_dir / b["npz"]
        if src.exists():
            shutil.copyfile(src, bar_dir / b["npz"])

    out_doc = {
        "source_index": str(in_index),
        "policy": "barriers_only",
        "keep_patterns": list(keep_patterns),
        "rmb_pool_size": doc.get("rmb_pool_size"),
        "chunk_count": len(out_chunks),
        "blob_count": len(keep_blobs),
        "instances_after_filter": inst_total,
        "blobs": keep_blobs,
        "chunks": out_chunks,
    }
    (bar_dir / "index.json").write_text(json.dumps(out_doc))
    print(f"[barriers-inst] {len(keep_blobs)} blobs, {len(out_chunks)} chunks, "
          f"{inst_total} instances → {bar_dir}/index.json", file=sys.stderr)
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
    v42k7_inst_dir: Path | None = out_dir / "v42k7_inst"
    if getattr(args, "no_v42k7_inst", False) or not (v42k7_inst_dir / "index.json").exists():
        v42k7_inst_dir = None
    v42k7_barriers_dir: Path | None = out_dir / "v42k7_barriers"
    if getattr(args, "no_v42k7_barriers", False) or not (v42k7_barriers_dir / "index.json").exists():
        v42k7_barriers_dir = None
    collobjs_dir: Path | None = out_dir / "collobjs_inst"
    if getattr(args, "no_collobjs", False) or not (collobjs_dir / "index.json").exists():
        collobjs_dir = None
    rmb_world_dir: Path | None = out_dir / "rmb_world"
    if getattr(args, "no_rmb_world", False) or not (rmb_world_dir / "index.json").exists():
        rmb_world_dir = None
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
        v42k7_inst_dir=v42k7_inst_dir,
        v42k7_barriers_dir=v42k7_barriers_dir,
        collobjs_dir=collobjs_dir,
        rmb_world_dir=rmb_world_dir,
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

    # rmb_world runs before v42k7_inst so its blob tags are available
    # for cross-source dedup. Without this ordering, v42k7_inst emits
    # ~30k duplicate instances (smelters, cables, decals, road segments)
    # that are already placed by rmb_world's wrapper-rmb path.
    if not args.no_rmb_world:
        rmb_world_args = argparse.Namespace(
            source=args.source, output=str(out_dir),
            policy=getattr(args, "rmb_world_policy", "freeroam"),
            all_lods=False,
        )
        rc = cmd_rmb_world(rmb_world_args)
        if rc != 0 and not args.keep_going:
            return rc

    if not args.no_v42k7_inst:
        v42k7_args = argparse.Namespace(
            source=args.source, output=str(out_dir),
            policy=getattr(args, "v42k7_policy", "freeroam"),
        )
        rc = cmd_v42k7_inst(v42k7_args)
        if rc != 0 and not args.keep_going:
            return rc

    if not args.no_v42k7_barriers and not args.no_v42k7_inst:
        bar_args = argparse.Namespace(output=str(out_dir))
        rc = cmd_barriers_inst(bar_args)
        if rc not in (0, 1) and not args.keep_going:
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
        no_v42k7_inst=args.no_v42k7_inst,
        no_v42k7_barriers=getattr(args, "no_v42k7_barriers", False),
        no_collobjs=args.no_collobjs,
        no_rmb_world=args.no_rmb_world,
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

    rwp = sub.add_parser("rmb-world",
                         help="extract world-placed rmb blobs at their header centroid")
    _add_source(rwp, output=True)
    rwp.add_argument("--policy", choices=("freeroam", "all"), default="freeroam",
                     help="freeroam drops race/festival dressing; all keeps every "
                          "world-placed blob (TERR_ always dropped — owned by terrain_hi)")
    rwp.add_argument("--all-lods", action="store_true",
                     help="include LOD01/LOD02/MIDDIST duplicates (default: LOD00 + no-LOD only)")
    rwp.set_defaults(func=cmd_rmb_world)

    vp = sub.add_parser("v42k7-inst",
                        help="extract v42k7 → rmb instance meshes")
    _add_source(vp, output=True)
    vp.add_argument("--all-lods", action="store_true",
                    help="include LOD01/LOD02/MIDDIST blobs (default: LOD0 only)")
    vp.add_argument("--policy", choices=("freeroam", "all"), default="freeroam",
                    help="freeroam drops race/festival dressing + tags owned by "
                         "CollObjs/GameObjs; all keeps every section")
    vp.set_defaults(func=cmd_v42k7_inst)

    bip = sub.add_parser("barriers-inst",
                         help="filter v42k7_inst to roadside-barrier blobs only")
    bip.add_argument("--output", required=True,
                     help="output directory (same as used for v42k7-inst); "
                          "reads <output>/v42k7_inst/, writes <output>/v42k7_barriers/")
    bip.set_defaults(func=cmd_barriers_inst)

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
    bp.add_argument("--no-v42k7-inst", action="store_true",
                    help="skip the v42k7 → rmb instance collection")
    bp.add_argument("--no-v42k7-barriers", action="store_true",
                    help="skip the barriers-only v42k7 collection")
    bp.add_argument("--no-collobjs", action="store_true",
                    help="skip the CollObjs.xml → rmb instance collection")
    bp.add_argument("--no-rmb-world", action="store_true",
                    help="skip the rmb world-placed collection")
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
    ap.add_argument("--no-v42k7-inst", action="store_true",
                    help="skip v42k7 → rmb instance extraction")
    ap.add_argument("--no-v42k7-barriers", action="store_true",
                    help="skip barriers-only v42k7 filter pass")
    ap.add_argument("--v42k7-policy", choices=("freeroam", "all"),
                    default="freeroam",
                    help="v42k7 filter policy (default: freeroam)")
    ap.add_argument("--no-collobjs", action="store_true",
                    help="skip CollObjs.xml → rmb instance extraction")
    ap.add_argument("--ribbon-dir",
                    help="path to Ribbon_00/ for CollObjs (auto-detected if omitted)")
    ap.add_argument("--collobjs-policy", choices=("freeroam", "all"),
                    default="freeroam",
                    help="CollObjs filter policy (default: freeroam)")
    ap.add_argument("--collobjs-fuzzy", action="store_true",
                    help="enable CollObjs normalised-key fallback "
                         "(may place wrong meshes for bases missing from "
                         "the rmb pool)")
    ap.add_argument("--no-rmb-world", action="store_true",
                    help="skip rmb world-placed blob extraction")
    ap.add_argument("--rmb-world-policy", choices=("freeroam", "all"),
                    default="freeroam",
                    help="rmb-world filter policy (default: freeroam)")
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
