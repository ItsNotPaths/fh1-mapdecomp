"""Headless top-down render of a loaded .blend for a quick visual check."""
import bpy
import sys

argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
in_blend, out_png = argv[0], argv[1]
bpy.ops.wm.open_mainfile(filepath=in_blend)

minv = [ 1e9,  1e9,  1e9]
maxv = [-1e9, -1e9, -1e9]
for o in bpy.data.objects:
    if o.type == "MESH" and o.data and len(o.data.vertices):
        # Real meshes: use the world-space bounding box of the geometry.
        corners = [o.matrix_world @ v.co for v in o.data.vertices]
        for c in corners:
            for i in range(3):
                minv[i] = min(minv[i], c[i])
                maxv[i] = max(maxv[i], c[i])
        continue
    if o.type == "EMPTY":
        s = o.empty_display_size
    else:
        s = max(o.scale) / 2
    for i in range(3):
        minv[i] = min(minv[i], o.location[i] - s)
        maxv[i] = max(maxv[i], o.location[i] + s)
cx = (minv[0]+maxv[0])/2
cy = (minv[1]+maxv[1])/2
sx = (maxv[0]-minv[0])
sy = (maxv[1]-minv[1])
print(f"scene extents x=[{minv[0]:.0f},{maxv[0]:.0f}] y=[{minv[1]:.0f},{maxv[1]:.0f}] z=[{minv[2]:.0f},{maxv[2]:.0f}]")

bpy.ops.object.camera_add(location=(cx, cy, max(maxv[2]+1000, 2000)), rotation=(0, 0, 0))
cam = bpy.context.active_object
cam.data.type = "ORTHO"
cam.data.ortho_scale = max(sx, sy) * 1.1
cam.data.clip_start = 0.1
cam.data.clip_end = 100000.0
bpy.context.scene.camera = cam

scn = bpy.context.scene
scn.render.engine = "BLENDER_WORKBENCH"
scn.render.resolution_x = 1600
scn.render.resolution_y = 1600
scn.render.filepath = out_png
scn.display.shading.color_type = "MATERIAL"
scn.display.shading.light = "FLAT"
bpy.ops.render.render(write_still=True)
print(f"saved {out_png}")
