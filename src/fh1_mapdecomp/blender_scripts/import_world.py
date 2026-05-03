"""Build a Blender scene from a ribbon_NN.json placement file.

For each PGEO chunk:
  * If the chunk's JSON entry has a ``mesh`` field and the corresponding
    ``.npz`` exists under ``--meshes``, load the decoded mesh and build real
    geometry (``bpy.data.meshes.new`` + ``from_pydata`` + loop UVs).
  * Otherwise fall back to a bbox-sized cube, per-variant colour (the
    original MVP behaviour).

Per-variant collections stay intact so the Outliner can toggle visibility
per layer. Terrain tiles use the wild y-placeholder (+/-100000); when we
only have a bbox cube for terrain, we clamp to y=[0,5] for visualization.

Run:
  blender --background --python <this> -- \\
      --json out/world/ribbon_00.json \\
      --out  out/blender/colorado.blend \\
      [--meshes out/meshes] \\
      [--limit 5000] [--variants terrain,grass,...] [--no-cubes]

``--no-cubes`` uses empties instead of cube meshes for the fallback path;
faster for the full 41k set when no decoded meshes exist.
"""
import bpy
import bmesh
import json
import sys
from pathlib import Path

import numpy as np


def parse_args():
    import argparse
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meshes", default="", help="directory with decoded per-chunk .npz meshes")
    ap.add_argument("--terrain-hi", default="",
                    help="directory with rmb.bin TERR per-tile .npz + index.json")
    ap.add_argument("--collobjs", default="",
                    help="directory with CollObjs.xml instance per-blob .npz + index.json")
    ap.add_argument("--pvs-inst", default="",
                    help="directory with PVS+PVSZ instance per-blob .npz + index.json "
                         "(authored placements — the engine's own table)")
    ap.add_argument("--crowd-inst", default="",
                    help="directory with inanimate-crowd PGEO instance per-blob "
                         ".npz + index.json (festival barriers, stalls, stages, "
                         "grandstands)")
    ap.add_argument("--limit", type=int, default=0, help="cap chunks (0=all)")
    ap.add_argument("--variants", default="", help="comma-separated variant filter")
    ap.add_argument("--no-cubes", action="store_true", help="use empties instead of bbox-cube meshes")
    ap.add_argument("--keep-bbox-cubes", action="store_true",
                    help="emit bbox cubes for grass/vegetation/crowd/landmark_anim "
                         "PGEO chunks (off by default — PVS covers their placements)")
    return ap.parse_args(argv)


VARIANT_COLORS = {
    "terrain":       (0.80, 0.55, 0.30, 1.0),
    "terrain_hi":    (0.65, 0.45, 0.25, 1.0),
    "grass":         (0.30, 0.70, 0.20, 1.0),
    "vegetation":    (0.15, 0.55, 0.45, 1.0),
    "crowd":         (0.85, 0.25, 0.25, 1.0),
    "crowd_inst":    (0.95, 0.45, 0.20, 1.0),
    "collobjs":      (0.90, 0.75, 0.35, 1.0),
    "pvs_inst":      (0.85, 0.55, 0.85, 1.0),
    "landmark_anim": (1.00, 0.85, 0.15, 1.0),
}


def clean_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for col in (bpy.data.meshes, bpy.data.materials, bpy.data.collections):
        for x in list(col):
            try: col.remove(x)
            except Exception: pass


def get_or_make_material(variant: str):
    name = f"mat_{variant}"
    m = bpy.data.materials.get(name)
    if m: return m
    m = bpy.data.materials.new(name)
    m.diffuse_color = VARIANT_COLORS.get(variant, (0.7, 0.7, 0.7, 1.0))
    m.use_nodes = False
    return m


def get_or_make_collection(name: str):
    c = bpy.data.collections.get(name)
    if c: return c
    c = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(c)
    return c


def make_cube(name, bb_min, bb_max, variant):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    sx = max(bb_max[0] - bb_min[0], 0.1)
    sy = max(bb_max[1] - bb_min[1], 0.1)
    sz = max(bb_max[2] - bb_min[2], 0.1)
    obj.scale = (sx, sy, sz)
    obj.location = ((bb_max[0]+bb_min[0])/2, (bb_max[1]+bb_min[1])/2, (bb_max[2]+bb_min[2])/2)
    mat = get_or_make_material(variant)
    mesh.materials.append(mat)
    return obj


