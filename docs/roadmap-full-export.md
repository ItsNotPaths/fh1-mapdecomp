# Roadmap — superseded by `mesh-export-state.md`

This doc described a 2026-04-pre roadmap that assumed only PGEO bbox
cubes were placed. The pipeline has moved well past that:

- All four major mesh layers are decoded and wired (terrain,
  rmb_world, v42k7_inst, collobjs_inst). See
  `mesh-export-state.md` for current geometry coverage.
- Landmark placement is solved via named-section refs (also in
  `mesh-export-state.md`).
- Textures / materials / lighting remain out of scope for the
  mesh-only export. Re-open this doc if/when that workstream
  starts.
