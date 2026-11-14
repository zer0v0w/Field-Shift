# Agri-Rotation Intelligence

A CLI-only Python research prototype that reconstructs a field's vegetation
history from public Earth observations, detects candidate growing cycles and
low-NDVI (bare-soil / fallow) windows, attaches weather, soil and terrain
context, and compares three crop rotations with transparent, reproducible
heuristics.

It is **decision-support research**, not farming advice, and it does not predict
yield, profit, or environmental outcomes.

---

## What it does

Given a point + buffer, a bounding box, or a GeoJSON polygon, and a date range:

1. Retrieves satellite observations and computes NDVI with cloud/shadow masks
   and quality flags.
2. Builds a daily smoothed series while keeping measured and interpolated rows
   separate.
3. Detects candidate seasons with the vendored NASA Harvest
   `crop-cycle-detection` model and generic A–E crop stages with
   `crop-stage-detection`.
4. Flags low-NDVI windows as **candidate** bare-soil / stubble / fallow periods.
5. Attaches regional weather (NASA POWER), predicted soil (SoilGrids) and
   terrain (SRTM), with explicit resolution and representativeness notes.
6. Scores three rotations across seven criteria, with weights, confidence,
   benefits, risks, trade-offs and missing information.
7. Writes machine-readable outputs, plots, a Markdown report, and a manifest
   with SHA-256 hashes.

## What it does **not** do

- Identify the crop species from NDVI alone.
- Predict yield, profit, soil-carbon change, erosion, or emissions.
- Replace a soil test, rain gauge, or farm management records.
- Model pests, diseases, salinity, labour, machinery, irrigation efficiency,
  water rights, or local prices.
- Present synthetic data as a real analysis. `--mock` is loudly labelled
  everywhere.

---

## Setup

Requires **Python 3.11+** (tested on 3.14).

```bash
cd agri-intelligence
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

No credentials are needed for the default path. Optional credentialed probes
(SMAP, IMERG, ERA5) only report availability; they never fabricate a fallback.

## Quick start (synthetic, no network)

```bash
python main.py analyze \
  --point 32.24,35.30 \
  --start 2022-01-01 --end 2024-12-31 \
  --mock \
  --output-dir results/mock_demo
```

This exercises the full pipeline. Every output is marked synthetic.

## Real analysis (no credentials)

```bash
python main.py analyze \
  --point 32.24,35.30 \
  --start 2023-01-01 --end 2024-12-31 \
  --source sentinel-2-l2a \
  --output-dir results/real_sentinel2
```

`32.24,35.30` is an example agricultural location near Nablus; it is not a claim
about a specific farmer's field. For a real field, pass a polygon:

```bash
python main.py analyze \
  --geojson field.geojson \
  --start 2023-01-01 --end 2024-12-31 \
  --output-dir results/my_field
```

Or a bounding box (`min_lon,min_lat,max_lon,max_lat`):

```bash
python main.py analyze \
  --bbox 35.29,32.23,35.31,32.25 \
  --start 2023-01-01 --end 2024-12-31