def make_empty(name, bb_min, bb_max, variant):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "CUBE"
    sx = max(bb_max[0] - bb_min[0], 0.1)
    sy = max(bb_max[1] - bb_min[1], 0.1)
    sz = max(bb_max[2] - bb_min[2], 0.1)
    obj.empty_display_size = max(sx, sy, sz) * 0.5
    obj.location = ((bb_max[0]+bb_min[0])/2, (bb_max[1]+bb_min[1])/2, (bb_max[2]+bb_min[2])/2)
    return obj


def make_mesh_from_npz(name, npz_path, variant):
    """Build real geometry from a decoded PGEO body cache.

    Input positions are in game-space (Y-up); converted to Blender Z-up via
    ``(x, y, z) -> (x, -z, y)`` here so the rest of the scene placement maths
    stays identical. Returns ``None`` for face-less caches (e.g. v42k7 point
    clouds): those variants are staged for a later decode pass and the caller
    skips them entirely rather than emitting a useless dot cloud.
    """
    import numpy as np
    with np.load(str(npz_path)) as z:
        positions = z["positions"]
        faces = z["faces"]
        uvs = z["uvs"]
    if len(faces) == 0:
        return None
    verts = [(float(p[0]), float(-p[2]), float(p[1])) for p in positions]
    tris = [(int(f[0]), int(f[1]), int(f[2])) for f in faces]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], tris)
    mesh.update(calc_edges=True)
    if uvs.shape[0] == len(verts):
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for poly in mesh.polygons:
            for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
                vi = mesh.loops[li].vertex_index
                u, v = uvs[vi]
                uv_layer.data[li].uv = (float(u), float(v))
    obj = bpy.data.objects.new(name, mesh)
    mat = get_or_make_material(variant)
    mesh.materials.append(mat)
    return obj


def _make_v42k7_instance_gn(name, src_obj):
    """Build a Geometry Nodes group that instances ``src_obj`` at every
    point of the input, reading per-point ``rot_euler`` (FLOAT_VECTOR)
    and ``inst_scale`` (FLOAT) attributes for rotation and per-instance
    scale.
    """
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    if hasattr(ng, "interface"):
        ng.interface.new_socket("Geometry", in_out="INPUT",  socket_type="NodeSocketGeometry")
        ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    else:  # Blender <4.0
        ng.inputs.new("NodeSocketGeometry",  "Geometry")
        ng.outputs.new("NodeSocketGeometry", "Geometry")

    n_in  = ng.nodes.new("NodeGroupInput")
    n_out = ng.nodes.new("NodeGroupOutput")
    n_obj = ng.nodes.new("GeometryNodeObjectInfo")
    n_obj.inputs["Object"].default_value = src_obj
    n_attr_rot = ng.nodes.new("GeometryNodeInputNamedAttribute")
    n_attr_rot.data_type = "FLOAT_VECTOR"
    n_attr_rot.inputs["Name"].default_value = "rot_euler"
    n_attr_scale = ng.nodes.new("GeometryNodeInputNamedAttribute")
    n_attr_scale.data_type = "FLOAT"
    n_attr_scale.inputs["Name"].default_value = "inst_scale"
    # Splat scalar to vec3 for the Scale socket.
    n_combine = ng.nodes.new("ShaderNodeCombineXYZ")
    n_inst = ng.nodes.new("GeometryNodeInstanceOnPoints")

    n_in.location  = (-700, 0)
    n_obj.location = (-500, -200)
    n_attr_rot.location = (-500, -380)
    n_attr_scale.location = (-500, -560)
    n_combine.location = (-260, -560)
    n_inst.location = (0, 0)
    n_out.location = (300, 0)

    ng.links.new(n_in.outputs["Geometry"],       n_inst.inputs["Points"])
    ng.links.new(n_obj.outputs["Geometry"],      n_inst.inputs["Instance"])
    ng.links.new(n_attr_rot.outputs["Attribute"], n_inst.inputs["Rotation"])
    ng.links.new(n_attr_scale.outputs["Attribute"], n_combine.inputs["X"])
    ng.links.new(n_attr_scale.outputs["Attribute"], n_combine.inputs["Y"])
    ng.links.new(n_attr_scale.outputs["Attribute"], n_combine.inputs["Z"])
    ng.links.new(n_combine.outputs["Vector"],    n_inst.inputs["Scale"])
    ng.links.new(n_inst.outputs["Instances"],    n_out.inputs["Geometry"])
    return ng


