"""Extract free-roam prop placements from ``Ribbon_00/CollObjs.xml``.

`CollObjs.xml` is the authoritative placement source for collision-prop
street furniture in Forza Horizon 1 Colorado — signs, benches, fences,
barriers, picnic tables, mailboxes, bins, flower-holders, etc. Each
``<ObjN>`` entry carries:

    PhysicsType="CO_Bench_001.0.rmb"   -> rmb.bin blob name + LOD digit
    <Pos x=.. y=.. z=../>              -> world position (Y-up game coords)
    <Orientation>
        <XAxis ../>                    -> world-space local +X axis
        <YAxis ../>                    -> world-space local +Y axis
        <ZAxis ../>                    -> world-space local +Z axis
    </Orientation>

Output JSON schema mirrors ``v42k7_inst`` so the Blender importer path
can consume both without duplication:

    index.json   {blobs: [{handle, npz, tag, ...}],
                   chunks: [{filename, name, origin, sections: [
                        {handles: [H], instances: [{pos, rot}]}
                   ]}]}
    blobs/HHHHHH.npz   positions:(N,3) f32 game-space, faces:(M,3) u32

A single synthetic chunk ``"CollObjs"`` is emitted with one section per
unique handle, accumulating every placement of that tag.

Filtering: ``policy='freeroam'`` drops ``CO_FEST_*`` (festival dressing
tied to race events). ``policy='all'`` keeps every placement.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable, Optional
from xml.etree import ElementTree as ET

import numpy as np

from fh1_mapdecomp.binzip import Entry, list_entries, read_entry
from fh1_mapdecomp.rmb import parse_blob


ProgressFn = Optional[Callable[[int, int], None]]


# PhysicsType strings look like 'CO_Bench_001.0.rmb' — the '.N' before
# '.rmb' is the LOD digit. Some entries omit the trailing '.N' (e.g.
# 'CO_CLRD_Sign_Speed35.rmb' appears directly in the XML).
_PHYS_RE = re.compile(r"^(?P<base>.+?)(?:\.(?P<lod>\d+))?\.rmb$")


def _parse_physics_type(s: str) -> tuple[str, str]:
    """Return (base_name, lod_digits). LOD defaults to '00' when absent."""
    m = _PHYS_RE.match(s)
    if not m:
        return s, "00"
    return m.group("base"), (m.group("lod") or "0").zfill(2)


def _candidate_tags(base: str, lod: str) -> list[str]:
    """Tag variants to try when resolving PhysicsType → rmb blob tag.

    Several distinct naming conventions live in the rmb pool; empirically
    an XML ``PhysicsType="CO_Foo.N.rmb"`` can be stored with any of:

    * bare ``CO_Foo`` (a minority of signs)
    * ``CO_Foo_LOD00`` / ``CO_Foo_LODNN``
    * ``O_CO_Foo`` (an ``O_`` prefix added verbatim)
    * ``O_Foo`` (the leading ``C`` dropped; e.g. ``O_SignG_001``)
    * ``OBJ_Foo`` / ``OBJ_Foo_LOD00`` (``CO_`` → ``OBJ_`` swap,
      e.g. ``OBJ_CLRD_FenceF_LOD00``)

    Order matters — try the more-specific LOD-suffixed variants first so
    we pick the LOD requested by the XML over a stray bare-name match.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    # Canonical base + LOD suffixes
    add(f"{base}_LOD{lod}")
    if lod != "00":
        add(f"{base}_LOD00")
    add(base)
    # "O_" prefix variant
    add(f"O_{base}_LOD{lod}")
    add(f"O_{base}")
    # CO_*-specific rewrites
    if base.startswith("CO_"):
        stripped = base[1:]  # "O_Foo" — leading C dropped
        add(f"{stripped}_LOD{lod}")
        add(stripped)
        obj_form = "OBJ_" + base[3:]  # CO_ -> OBJ_
        add(f"{obj_form}_LOD{lod}")
        if lod != "00":
            add(f"{obj_form}_LOD00")
        add(obj_form)
    return out


