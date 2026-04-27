"""Blender-side scripts executed under ``blender --background --python``.

These modules import ``bpy`` and therefore cannot be imported by the host
CLI directly — they are located as file paths and run as subprocesses by
``fh1_mapdecomp.blender_run``.
"""