def make_v42k7_inst_mesh_data(name, npz_path):
    """Build a Blender mesh data block from a v42k7 instance .npz.

    Vertices are local-space (or world-space for world-authored rmbs)
    game coords (Y-up); converted to Blender Z-up via the vertex basis
    ``(x, y, z) -> (x, -z, y)``. PVS placement translations use a
    different basis in ``_import_inst_dir`` because the engine's
    placement convention differs from its vertex convention. Returns
    ``None`` for face-less blobs.
    """
    import numpy as np
    with np.load(str(npz_path)) as z:
        positions = z["positions"]
        faces = z["faces"]
    if len(faces) == 0 or len(positions) == 0:
        return None
    verts = [(float(p[0]), float(-p[2]), float(p[1])) for p in positions]
    tris = [(int(f[0]), int(f[1]), int(f[2])) for f in faces]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], tris)
    mesh.update(calc_edges=True)
    return mesh


def make_terrain_hi_mesh(name, npz_path, variant):
    """Triangulate a TERR point cloud via XZ-plane Delaunay, preserve Y as height.

    The rmb.bin TERR blob is a vertex-only cloud — the companion index buffer
    isn't decoded yet. Projecting onto XZ and running Delaunay builds a
    reasonable surface because terrain in this game is single-valued in Y;
    we can swap it out once the real triangle-strip order is recovered.
    """
    import numpy as np
    from mathutils import Vector
    from mathutils.geometry import delaunay_2d_cdt

    with np.load(str(npz_path)) as z:
        positions = z["positions"]

    # delaunay_2d_cdt chokes on coincident points — dedup on (x,z) keeping
    # the first-seen Y.
    key = np.ascontiguousarray(positions[:, [0, 2]])
    _, first = np.unique(key.view([("", key.dtype)] * 2), return_index=True)
    first.sort()
    pts = positions[first]

    verts2d = [Vector((float(p[0]), float(p[2]))) for p in pts]
    verts_out, _, faces_out, orig_verts, _, _ = delaunay_2d_cdt(
        verts2d, [], [], 1, 1e-4, True,
    )

    # output_type=1 returns the input vertices after dedup; orig_verts[i] is
    # the list of input indices that collapsed to output vertex i.
    ys = np.array(
        [pts[ov[0], 1] if ov else 0.0 for ov in orig_verts], dtype=np.float32,
    )
    verts_bl = [
        (float(v.x), -float(v.y), float(ys[i]))  # game (x,y,z) -> blender (x,-z,y)
        for i, v in enumerate(verts_out)
    ]
    tris = [tuple(int(i) for i in f) for f in faces_out if len(f) == 3]

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts_bl, [], tris)
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new(name, mesh)
    mat = get_or_make_material(variant)
    mesh.materials.append(mat)
    return obj


def clamp_terrain_bbox(variant, bb_min, bb_max):
    # Terrain tiles use +/-100000 as a y-placeholder; clamp for visualization.
    if variant == "terrain":
        return [bb_min[0], 0.0, bb_min[2]], [bb_max[0], 5.0, bb_max[2]]
    return bb_min, bb_max


def game_to_blender(bb_min, bb_max):
    # Forza is Y-up; Blender is Z-up. Swap Y/Z on axes + flip Z to keep handedness.
    return (
        [ bb_min[0], -bb_max[2], bb_min[1]],
        [ bb_max[0], -bb_min[2], bb_max[1]],
    )