_NORM_PREFIX_RE = re.compile(r"^(?:OBJ_|O_|CO_)+", re.IGNORECASE)
_NORM_LODSUFFIX_RE = re.compile(r"_LOD\d+_?$", re.IGNORECASE)
_NORM_LEADZERO_RE = re.compile(r"0+(\d)(?=\D*$)")
_NORM_NONALNUM_RE = re.compile(r"[^a-z0-9]")


def _norm_tag(s: str) -> str:
    """Collapse the rmb tag / XML PhysicsType base to a naming-convention-
    agnostic key.

    Strips the leading ``OBJ_``/``O_``/``CO_`` prefixes (the three forms
    used interchangeably in the rmb pool), strips a trailing ``_LODNN``
    suffix with optional trailing underscore, collapses leading zeros on
    any trailing numeric run (``_001`` ↔ ``_01``), lowercases, and drops
    all non-alphanumerics. Trailing *distinct* numeric suffixes (e.g.
    ``Gladstone_Brown2``) are preserved so near-siblings don't collide.
    """
    s = _NORM_PREFIX_RE.sub("", s)
    s = _NORM_LODSUFFIX_RE.sub("", s)
    s = _NORM_LEADZERO_RE.sub(r"\1", s)
    return _NORM_NONALNUM_RE.sub("", s.lower())


def _build_tag_index(
    zip_path: Path, progress: ProgressFn = None,
) -> tuple[dict[str, int], dict[str, list[tuple[str, int]]], list[tuple[Entry, int]]]:
    """Scan every rmb.bin entry once, returning:
        tag_to_handle: first-seen handle for each exact blob tag
        norm_to_tags: normalized-key → [(raw_tag, handle), ...] buckets
                      used by the fallback resolver when no exact / LOD
                      variant matches.
        rmb_list:     (entry, handle) pairs in pool order — handle is
                      the index of the rmb.bin entry in bin.zip order.
    """
    entries = list_entries(zip_path)
    rmb_list: list[tuple[Entry, int]] = []
    tag_to_handle: dict[str, int] = {}
    norm_to_tags: dict[str, list[tuple[str, int]]] = {}
    handle = 0
    for e in entries:
        if "rmb.bin" not in e.filename:
            continue
        rmb_list.append((e, handle))
        handle += 1
    total = len(rmb_list)
    for i, (e, h) in enumerate(rmb_list):
        if progress and i % 2000 == 0:
            progress(i, total)
        try:
            buf = read_entry(zip_path, e)
            blob = parse_blob(buf)
        except Exception:
            continue
        if blob is None:
            continue
        # First-seen wins: duplicate tags across LOD / case-variant
        # rmb.bin files all point at the same geometry, so the first
        # valid handle is as good as any.
        if tag_to_handle.setdefault(blob.tag, h) == h:
            norm_to_tags.setdefault(_norm_tag(blob.tag), []).append((blob.tag, h))
    if progress:
        progress(total, total)
    return tag_to_handle, norm_to_tags, rmb_list


def _resolve_normalized(
    base: str,
    lod: str,
    norm_to_tags: dict[str, list[tuple[str, int]]],
) -> Optional[int]:
    """Fallback resolver using the normalized-key index.

    Picks the candidate whose raw tag is closest to the requested LOD:
    prefer ``..._LOD{lod}``, then ``..._LOD00``, then a bare/other form.
    """
    hits = norm_to_tags.get(_norm_tag(base))
    if not hits:
        return None
    want_lod = f"_LOD{lod}"
    want_any = "_LOD"
    # Rank: requested LOD > LOD00 > no LOD suffix > any other LOD.
    def score(raw_tag: str) -> int:
        if want_lod.lower() in raw_tag.lower():
            return 0
        if "_LOD00" in raw_tag.upper():
            return 1
        if want_any not in raw_tag.upper():
            return 2
        return 3
    best = min(hits, key=lambda rh: (score(rh[0]), rh[0]))
    return best[1]


