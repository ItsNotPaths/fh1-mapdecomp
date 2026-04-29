"""Shared types for per-variant PGEO body decoders.

A decoded mesh is intentionally a small flat bag of numpy arrays rather than
a tree of objects: it serialises to ``.npz`` cheaply, loads into Blender with
``mesh.from_pydata`` directly, and is easy to reason about in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class MeshData:
    # (N, 3) float32 — world-space coordinates, game axes (Y-up).
    # Blender-side scripts handle the Y-up → Z-up conversion.
    positions: np.ndarray

    # (M, 3) uint32 — triangle indices into ``positions``.
    faces: np.ndarray

    # (N, 2) float32 — per-vertex UVs. Empty array if unknown.
    uvs: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), dtype=np.float32))

    # (N, 3) float32 — per-vertex normals. Empty array if unknown (Blender auto-computes).
    normals: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.float32))

    # (K,) uint32 — material hashes referenced by faces. Resolution is a
    # later workstream (textures/bundles); left opaque here.
    material_refs: np.ndarray = field(default_factory=lambda: np.zeros((0,), dtype=np.uint32))


def save(mesh: MeshData, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        positions=mesh.positions.astype(np.float32, copy=False),
        faces=mesh.faces.astype(np.uint32, copy=False),
        uvs=mesh.uvs.astype(np.float32, copy=False),
        normals=mesh.normals.astype(np.float32, copy=False),
        material_refs=mesh.material_refs.astype(np.uint32, copy=False),
    )


def load(path: Path) -> Optional[MeshData]:
    if not path.exists():
        return None
    with np.load(path) as z:
        return MeshData(
            positions=z["positions"],
            faces=z["faces"],
            uvs=z["uvs"],
            normals=z["normals"],
            material_refs=z["material_refs"],
        )