def _import_inst_dir(
    vi_dir,
    *,
    label: str,
    collection: str,
    material_variant: str,
    src_prefix: str,
    inst_prefix: str,
    blob_prefix: str,
) -> None:
    """Load a (blobs + chunks-of-sections-of-instances) directory into the scene.

    Shared path for v42k7_inst and CollObjs: both emit the same JSON
    schema and the same per-blob ``.npz`` layout, so one GN-instance
    loader handles both.
    """
    import numpy as np
    from mathutils import Matrix

    vi_index = vi_dir / "index.json"
    if not vi_index.exists():
        print(f"[import_world] {label}: no index.json at {vi_index}", file=sys.stderr)
        return
    vi_doc = json.loads(vi_index.read_text())
    blobs = {b["handle"]: b for b in vi_doc["blobs"]}
    chunks_v = vi_doc["chunks"]
    print(
        f"[import_world] {label}: {len(blobs)} blobs, {len(chunks_v)} chunks",
        file=sys.stderr,
    )
    # Engine encodes PVS pos.Z with the opposite sign convention from
    # rmb vertex Z (verified by user-supplied coords: models at PVS
    # Z=-204 align with world-authored mesh at vertex Z=+211 → engine
    # applies a Z-negate when placing). A second uniform negate-Y
    # mirror across Blender's X axis matches the user's expected
    # orientation for the whole scene.
    #
    #   B_POS  = [[1,0,0],[0,0, 1],[0,1,0]]  → (X, Y, Z) -> (X,  Z, Y)
    #   B_VERT = [[1,0,0],[0,0,-1],[0,1,0]]  → (X, Y, Z) -> (X, -Z, Y)
    B_POS = np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]], dtype=np.float32)
    B = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.float32)
    B_T = B.T

    per_handle_pos: dict = {}
    per_handle_rot: dict = {}
    per_handle_scale: dict = {}
    for chunk in chunks_v:
        for sec in chunk["sections"]:
            insts = sec["instances"]
            if not insts:
                continue
            for h in sec["handles"]:
                if h not in blobs:
                    continue
                p = per_handle_pos.setdefault(h, [])
                r = per_handle_rot.setdefault(h, [])
                s = per_handle_scale.setdefault(h, [])
                for inst in insts:
                    p.append(inst["pos"])
                    r.append(inst["rot"])
                    s.append(float(inst.get("scale", 1.0)))

    col = get_or_make_collection(collection)
    mat = get_or_make_material(material_variant)
    inst_total = 0
    blobs_built = 0
    for hi, (h, positions_py) in enumerate(per_handle_pos.items()):
        if hi % 20 == 0:
            print(
                f"  {label} blob {hi}/{len(per_handle_pos)} "
                f"(instances so far: {inst_total})",
                file=sys.stderr,
            )
        blob = blobs[h]
        npz = vi_dir / blob["npz"]
        if not npz.exists():
            continue
        try:
            src_mesh = make_v42k7_inst_mesh_data(
                f"{blob_prefix}_{h:06d}", npz,
            )
        except Exception as ex:
            print(f"    blob {h} failed: {ex}", file=sys.stderr)
            continue
        if src_mesh is None:
            continue
        src_mesh.materials.append(mat)
        src_obj = bpy.data.objects.new(
            f"{src_prefix}/{h:06d}_{blob['tag']}", src_mesh,
        )
        src_obj.hide_viewport = True
        src_obj.hide_render = True
        col.objects.link(src_obj)

        positions = np.asarray(positions_py, dtype=np.float32)
        rotations = np.asarray(per_handle_rot[h], dtype=np.float32)
        positions_bl = positions @ B_POS.T
        rot_bl = np.einsum("ij,kjl,lm->kim", B, rotations, B_T)

        pc_mesh = bpy.data.meshes.new(f"{label}_pc_{h:06d}")
        pc_mesh.vertices.add(len(positions_bl))
        pc_mesh.vertices.foreach_set(
            "co", positions_bl.reshape(-1).astype(np.float32),
        )
        pc_mesh.update()

        eulers = np.empty((len(rot_bl), 3), dtype=np.float32)
        for k in range(len(rot_bl)):
            M = Matrix(tuple(tuple(float(v) for v in row) for row in rot_bl[k]))
            e = M.to_euler("XYZ")
            # Negate Z (yaw): the X-axis mirror baked into B_POS reflects
            # a coordinate axis, which flips the rotation direction
            # around the vertical axis. X/Y rotations look correct
            # because almost all PVS instances are pure-yaw (no pitch
            # or roll) so the same flip on those isn't visible.
            eulers[k] = (e[0], e[1], -e[2])
        attr_rot = pc_mesh.attributes.new(
            name="rot_euler", type="FLOAT_VECTOR", domain="POINT",
        )
        attr_rot.data.foreach_set("vector", eulers.reshape(-1))

        scales = np.asarray(per_handle_scale.get(h, [1.0] * len(positions_bl)),
                            dtype=np.float32)
        attr_scale = pc_mesh.attributes.new(
            name="inst_scale", type="FLOAT", domain="POINT",
        )
        attr_scale.data.foreach_set("value", scales)

        pc_obj = bpy.data.objects.new(
            f"{inst_prefix}/{h:06d}_{blob['tag']}", pc_mesh,
        )
        col.objects.link(pc_obj)

        node_group = _make_v42k7_instance_gn(
            f"gn_{label}_{h:06d}", src_obj,
        )
        mod = pc_obj.modifiers.new(name=label, type="NODES")
        mod.node_group = node_group

        blobs_built += 1
        inst_total += len(positions_bl)
    print(
        f"[import_world] {label}: {blobs_built} unique meshes, "
        f"{inst_total} instances (via Geometry Nodes)",
        file=sys.stderr,
    )