def _iter_xml_objects(xml_path: Path, type_attr: str) -> list[dict]:
    """Read every ``<ObjN type_attr="..." ...>`` element under the root.

    ``type_attr`` is ``'PhysicsType'`` for CollObjs.xml or ``'GameplayID'``
    for GameObjs.xml. Returns dicts ``{'type', 'pos', 'rot'}``.
    """
    out: list[dict] = []
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    for obj in root:
        ttype = obj.attrib.get(type_attr)
        if not ttype:
            continue
        pos_el = obj.find("Pos")
        ori = obj.find("Orientation")
        if pos_el is None or ori is None:
            continue
        pos = (
            float(pos_el.attrib["x"]),
            float(pos_el.attrib["y"]),
            float(pos_el.attrib["z"]),
        )
        xax = ori.find("XAxis")
        yax = ori.find("YAxis")
        zax = ori.find("ZAxis")
        if None in (xax, yax, zax):
            continue
        # The three axes are local +X/+Y/+Z expressed in world space —
        # i.e. the COLUMNS of the rotation matrix. Convert to row-major
        # rot[i][j] where rot @ local_v = world_v.
        X = (float(xax.attrib["x"]), float(xax.attrib["y"]), float(xax.attrib["z"]))
        Y = (float(yax.attrib["x"]), float(yax.attrib["y"]), float(yax.attrib["z"]))
        Z = (float(zax.attrib["x"]), float(zax.attrib["y"]), float(zax.attrib["z"]))
        rot = [
            [X[0], Y[0], Z[0]],
            [X[1], Y[1], Z[1]],
            [X[2], Y[2], Z[2]],
        ]
        out.append({"type": ttype, "pos": list(pos), "rot": rot})
    return out


def _freeroam_keep(base: str) -> bool:
    """Drop only explicit festival/race dressing. Everything else stays.

    ``base`` is the PhysicsType minus the ``.N.rmb`` tail, e.g.
    ``CO_FEST_EquipSign_001_P1_CONV`` or ``CO_Bench_001``.
    """
    # Festival dressing (race event only)
    if base.startswith(("CO_FEST_", "OBJ_FEST_")):
        return False
    return True


