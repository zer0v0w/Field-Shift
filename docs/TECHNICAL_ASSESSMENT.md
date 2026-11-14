# Technical Assessment — NASA Harvest tools and Earth-observation inputs

**Status:** assessment completed; milestone-1 prototype implemented
**Assessment date:** 2026-11-12
**Inspected revisions** (shallow clones of the public `main` branches):

| Repository | Commit | Date | License | Language |
|---|---|---|---|---|
| [cropharvest](https://github.com/nasaharvest/cropharvest) | `aa3c94a667a737bd14329b53da3b5475607bda5f` | 2024-04-30 | CC-BY-SA-4.0 (data); code licence unclear in `setup.py` | Jupyter/Python |
| [presto](https://github.com/nasaharvest/presto) | `11e207a668a34336ced1d8e492a1bd5849b96c4a` | 2026-06-10 | MIT | Jupyter/Python |
| [crop-cycle-detection](https://github.com/nasaharvest/crop-cycle-detection) | `293f90658be64dd1f8fc0ca8d36106f70edfc8f2` | 2026-08-26 | Apache-2.0 | Python |
| [crop-stage-detection](https://github.com/nasaharvest/crop-stage-detection) | `2005f53d53c0aa9c6240800db4afc514d8f8f13d` | 2026-08-10 | Apache-2.0 | Python |
| [openmapflow](https://github.com/nasaharvest/openmapflow) | `bb3256cedaa0b568b85ae026f665e52cb9accaee` | 2024-11-21 | Apache-2.0 | Python |

The assessment is based on source inspection, not on second-hand descriptions.

---

## 1. Repository findings

### 1.1 CropHarvest — dataset and labels, not a field-level observation API

**What it actually provides**

- A curated label database of **95,186 points** (35% multiclass, the rest binary crop/non-crop), aggregated from 21 source datasets. Labels are distributed as `labels.geojson` on Zenodo record **10251170** (85.7 MB).
- Processed Sentinel-2 / Sentinel-1 / ERA5 / SRTM arrays as HDF5 files (`features.tar.gz`, 82.5 MB) and test features (786 MB).
- A `Task`/`CropHarvest` API that filters labels by bounding box, target crop, and test region, then exposes `(X, y)` arrays for supervised benchmarks.

**Compatibility finding**

`setup.py` pins `pandas<2.0.0`, `geopandas==0.9.0`, and `xarray>=0.16.2,<0.20.0`. Those pins conflict with the modern Python 3.11+ / GeoPandas / Rasterio stack required here. Installing `cropharvest` as a package would force an old, unsupported environment. The repository is also primarily a *dataset generation and benchmark* codebase, not a runtime data source for an arbitrary field.

**Decision**

- Do **not** install the `cropharvest` package.
- Reuse its **data product** through a small, read-only adapter that downloads/caches `labels.geojson` and uses modern GeoPandas to answer: *are there labelled crop/non-crop points near this field, and which crop classes occur?*
- Use it for context and future model evaluation, **not** as evidence about the selected field's current crop. Labels have heterogeneous collection dates and are not a substitute for field observations.
- Record CC-BY-SA-4.0 attribution and the Zenodo DOI in output provenance.

**What it can support**

- Crop / non-crop plausibility context and regional class priors.
- Evaluation of a future classifier trained on the same label taxonomy.
- It cannot identify the crop in the selected field, and it does not provide a current-year label.

**Palestine coverage check (2026-11-12)**

A direct query of the published `labels.geojson` within a broad Palestine
bounding box returned only **11 labels**, of which **5 are crop labels**, and
**none carry a multiclass crop label**. This is far too sparse to train or
validate a local crop classifier, and it confirms that CropHarvest is useful here
only as optional regional context. The adapter therefore reports nearby label
counts and class composition, never a field-level crop prediction.

---

### 1.2 Presto — usable pretrained encoder, but not a milestone-1 dependency

**What it actually provides**

- A pretrained transformer encoder/decoder for 24-timestep, 18-band remote-sensing time series (Sentinel-1 VV/VH, Sentinel-2 B1–B12 except B9, ERA5 temperature/precipitation, SRTM elevation/slope).
- `Presto.load_pretrained()` reads `data/default_model.pt` (3.3 MB, committed to the repository, not LFS).
- `single_file_presto.py` is a deliberately self-contained copy of the model for integration into other applications.
- A finetuning head (`construct_finetuning_model`) and `presto.eval.cropharvest_eval` for crop classification benchmarks.
- Missing bands are handled by a mask, so a Sentinel-2-only input is technically possible.

**Compatibility finding**

`setup.py` pins `numpy==1.23.5`, `torch==2.0`, `matplotlib==3.6.2`, `cropharvest==0.6.0`, `earthengine-api==0.1.334`, and `openmapflow[data]==0.2.2`. Installing the package would downgrade the scientific stack. The model code itself is MIT-licensed and can be used through the single-file copy.

**Decision**

- Treat Presto as the **next integration target**, not part of the first end-to-end milestone. It is **not implemented** in this milestone: there is no adapter, model download, or CLI command yet.
- When implemented, provide an optional adapter that lazily imports PyTorch, loads the upstream single-file model and checkpoint, and produces embeddings from the Sentinel-2 bands we already retrieve (other bands masked).
- Do not add PyTorch to the core requirements. Put it in `requirements-presto.txt`.
- Never present Presto embeddings as a crop classification until a labelled, spatially independent evaluation has been done.

---

### 1.3 crop-cycle-detection — the best fit for milestone 1

**What it actually does**

- Input: a single-polygon DataFrame with `date` and `NDVI`.
- Cleans cloud-related downward spikes and outliers using local regression.
- Resamples to daily, gap-fills with PCHIP, smooths with a Whittaker filter.
- Detects peaks/troughs and builds trough-to-trough seasons.
- Outputs per-season start/end, green-up, peak date/value, bounding troughs, and season length, plus the daily processed series.
- Thresholds are configurable and crop-agnostic; `exclude_months` can suppress off-season cover-crop cycles.

**Compatibility finding**

Dependencies are `numpy`, `pandas`, `scipy`, `statsmodels`, `matplotlib` — all compatible with a modern stack. The code is 844 lines, Apache-2.0, and has a smoke test and a bundled sample CSV. It is not packaged on PyPI.

**Decision**

- **Vendor the module unmodified** under `vendor/crop_cycle_detection/` with its LICENSE and upstream commit recorded. This avoids a network dependency on GitHub at runtime and keeps the algorithm auditable.
- Wrap it behind `src/crop_detection/cycles.py` so the rest of the system does not depend on its DataFrame schema.
- Add our own bare-soil-window logic and data-quality annotations around it.

**Limitations to state in outputs**

- Trough-to-trough seasons are a heuristic; they can merge or split cycles in Mediterranean bimodal rainfall years.
- The method assumes the NDVI trajectory has a clear rise/peak/decline. Evergreen crops (olive, citrus) violate this assumption.
- The default `min_ndvi_for_peak=0.5` and 60-day minimum season may reject low-biomass rainfed fields. Parameters are exposed and recorded.
- The model does not distinguish crop from weed/cover-crop growth.

---

### 1.4 crop-stage-detection — useful, optional timeline enrichment

**What it actually does**

- Same preprocessing family (daily interpolation + Whittaker smoothing).
- Assigns generic stages A–E from NDVI level, Kalman-filter trend, and whether a seasonal peak has been confirmed.
- `run_crop_stage_from_dataframe` returns the stage of the **last observation** for one or many fields.
- The optional GEE wrapper fetches Sentinel-2/Landsat NDVI, but requires Earth Engine authentication.

**Compatibility finding**

Core BYOD path depends only on `numpy`, `pandas`, `scipy`, `statsmodels`, `matplotlib`, `tqdm`; compatible. GEE path is optional and not needed.

**Decision**

- Vendor `crop_stage.py` (Apache-2.0) and use it to annotate the **end of each detected season** and selected milestones, producing a field timeline.
- Do not call the GEE wrapper; our own Sentinel-2 ingestion already produces NDVI and keeps provenance.
- Clearly label stages as **generic NDVI-relative stages**, not BBCH/zadoks phenology.

---

### 1.5 OpenMapFlow — regional mapping platform, out of scope for milestone 1

**What it actually provides**

- A project generator and workflow for labelled datasets, Earth Engine export, model training, and regional inference/deployment (Google Cloud, GitHub Actions, TorchServe).
- Its own EO modules (`sentinel1.py`, `sentinel2.py`, `era5.py`, `srtm.py`) and label engineering utilities.

**Compatibility finding**

Pins `numpy<2.0`, `pandas<2`, `xarray==0.19`, and its `[data]` extra pulls Earth Engine, Google Cloud Storage, and NetCDF tooling. It is designed around a cloud project and a GitHub-based project template, not a local CLI experiment.

**Decision**

- Do not integrate in milestone 1.
- Document it as the future path for **regional cropland mapping** once the field-level pipeline and labels are validated.
- Reuse only conceptual patterns (label provenance, bbox handling), not code.

---

## 2. Environmental data sources

The prototype prefers **public, documented, credential-free** interfaces for the default path, and exposes credentialed sources as optional adapters that fail gracefully.

| Source | Variable(s) used | Native spatial | Native temporal | Coverage | Access / auth | Licence | Role in prototype |
|---|---|---|---|---|---|---|---|
| **Sentinel-2 L2A** (AWS Earth Search STAC) | B04, B08, SCL → NDVI | 10 m (VIS/NIR) | ~5-day revisit (constellation) | Global land, 2015–present (L2A availability varies) | Public HTTPS COGs, no auth | Copernicus open, attribution | **Primary NDVI** |
| **MODIS MOD13Q1 v061** (Planetary Computer) | 16-day NDVI, VI quality | 250 m | 16-day composite | Global, 2000–present | Public COGs, no auth | NASA/USGS public domain | Coarse regional context / gap comparison |
| **Landsat C2 L2** (Planetary Computer) | red, nir, QA_PIXEL → NDVI | 30 m | 16-day | Global, 1982–present (mission-dependent) | Public COGs, no auth | USGS public domain | Historical NDVI (longer record) |
| **NASA POWER** (AG community) | T2M, T2M_MIN/MAX, PRECTOTCORR, ALLSKY_SFC_SW_DWN, RH2M, WS2M | 0.5° × 0.625° | Daily | Global land, 1981–present | Public REST, no auth | NASA open | **Primary weather** |
| **SoilGrids 2.0** (ISRIC) | pH, SOC, N, clay, sand, silt | 250 m | Static prediction | Global | Public REST, no auth | CC-BY-4.0 | **Primary soil** |
| **SRTM** (Skadi 1° tiles, SRTM-derived) | elevation, slope | ~90 m | Static (2000) | 60°N–56°S | Public S3, no auth | Public domain (SRTM) | **Primary terrain** |
| **SMAP** | soil moisture | 9–36 km | Daily / 3-hourly | Global, 2015–present | NASA Earthdata login + token | NASA open | Optional adapter; availability check only without token |
| **GPM IMERG** | precipitation | 0.1° (~11 km) | 30 min / daily | 60°N–60°S, 2000–present | NASA GES DISC Earthdata login | NASA open | Optional adapter; NASA POWER precipitation is the default |
| **ERA5 / AgERA5** | temperature, radiation, wind, humidity, precipitation | 0.25° / 0.1° | Hourly / daily | Global, 1940–present / 1979–present | Copernicus CDS API key | Copernicus licence, attribution | Optional adapter; NASA POWER is the default |

**Resolution caveat.** A typical field of 0.1–1 ha is smaller than a POWER grid cell, a SoilGrids pixel, and a MODIS pixel. Weather and soil values are therefore **regional context**, not field measurements. Outputs record this explicitly (`spatial_representativeness`).

**Cloud and quality handling.** Sentinel-2 uses the Scene Classification Layer (SCL): classes 4 (vegetation), 5 (bare), 7 (unclassified) are accepted; cloud, shadow, cirrus, snow, water, and saturated pixels are rejected. Landsat uses QA_PIXEL bits. MODIS uses the VI Quality summary bits. Every observation stores the valid-pixel fraction and scene cloud cover, and observations below a configurable valid-fraction threshold are discarded rather than silently averaged.

**No fabricated observations.** In `mock` mode the pipeline generates a clearly labelled synthetic series. Every JSON output carries `data_mode: "synthetic"`, and the report prints a banner. Synthetic results are never described as an analysis of the requested field.

---

## 3. Integration architecture

```
CLI (main.py)
  └─ pipeline.py
       ├─ ingestion/      adapters → ObservationSet + SourceStatus + provenance
       ├─ preprocessing/  geometry, CRS, quality flags, caching
       ├─ vegetation/     NDVI computation and observation aggregation
       ├─ crop_detection/ vendored cycle + stage models, bare-soil windows
       ├─ environmental/  weather/soil/terrain aggregation, GDD, aridity
       ├─ crop_knowledge/ versioned crop catalog with sources/confidence
       ├─ rotation_engine/ transparent weighted scoring + confidence
       └─ reporting/      JSON/CSV/PNG/Markdown + manifest
```

Design rules:

1. **Ingestion never computes agronomy.** It returns observations and provenance.
2. **Crop detection never reads files or the network.** It takes a DataFrame.
3. **Rotation scoring never calls an API.** It takes a catalog, a rotation definition, priorities, and an environmental summary.
4. **Reporting never recomputes science.** It serialises an analysis result object.
5. Every output is JSON-serialisable so a future REST API can return it unchanged.

---

## 4. Rotation evaluation design (transparent by construction)

The crop catalog stores **ranges and qualitative classes**, not point estimates presented as local truth. Every attribute carries:

```json
{
  "value": [300, 500],
  "unit": "mm per growing season",
  "confidence": "low|medium|high",
  "sources": ["..."],
  "notes": "global indicative range; not a local recommendation"
}
```

Scoring criteria (weights set by farmer priorities):

| Criterion | What it uses | Example computation |
|---|---|---|
| Water balance | crop water demand vs. seasonal precipitation and reference ET | `1 - max(0, demand - effective_rain) / demand` |
| Soil health | legume frequency, residue/organic-matter class, rooting depth | weighted mean of normalised classes + legume bonus |
| Erosion control | slope, residue cover, canopy/root class | class score reduced when slope > 8% |
| Forage / livestock | forage value class | normalised class score |
| Market fit | market class diversity and qualitative suitability | diversity + mean suitability, capped |
| Resilience | crop/family diversity, drought class, calendar overlap | diversity bonus − monoculture penalty |

Rotation-level adjustments (documented in `config/scoring.json`):

- legume frequency bonus (nitrogen fixation potential);
- consecutive same-crop / same-family penalty;
- bare-soil-over-winter penalty;
- diversity bonus.

Outputs include per-criterion score, weight, contribution, confidence, and a human-readable reason. The engine reports **missing information** (e.g. no soil test, no local price data, no irrigation data) and **evidence quality** separately from the score. It never claims yield improvement.

---

## 5. Scope and risks for milestone 1

**In scope**

- Credential-free real analysis for Sentinel-2 NDVI + NASA POWER weather + SoilGrids + SRTM terrain.
- Optional MODIS/Landsat adapters for comparison.
- Vendored cycle and stage detection.
- Configurable crop catalog and three-rotation comparison.
- Mock mode, caching, unit tests, provenance manifest.

**Out of scope**

- Crop type identification from satellite data.
- Yield prediction.
- Field boundary delineation.
- Regional mapping (OpenMapFlow).
- Presto fine-tuning and validated crop classification.
- Irrigation scheduling or fertiliser recommendations.

**Principal risks**

1. **Mixed pixels / small fields.** A 10 m pixel can contain multiple land covers; median NDVI over a 100 m buffer is not a pure field signal. Mitigation: report valid-pixel fraction and polygon area; encourage field polygons.
2. **Cloud gaps in Mediterranean winter.** Critical green-up periods can be missing. Mitigation: quality flags, gap statistics, no interpolation claims; crop-cycle output distinguishes observed vs. interpolated.
3. **Coarse weather/soil.** POWER/SoilGrids are not field measurements. Mitigation: explicit representativeness notes and confidence penalties.
4. **Heuristic season detection.** Trough-to-trough logic is sensitive to thresholds and bimodal rainfall. Mitigation: expose parameters, record them, report caveats.
5. **Crop catalog uncertainty.** Agronomic ranges are global/regional, not Palestinian field trials. Mitigation: ranges, sources, low/medium confidence, and a prominent non-recommendation disclaimer.
6. **API drift.** STAC endpoints and POWER parameters can change. Mitigation: adapter status objects, cache, graceful degradation, and recorded access dates.

---

## 6. Milestone-1 success criterion

Given a field geometry and date range, the prototype must be able to:

1. retrieve real Sentinel-2 observations and compute NDVI with quality flags;
2. build and plot a time series that separates observations from interpolation;
3. detect candidate growing cycles and bare-soil windows with recorded method parameters;
4. attach real weather, soil, and terrain context with resolution caveats;
5. compare three user-configurable rotations with transparent, sourced, confidence-weighted criteria;
6. emit machine-readable outputs plus a human report that states what is measured, estimated, missing, and uncertain.

If any of these cannot be met with real data, the pipeline must say so rather than fill the gap with synthetic values.

---

## 7. Milestone-1 verification (2026-11-12)

A credential-free Sentinel-2 run at the example point `32.24, 35.30`
(2023-01-01 → 2024-12-31, 100 m buffer) produced:

- **110 usable Sentinel-2 scenes** from 130 candidates; 20 rejected for
  insufficient clear pixels, with rejection reasons recorded per scene.
- **2 candidate seasons** (one with an unresolved start before the first usable
  observation, explicitly flagged) and **1 low-NDVI window** (2024-06-17 →
  2024-11-02).
- NASA POWER weather, SoilGrids soil and SRTM terrain all retrieved; composite
  data completeness 70% during a SoilGrids timeout and 100% on retry.
- A full rotation comparison, report, plots, CSV/JSON outputs and a hashed
  manifest.

The synthetic `--mock` path is exercised by the test suite and is labelled as
synthetic in every output. Unit tests cover geometry, JSON safety, catalog and
rotation validation, scoring, cycle detection, completeness accounting,
Hargreaves ET0, the full mock pipeline, and report/manifest hash consistency
across reruns.

Three implementation bugs found during verification and fixed:

1. The Hargreaves ET0 formula was missing the `0.408` conversion from
   MJ m⁻² day⁻¹ to mm day⁻¹, inflating ET0 and misclassifying aridity.
2. Per-source sampling defaults were wrong for MODIS: a small field occupies
   one 250 m pixel, so the Sentinel-2 default of five valid pixels rejected
   every composite. Defaults are now source-specific and recorded per run.
3. `report.md` embedded stale hashes for outputs written after its inventory was
   computed (notably `run_config.json` and `report_context.json`). The inventory
   is now built after all data outputs exist, and the two self-referential files
   defer their hashes to `manifest.json`.

SoilGrids remains intermittently slow; the adapter records the timeout and the
run continues with a lower completeness score rather than substituting values.