def _build_mesh_from_arrays(name, verts_xyz, tris, mat):
    """Build a bpy.data.meshes object from numpy ``(N,3) f32`` and
    ``(M,3) i32`` arrays via the foreach_set buffer API.

    ~5-10× faster than ``mesh.from_pydata`` once the mesh count climbs
    into the tens of thousands: ``foreach_set`` does a single contiguous
    memcpy instead of iterating over Python tuples and re-allocating
    Blender's internal MLoop / MPoly arrays for each vertex.
    """
    n_v = int(verts_xyz.shape[0])
    n_t = int(tris.shape[0])
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(n_v)
    mesh.vertices.foreach_set("co", verts_xyz.reshape(-1))
    mesh.loops.add(n_t * 3)
    mesh.loops.foreach_set("vertex_index", tris.reshape(-1))
    mesh.polygons.add(n_t)
    mesh.polygons.foreach_set("loop_start",
                              (np.arange(n_t, dtype=np.int32) * 3))
    mesh.polygons.foreach_set("loop_total",
                              np.full(n_t, 3, dtype=np.int32))
    mesh.update(calc_edges=True)
    mesh.materials.append(mat)
    return mesh


def main():
    args = parse_args()
    data = json.loads(Path(args.json).read_text())
    chunks = data["chunks"]
    if args.variants:
        allowed = set(args.variants.split(","))
        chunks = [c for c in chunks if c["variant"] in allowed]
    # The PGEO placement JSON is mostly bbox-cube scaffolding now —
    # PVS (`fh1_pvs_inst`), rmb_world, terrain_hi, and the v42k7_inst /
    # collobjs collections all carry real geometry. The variants below
    # have no decoded body in this build (or are owned by another
    # pipeline), so emitting their bboxes just adds green/blue/orange
    # cube clouds. Pass `--keep-bbox-cubes` to opt them back in for
    # diagnostic comparison.
    BBOX_NOISE_VARIANTS = (
        "terrain", "terrain_hi",     # owned by terrain_hi (real meshes)
        "grass", "vegetation",       # PGEO bodies undecoded — placeholder
                                     # for the future vegetation extractor
        "crowd",                     # crowd PGEO undecoded
        "v42k7", "v44k5",            # legacy runtime instancing chunks;
                                     # PVS covers their placement
        "landmark_anim",             # 447 small ride PGEOs; PVS covers placement
    )
    if not getattr(args, "keep_bbox_cubes", False):
        chunks = [c for c in chunks if c["variant"] not in BBOX_NOISE_VARIANTS]
    if args.limit > 0:
        chunks = chunks[:args.limit]

    meshes_dir = Path(args.meshes) if args.meshes else None
    mesh_hits = 0
    skipped = 0
    print(f"[import_world] {len(chunks)} chunks", file=sys.stderr)

    clean_scene()

    # Mute the depsgraph during bulk import — without this, every
    # ``objects.link`` triggers a full graph rebuild and the per-mesh
    # cost climbs from ~µs to >1 ms once the scene has tens of thousands
    # of objects. We restore the dummy override at the end of main().
    try:
        _orig_dg_update_pre = bpy.app.handlers.depsgraph_update_pre[:]
        _orig_dg_update_post = bpy.app.handlers.depsgraph_update_post[:]
        bpy.app.handlers.depsgraph_update_pre.clear()
        bpy.app.handlers.depsgraph_update_post.clear()
    except Exception:
        _orig_dg_update_pre = _orig_dg_update_post = None

    collections = {}
    for v in set(c["variant"] for c in chunks):
        collections[v] = get_or_make_collection(f"fh1_{v}")

    fallback_fn = make_empty if args.no_cubes else make_cube
    for i, c in enumerate(chunks):
        if i % 2000 == 0:
            print(f"  {i}/{len(chunks)}  (meshes so far: {mesh_hits})", file=sys.stderr)
        v = c["variant"]
        name = f"{v}/{c['filename'][:-5]}"
        mesh_name = c.get("mesh")
        obj = None
        skip_chunk = False
        if mesh_name and meshes_dir is not None:
            npz = meshes_dir / mesh_name
            if npz.exists():
                try:
                    obj = make_mesh_from_npz(name, npz, v)
                    if obj is not None:
                        mesh_hits += 1
                    else:
                        # Cache exists but has no faces yet (e.g. v42k7 point
                        # cloud). Drop the chunk — no bbox-cube fallback.
                        skip_chunk = True
                except Exception as ex:
                    print(f"    mesh load failed for {name}: {ex}", file=sys.stderr)
                    obj = None
        if skip_chunk or obj is None:
            skipped += 1
            continue
        collections[v].objects.link(obj)

    # rmb.bin TERR high-detail pass — independent of the PGEO placement JSON.
    # TEMP: terrain import disabled alongside the terrain bbox-cube path
    # while v42k7 placement is being debugged.
    if args.terrain_hi and False:
        th_dir = Path(args.terrain_hi)
        th_index = th_dir / "index.json"
        if th_index.exists():
            th_doc = json.loads(th_index.read_text())
            tiles = th_doc["tiles"]
            print(f"[import_world] loading {len(tiles)} TERR hi-detail tiles", file=sys.stderr)
            th_col = get_or_make_collection("fh1_terrain_hi")
            th_hits = 0
            for i, t in enumerate(tiles):
                if i % 500 == 0:
                    print(f"  terrain_hi {i}/{len(tiles)}", file=sys.stderr)
                npz = th_dir / t["npz"]
                if not npz.exists():
                    continue
                try:
                    obj = make_terrain_hi_mesh(
                        f"terrain_hi/{t['tag']}", npz, "terrain_hi",
                    )
                    th_col.objects.link(obj)
                    th_hits += 1
                except Exception as ex:
                    print(f"    terrain_hi tile {t['tag']} failed: {ex}", file=sys.stderr)
            print(f"[import_world] terrain_hi: {th_hits}/{len(tiles)} tiles meshed", file=sys.stderr)

    # PVS = authoritative authored-placement table (most freeroam
    # geometry). CollObjs.xml = static-collision-prop placement table
    # — complementary to PVS (e.g. OBJ_BarrierBrand has PVS pos=(0,0,0)
    # and the real per-barrier transforms live in CollObjs).
    if args.pvs_inst:
        _import_inst_dir(Path(args.pvs_inst),
                         label="pvs_inst",
                         collection="fh1_pvs_inst",
                         material_variant="pvs_inst",
                         src_prefix="pvs_src",
                         inst_prefix="pvs_inst",
                         blob_prefix="pvs_blob")
    if args.collobjs:
        _import_inst_dir(Path(args.collobjs),
                         label="collobjs",
                         collection="fh1_collobjs",
                         material_variant="collobjs",
                         src_prefix="collobjs_src",
                         inst_prefix="collobjs",
                         blob_prefix="collobjs_blob")
    if args.crowd_inst:
        _import_inst_dir(Path(args.crowd_inst),
                         label="crowd_inst",
                         collection="fh1_crowd_inst",
                         material_variant="crowd_inst",
                         src_prefix="crowd_src",
                         inst_prefix="crowd_inst",
                         blob_prefix="crowd_blob")

    # Restore depsgraph handlers and trigger a single update so the
    # scene is consistent before we save / render.
    if _orig_dg_update_pre is not None:
        bpy.app.handlers.depsgraph_update_pre[:] = _orig_dg_update_pre
    if _orig_dg_update_post is not None:
        bpy.app.handlers.depsgraph_update_post[:] = _orig_dg_update_post
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

    bpy.ops.object.camera_add(location=(0, -10000, 5000), rotation=(1.1, 0, 0))
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 2000))
    bpy.context.scene.camera = bpy.context.active_object

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out_path))
    print(
        f"[import_world] saved {out_path} "
        f"({mesh_hits} real meshes, {len(chunks)-mesh_hits-skipped} cubes, "
        f"{skipped} skipped)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