```

## CLI reference

| Command | Purpose |
|---|---|
| `analyze` | Full field analysis (point/bbox/GeoJSON). |
| `sources` | List configured Earth-observation sources and NDVI adapters. |
| `rotations` | Show the three rotations and, with `--crops`, crop attribute ranges. |
| `report --output-dir DIR` | Rebuild `report.md` from `report_context.json`. |
| `report --from-config DIR/run_config.json` | Re-run an analysis from saved parameters (same output dir unless `--output-dir` is given). |
| `cropharvest-labels` | Download/inspect nearby CropHarvest labels (optional context). |

Useful `analyze` flags:

```text
--source sentinel-2-l2a | modis-13Q1-061 | landsat-c2-l2 | mock
--buffer-m 100
--cloud-cover 60
--min-valid-fraction 0.20
--min-valid-pixels N        # default: S2 5, MODIS 1, Landsat 3
--max-scenes N              # default: S2 400, MODIS 60, Landsat 120
--priorities "water=2,soil=1,resilience=1"
--baseline existing
--no-environment
--cropharvest --cropharvest-radius-km 25
```

Unknown priority names are rejected rather than silently ignored.

## Outputs

Each run writes to `results/<run-id>/` (or `--output-dir`):

| File | Contents |
|---|---|
| `field_summary.json` | Run status, field geometry, source statuses, rotation ranking. |
| `vegetation_timeseries.csv` | Observed NDVI rows plus separately flagged daily smoothed rows. |
| `crop_cycles.json` | Candidate seasons, generic stages, low-NDVI windows, method metadata. |
| `environmental_summary.json` | Weather, soil, terrain, completeness, caveats. |
| `rotation_comparison.csv` | One row per rotation with criterion scores and explanations. |
| `rotation_analysis.json` | Full evaluations: criteria, benefits, risks, trade-offs, missing info. |
| `report.md` | Human-readable report generated from the structured outputs. |
| `ndvi_timeseries.png` | Observations, smoothed NDVI, seasons, low-NDVI windows. |
| `rotation_comparison.png` | Stacked weighted-criterion comparison. |
| `run_config.json` | Exact request parameters and output directory for reproducibility. |
| `report_context.json` | Structured inputs for regenerating the report. |
| `manifest.json` | File inventory with sizes and SHA-256 hashes. |

### Observed vs interpolated

In `vegetation_timeseries.csv`:

- `record_type = observation` → measured scene value (`is_observed = true`).
- `record_type = daily_smoothed` → PCHIP/Whittaker model output
  (`is_observed = false`, `ndvi` empty, `ndvi_smoothed` filled).

Never treat the second class as a measurement.

## Data sources

| Source | Role | Resolution | Credentials |
|---|---|---|---|
| Sentinel-2 L2A (AWS Earth Search) | Default NDVI | 10 m / ~5-day revisit | none |
| MODIS MOD13Q1 (Planetary Computer) | Coarse NDVI | 250 m / 16-day composite | none |
| Landsat C2 L2 (Planetary Computer) | Alternative NDVI | 30 m / 16-day repeat | none |
| NASA POWER | Weather | ~55 × 60 km grid | none |
| SoilGrids 2.0 | Soil context | 250 m prediction | none |
| SRTM / Skadi | Elevation, slope | ~90 m | none |
| CropHarvest labels | Optional context | point labels | none |
| SMAP / IMERG / ERA5 | Probe-only adapters | varies | Earthdata / CDS |

All source statuses, access times, rejection reasons and licenses are recorded in
`field_summary.json` and the report. A failed source produces an explicit
`error`/`unavailable` entry and degrades the affected scores; it is never
replaced by synthetic values.

### Sentinel-2 masking

NDVI uses B04 (red) and B08 (NIR) with the scene classification layer (SCL).
Accepted classes are vegetation (4), bare soil (5) and unclassified/low-
probability (7). Water, cloud, shadow, snow, saturated and no-data pixels are
excluded. The scene value is the **median NDVI over valid field pixels**.

## Rotation scoring

Weights live in `config/scoring.json`; rotations in `config/rotations.json`;
crop attribute ranges and source references in `config/crop_catalog.json`.

Seven criteria:

1. Water balance — indicative crop demand vs effective growing-season rain.
2. Soil health — N fixation, residue/organic matter, rooting depth, legume bonus.
3. Erosion control — residue/cover class, slope penalty, fallow penalty.
4. Forage/livestock value.
5. Market fit — qualitative suitability plus diversity (no prices/contracts).
6. Resilience — family diversity, drought tolerance, monoculture penalties.
7. Agronomic fit — pH, texture, duration vs the available season.

Scores are in `[0, 1]` and are weighted averages. Confidence reflects catalog
attribute confidence and environmental completeness. Every evaluation includes
benefits, risks, trade-offs versus the baseline, and missing information.

**Scores are not recommendations, yields, or profits.** Change the weights with
`--priorities` and inspect how the ranking moves.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The suite covers geometry, JSON safety, catalog/rotation validation, scoring,
cycle detection, context completeness, Hargreaves ET0, and a full mock pipeline
that checks every required output and the synthetic labelling.

## Local API (for the farmer app)

`api_server.py` is a small standard-library HTTP wrapper around the saved
`results/<run-id>/` outputs. It requires no extra dependencies and is what the
Flutter farmer assistant reads.

```bash
.venv/bin/python api_server.py --host 0.0.0.0 --port 8000
```

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Service and run count. |
| `GET /api/farms` | One lightweight record per saved run. |
| `GET /api/farms/<run-id>` | Combined payload for the app's detail screen. |
| `GET /api/farms/<run-id>/summary` | Raw `field_summary.json`. |
| `GET /api/farms/<run-id>/cycles` | Raw `crop_cycles.json`. |
| `GET /api/farms/<run-id>/environment` | Raw `environmental_summary.json`. |
| `GET /api/farms/<run-id>/rotations` | Raw `rotation_analysis.json`. |
| `GET /api/farms/<run-id>/report` | `report.md` as Markdown. |
| `GET /api/sources` | Configured source catalog. |
| `POST /api/analyze` | Run `analyze` from a JSON body (e.g. `--mock`). |

The API is read-only for the app; it never modifies run outputs. `POST
/api/analyze` executes synchronously, so real analyses can take minutes.

## Caching and reproducibility

- HTTP responses, STAC searches and per-scene NDVI are cached under
  `data/downloads/cache/` (override with `AGRI_CACHE_DIR`).
- `run_config.json` records the exact command and parameters.
- `manifest.json` hashes every output.
- Re-running with the same inputs uses the cache where fresh; upstream products
  can still be reprocessed by their providers.

## Architecture

```text
main.py                      CLI
api_server.py                Local HTTP API for the farmer app
config/                      settings, sources, crop catalog, scoring, rotations
src/
  ingestion/                 STAC/COG clients and source adapters
  preprocessing/             geometry and quality flags
  vegetation/                observed/interpolated time-series assembly
  crop_detection/            wrapper around vendored NASA Harvest models
  environmental/             weather/soil/terrain context and derived indices
  crop_knowledge/            crop catalog and rotation definitions
  rotation_engine/           criteria, evaluation, explanations
  reporting/                 JSON/CSV, plots, Markdown, manifest
  pipeline.py                orchestration
