# Vendored NASA Harvest modules

These modules are copied **unmodified** from the upstream NASA Harvest
repositories so the prototype is reproducible offline and the algorithms are
auditable. They are not presented as our own work.

## crop_cycle_detection

- Upstream: https://github.com/nasaharvest/crop-cycle-detection
- Commit: `293f90658be64dd1f8fc0ca8d36106f70edfc8f2` (2026-08-26)
- Files: `crop_cycle.py`, `ndvi_preprocessing.py`
- Licence: Apache-2.0 (see `crop_cycle_detection/LICENSE`)
- Citation: Pelta, R. (2026). *Crop Cycle Detection*. NASA Harvest / Agmatix.

## crop_stage_detection

- Upstream: https://github.com/nasaharvest/crop-stage-detection
- Commit: `2005f53d53c0aa9c6240800db4afc514d8f8f13d` (2026-08-10)
- File: `crop_stage.py` (the GEE wrapper is intentionally not vendored)
- Licence: Apache-2.0 (see `crop_stage_detection/LICENSE`)
- Citation: Pelta, R. (2026). *Crop Stage Detection*. NASA Harvest / Agmatix.

## Why vendor instead of pip install

Neither package is published on PyPI as a stable, versioned dependency. The
upstream repositories are small, Apache-2.0, and their core algorithms have no
exotic dependencies. Vendoring avoids a runtime GitHub dependency and makes the
exact revision used by an analysis auditable. The wrappers in
`src/crop_detection/` are the only code that imports these modules.