def extract_collobjs(
    zip_path: Path,
    ribbon_dir: Path,
    out_dir: Path,
    *,
    policy: str = "freeroam",
    include_all_lods: bool = False,
    fuzzy: bool = False,
    progress: ProgressFn = None,
) -> dict:
    """Parse CollObjs.xml, resolve names to rmb blobs, emit index.json
    + per-blob .npz files under ``out_dir``.

    ``ribbon_dir`` must point at the ``Ribbon_00/`` directory (containing
    ``CollObjs.xml``). ``zip_path`` must be the companion bin.zip.

    Set ``fuzzy=True`` to re-enable the normalised-key fallback (strip
    prefix + LOD + leading-zeros, then match). Off by default: the rmb
    pool is missing roughly a third of the PhysicsType bases CollObjs
    references, and fuzzy matching those bases against unrelated tags
    produces visibly-wrong meshes in the scene. Strict mode drops the
    unresolvable placements instead.
    """
    blob_dir = out_dir / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)

    xml_path = ribbon_dir / "CollObjs.xml"
    if not xml_path.exists():
        raise FileNotFoundError(f"CollObjs.xml not found in {ribbon_dir}")

    if progress:
        progress(0, 1)
    objs = _iter_xml_objects(xml_path, "PhysicsType")
    if progress:
        progress(1, 1)

    if progress:
        progress(0, 1)
    tag_to_handle, norm_to_tags, rmb_list = _build_tag_index(
        zip_path, progress=progress,
    )

    # Group placements by resolved handle
    per_handle: dict[int, list[dict]] = {}
    dropped_policy = 0
    unresolved: dict[str, int] = {}
    resolved_via_norm = 0
    for o in objs:
        base, lod = _parse_physics_type(o["type"])
        if policy == "freeroam" and not _freeroam_keep(base):
            dropped_policy += 1
            continue
        handle: Optional[int] = None
        for tag in _candidate_tags(base, lod):
            if tag in tag_to_handle:
                handle = tag_to_handle[tag]
                break
        if handle is None and fuzzy:
            handle = _resolve_normalized(base, lod, norm_to_tags)
            if handle is not None:
                resolved_via_norm += 1
        if handle is None:
            unresolved[base] = unresolved.get(base, 0) + 1
            continue
        per_handle.setdefault(handle, []).append({
            "pos": o["pos"], "rot": o["rot"],
        })

    # Emit per-handle blobs (positions + faces from rmb parse)
    blobs_meta: list[dict] = []
    blob_failed: list[tuple[int, str]] = []
    skipped_lod: set[int] = set()
    total_h = len(per_handle)
    for n, handle in enumerate(sorted(per_handle.keys())):
        if progress and n % 200 == 0:
            progress(n, total_h)
        entry, _ = rmb_list[handle]
        try:
            buf = read_entry(zip_path, entry)
            blob = parse_blob(buf)
            if blob is None:
                blob_failed.append((handle, "parse_blob returned None"))
                continue
            if not include_all_lods and blob.lod and blob.lod != "00":
                # Drop non-LOD0 placements; these are LOD tiers of the
                # same asset and would overlap the hero LOD0 placement.
                skipped_lod.add(handle)
                continue
            tris_list = [s.triangles for s in blob.sections if s.triangles.size]
            if tris_list:
                tris = np.concatenate(tris_list, axis=0).astype(np.uint32, copy=False)
            else:
                tris = np.zeros((0, 3), dtype=np.uint32)
            name = f"{handle:06d}.npz"
            np.savez(
                blob_dir / name,
                positions=blob.positions.astype(np.float32, copy=False),
                faces=tris,
            )
            blobs_meta.append({
                "handle": handle,
                "npz": f"blobs/{name}",
                "tag": blob.tag,
                "lod": blob.lod,
                "vcount": int(blob.vcount),
                "tri_count": int(tris.shape[0]),
            })
        except Exception as ex:
            blob_failed.append((handle, str(ex)))
    if progress:
        progress(total_h, total_h)

    # Drop instances whose blob was skipped (LOD filter) so the importer
    # doesn't need to guard against missing npz files.
    sections: list[dict] = []
    for handle in sorted(per_handle.keys()):
        if handle in skipped_lod:
            continue
        # Only emit sections whose blob made it to blobs_meta (avoid
        # orphaned placements pointing at failed parses).
        if not any(b["handle"] == handle for b in blobs_meta):
            continue
        sections.append({
            "handles": [handle],
            "instances": per_handle[handle],
        })

    chunks = [{
        "filename": str(xml_path.name),
        "name": "CollObjs",
        "origin": [0.0, 0.0, 0.0],
        "sections": sections,
    }] if sections else []

    doc = {
        "source": str(xml_path),
        "rmb_pool_size": len(rmb_list),
        "policy": policy,
        "fuzzy": fuzzy,
        "xml_object_count": len(objs),
        "dropped_policy": dropped_policy,
        "resolved_via_norm": resolved_via_norm,
        "unresolved_types": sorted(unresolved.items(), key=lambda kv: -kv[1])[:50],
        "unresolved_count": sum(unresolved.values()),
        "skipped_lod": sorted(skipped_lod),
        "blob_count": len(blobs_meta),
        "blob_failed": blob_failed,
        "blobs": blobs_meta,
        "chunks": chunks,
    }
    (out_dir / "index.json").write_text(json.dumps(doc))
    return doc


def summarise(doc: dict, stream=sys.stderr) -> None:
    kept = sum(len(s["instances"]) for c in doc["chunks"] for s in c["sections"])
    print(
        f"collobjs: {doc['xml_object_count']} xml entries, "
        f"{kept} kept instances ({doc['dropped_policy']} dropped by policy, "
        f"{doc['unresolved_count']} unresolved, "
        f"{doc.get('resolved_via_norm', 0)} via normalised fallback), "
        f"{doc['blob_count']} unique blobs",
        file=stream,
    )
    if doc["unresolved_count"] and doc.get("unresolved_types"):
        print("  top unresolved PhysicsType bases:", file=stream)
        for base, n in doc["unresolved_types"][:5]:
            print(f"    {n:5d} {base}", file=stream)