vendor/                      unmodified Apache-2.0 NASA Harvest modules
docs/TECHNICAL_ASSESSMENT.md Repository/data-source assessment and decisions
tests/                       unit + mock end-to-end tests
```

## Vendored code and attribution

- `vendor/crop_cycle_detection/` — NASA Harvest crop-cycle-detection,
  commit `293f90658be64dd1f8fc0ca8d36106f70edfc8f2`, Apache-2.0.
- `vendor/crop_stage_detection/` — NASA Harvest crop-stage-detection,
  commit `2005f53d53c0aa9c6240800db4afc514d8f8f13d`, Apache-2.0.

Both are vendored unmodified with their licenses. See `vendor/README.md` and
`docs/TECHNICAL_ASSESSMENT.md` for why CropHarvest is used as a read-only label
database rather than installed (its pins conflict with the modern stack), and
why Presto is deferred.

**Presto status:** not implemented in this milestone. `requirements-presto.txt`
exists only to record the future PyTorch dependency; no adapter, checkpoint
download, embedding output, or CLI command is provided, and the default pipeline
never imports `torch`. Presto embeddings would not be a crop classification
without a labelled, spatially independent evaluation.

## Known limitations and troubleshooting

- **Small fields / mixed pixels.** A 100 m buffer is ~3.1 ha and may include
  multiple land covers. Prefer a real field polygon.
- **Winter cloud gaps.** Mediterranean winters can leave few clear scenes.
  Check gap statistics and valid fractions before trusting season boundaries.
- **SoilGrids timeouts.** The ISRIC service is sometimes slow. The adapter
  records the failure and continues; retry later or use `--no-environment` for a
  vegetation-only run.
- **MODIS signing.** Planetary Computer assets are signed per href via
  `/api/sas/v1/sign`; the per-collection token endpoint is not sufficient for
  MODIS because its items span storage accounts.
- **Heuristic seasons.** Trough-to-trough detection can merge or split cycles in
  bimodal rainfall years, and evergreen crops violate its rise/peak/decline
  assumption.
- **Crop catalog uncertainty.** Attribute ranges are indicative, compiled from
  public references; they are not Palestinian field trials.

## License and data attribution

Code in this prototype is for research use. Vendored modules retain Apache-2.0.
Data licenses are recorded per source in `config/sources.json` and in every run's
provenance. Sentinel-2/Copernicus data require attribution; CropHarvest labels
are CC-BY-SA-4.0; NASA/USGS products are public domain.
