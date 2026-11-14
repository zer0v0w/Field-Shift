"""End-to-end analysis pipeline.

The pipeline is deliberately explicit about failures: an unavailable source
produces an ``unavailable``/``error`` entry in the provenance table and a
degraded output, never synthetic values silently substituted for real data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config.settings import (
    BARE_SOIL_MIN_DAYS,
    BARE_SOIL_NDVI_MAX,
    CYCLE_DEFAULTS,
    DATA_DIR,
    DEFAULT_BUFFER_M,
    DEFAULT_CLOUD_COVER_MAX,
    DEFAULT_MIN_VALID_FRACTION,
    DISCLAIMER,
    RESULTS_DIR,
    ROTATIONS_PATH,
    SCORING_CONFIG_PATH,
    CROP_CATALOG_PATH,
    RuntimeSettings,
    software_versions,
)

from .crop_detection.cycles import detect_cycles
from .crop_knowledge.catalog import CropCatalog, load_rotations
from .environmental.context import build_context
from .ingestion.base import SourceResult, SourceStatus, utc_now_iso
from .ingestion.cache import Cache
from .ingestion.cropharvest import CropHarvestAdapter
from .ingestion.http import build_session
from .ingestion.landsat import LandsatAdapter
from .ingestion.mock import MockAdapter
from .ingestion.modis import ModisAdapter
from .ingestion.nasa_power import NasaPowerAdapter
from .ingestion.sentinel2 import Sentinel2Adapter
from .ingestion.soilgrids import SoilGridsAdapter
from .ingestion.srtm import SrtmAdapter
from .ingestion.open_meteo import OpenMeteoAdapter
from .preprocessing.geometry import FieldGeometry
from .preprocessing.quality import quality_summary
from .reporting.json_writer import json_safe, read_json, write_csv, write_json
from .reporting.manifest import build_manifest, describe_outputs, write_manifest
from .reporting.markdown import write_report
from .reporting.plots import plot_ndvi, plot_rotations
from .rotation_engine.evaluator import (
    comparison_rows,
    evaluate_rotations,
    evaluation_summary,
    parse_priorities,
)
from .vegetation.timeseries import coverage_summary, merge_daily, observations_to_frame

NDVI_ADAPTERS = {
    "sentinel-2-l2a": "Sentinel-2 L2A (10 m, about 5-day revisit)",
    "modis-13Q1-061": "MODIS MOD13Q1 (250 m, 16-day composite)",
    "landsat-c2-l2": "Landsat Collection 2 L2 (30 m, 16-day repeat)",
    "mock": "Synthetic generator (pipeline testing only)",
}

#: Per-source sampling defaults.  A small field occupies one 250 m MODIS pixel,
#: so requiring five valid pixels would reject every composite.
SOURCE_SAMPLING_DEFAULTS = {
    "sentinel-2-l2a": {"min_valid_pixels": 5, "max_scenes": 400},
    "modis-13Q1-061": {"min_valid_pixels": 1, "max_scenes": 60},
    "landsat-c2-l2": {"min_valid_pixels": 3, "max_scenes": 120},
    "mock": {"min_valid_pixels": 5, "max_scenes": 400},
}

DESCRIPTIONS = {
    "field_summary.json": "Run overview, field geometry, source statuses and rotation ranking.",
    "vegetation_timeseries.csv": "Observed NDVI rows plus separately flagged daily smoothed rows.",
    "crop_cycles.json": "Detected candidate seasons, generic stages and low-NDVI windows.",
    "environmental_summary.json": "Weather, soil and terrain context with completeness and caveats.",
    "rotation_comparison.csv": "One row per rotation with criterion scores and explanations.",
    "rotation_analysis.json": "Full rotation evaluations, criteria, benefits, risks and trade-offs.",
    "report.md": "Human-readable report generated from the structured outputs.",
    "ndvi_timeseries.png": "NDVI observations, smoothed series, seasons and low-NDVI windows.",
    "rotation_comparison.png": "Stacked weighted-criterion comparison of the rotations.",
    "report_context.json": "All structured inputs used to render report.md (for `main.py report`).",
    "run_config.json": "Exact request parameters used for this run.",
    "manifest.json": "Inventory of output files with sizes and SHA-256 hashes.",
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")[:80]


@dataclass
class AnalysisRequest:
    field: FieldGeometry
    start: str
    end: str
    source: str = "sentinel-2-l2a"
    output_dir: Path | None = None
    cloud_cover_max: float = DEFAULT_CLOUD_COVER_MAX
    min_valid_fraction: float = DEFAULT_MIN_VALID_FRACTION
    min_valid_pixels: int | None = None
    max_scenes: int | None = None
    priorities: dict[str, float] | None = None
    priorities_text: str = "configured defaults"
    baseline_id: str | None = None
    include_environment: bool = True
    include_cropharvest: bool = False
    cropharvest_radius_km: float = 25.0
    cropharvest_download: bool = False
    seed: int = 42
    command: str = ""
    run_id: str | None = None
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)

    @property
    def data_mode(self) -> str:
        return "synthetic" if self.source == "mock" else "real"

    @property
    def n_days(self) -> int:
        return (pd.Timestamp(self.end) - pd.Timestamp(self.start)).days + 1


class AnalysisPipeline:
    def __init__(self, request: AnalysisRequest):
        if request.source not in NDVI_ADAPTERS:
            raise ValueError(
                f"Unknown source '{request.source}'. Choose from: {', '.join(NDVI_ADAPTERS)}"
            )
        if pd.Timestamp(request.end) <= pd.Timestamp(request.start):
            raise ValueError("End date must be after start date")
        self.request = request
        self.settings = request.settings
        self.settings.ensure_dirs()
        self.output_dir = Path(request.output_dir) if request.output_dir else self._default_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = request.run_id or self._make_run_id()
        self.cache = Cache(self.settings.cache_dir)
        self.session = build_session(self.settings.user_agent)
        self.scoring_config = json.loads(SCORING_CONFIG_PATH.read_text(encoding="utf-8"))
        self.catalog = CropCatalog(CROP_CATALOG_PATH)
        self.rotations, self.configured_baseline = load_rotations(ROTATIONS_PATH, self.catalog)
        self.baseline_id = request.baseline_id or self.configured_baseline
        if self.baseline_id not in {r.rotation_id for r in self.rotations}:
            raise ValueError(f"Baseline '{self.baseline_id}' is not a configured rotation")
        self.priorities = request.priorities or parse_priorities(None, self.scoring_config)
        self.sources: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ setup
    def _default_dir(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return RESULTS_DIR / f"{_slug(self.request.field.field_id)}_{self.request.start}_{self.request.end}_{stamp}"

    def _make_run_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{_slug(self.request.field.field_id)}_{self.request.start}_{self.request.end}_{stamp}"

    def _source_entry(self, result: SourceResult) -> dict[str, Any]:
        entry = result.status.to_dict()
        entry["metadata"] = {**result.metadata, **(result.status.metadata or {})}
        return entry

    # --------------------------------------------------------------- adapters
    def _ndvi_adapter(self):
        source = self.request.source
        if source == "sentinel-2-l2a":
            return Sentinel2Adapter(
                self.cache, self.session, workers=self.settings.workers, timeout=self.settings.timeout_s
            )
        if source == "modis-13Q1-061":
            return ModisAdapter(
                self.cache, self.session, workers=self.settings.workers, timeout=self.settings.timeout_s
            )
        if source == "landsat-c2-l2":
            return LandsatAdapter(
                self.cache, self.session, workers=self.settings.workers, timeout=self.settings.timeout_s
            )
        return MockAdapter(seed=self.request.seed)

    def _sampling(self) -> dict[str, int]:
        defaults = SOURCE_SAMPLING_DEFAULTS[self.request.source]
        return {
            "min_valid_pixels": (
                self.request.min_valid_pixels
                if self.request.min_valid_pixels is not None
                else defaults["min_valid_pixels"]
            ),
            "max_scenes": (
                self.request.max_scenes
                if self.request.max_scenes is not None
                else defaults["max_scenes"]
            ),
        }

    def _fetch_vegetation(self) -> SourceResult:
        adapter = self._ndvi_adapter()
        sampling = self._sampling()
        kwargs = {
            "cloud_cover_max": self.request.cloud_cover_max,
            "min_valid_fraction": self.request.min_valid_fraction,
            "min_valid_pixels": sampling["min_valid_pixels"],
            "max_scenes": sampling["max_scenes"],
        }
        if self.request.source == "modis-13Q1-061":
            # MODIS has no scene-level cloud cover in the STAC item.
            kwargs.pop("cloud_cover_max")
        try:
            return adapter.fetch(self.request.field, self.request.start, self.request.end, **kwargs)
        except Exception as exc:  # pragma: no cover - adapter-level guard
            return SourceResult(
                source_id=self.request.source,
                status=SourceStatus(
                    source_id=self.request.source,
                    status="error",
                    message=f"{type(exc).__name__}: {exc}",
                ),
                metadata={"requested_start": self.request.start, "requested_end": self.request.end},
            )

    def _fetch_environment(self) -> tuple[SourceResult | None, SourceResult | None, SourceResult | None, SourceResult | None]:
        """Return (nasa_power_weather, soil, terrain, open_meteo)."""
        if not self.request.include_environment:
            return None, None, None, None
        field, start, end = self.request.field, self.request.start, self.request.end
        if self.request.data_mode == "synthetic":
            mock = MockAdapter(seed=self.request.seed)
            # MockAdapter.terrain takes (field) in pipeline's original real path signature? keep compatible.
            try:
                terrain_res = mock.terrain(field)
            except TypeError:
                terrain_res = mock.terrain(field, start, end)
            return mock.weather(field, start, end), mock.soil(field, start, end), terrain_res, None

        def safe(source_id: str, call):
            try:
                return call()
            except Exception as exc:
                return SourceResult(
                    source_id=source_id,
                    status=SourceStatus(
                        source_id=source_id,
                        status="error",
                        message=f"{type(exc).__name__}: {exc}",
                    ),
                )

        weather = safe(
            "nasa-power", lambda: NasaPowerAdapter(self.cache, self.session, timeout=90).fetch(field, start, end)
        )
        soil = safe(
            "soilgrids-2",
            lambda: SoilGridsAdapter(self.cache, self.session, timeout=60).fetch(field, start, end),
        )
        terrain = safe(
            "srtm-skadi",
            lambda: SrtmAdapter(self.cache, self.session, timeout=120).fetch(field, start, end),
        )
        # Open-Meteo: free, no-auth soil moisture + temperature + precipitation + derived metrics
        om = safe(
            "open-meteo",
            lambda: OpenMeteoAdapter(self.cache, self.session, timeout=30).fetch(field, start, end),
        )
        return weather, soil, terrain, om

    def _fetch_cropharvest(self) -> SourceResult | None:
        if not self.request.include_cropharvest:
            return None
        adapter = CropHarvestAdapter(DATA_DIR / "cropharvest", self.cache, self.session)
        try:
            return adapter.nearby(
                self.request.field,
                radius_km=self.request.cropharvest_radius_km,
                download=self.request.cropharvest_download,
            )
        except Exception as exc:
            return SourceResult(
                source_id=adapter.source_id,
                status=SourceStatus(
                    source_id=adapter.source_id,
                    status="error",
                    message=f"{type(exc).__name__}: {exc}",
                ),
            )

    # ------------------------------------------------------------------- run
    def run(self) -> dict[str, Any]:
        request = self.request
        field = request.field

        vegetation_result = self._fetch_vegetation()
        self.sources.append(self._source_entry(vegetation_result))
        observations = observations_to_frame(vegetation_result)

        cycles, daily = detect_cycles(
            observations,
            field_id=field.field_id,
            params=CYCLE_DEFAULTS,
            bare_soil_ndvi_max=BARE_SOIL_NDVI_MAX,
            bare_soil_min_days=BARE_SOIL_MIN_DAYS,
        )
        frame = merge_daily(observations, daily, source_id=vegetation_result.source_id)

        weather, soil, terrain, om = self._fetch_environment()
        for result in (weather, soil, terrain, om):
            if result is not None:
                self.sources.append(self._source_entry(result))

        # Explore requested NASA datasets (SMAP, GPM IMERG, ECOSTRESS, GRACE/GRACE-FO)
        nasa_extra: dict[str, SourceResult | None] = {"gpm-imerg": None, "smap": None, "ecostress": None, "grace-fo": None}
        if request.include_environment and request.data_mode != "synthetic":
            def safe_fetch(name: str, fn):
                try:
                    return fn()
                except Exception as exc:
                    return SourceResult(
                        source_id=name,
                        status=SourceStatus(source_id=name, status="error", message=f"{type(exc).__name__}: {exc}"),
                    )
            nasa_extra["gpm-imerg"] = safe_fetch("gpm-imerg", lambda: GpmImergExplorerAdapter(self.cache, self.session).fetch(field, request.start, request.end))
            nasa_extra["smap"] = safe_fetch("smap", lambda: SmapExplorerAdapter(self.cache, self.session).fetch(field, request.start, request.end))
            nasa_extra["ecostress"] = safe_fetch("ecostress", lambda: EcostressExplorerAdapter(self.cache, self.session).fetch(field, request.start, request.end))
            nasa_extra["grace-fo"] = safe_fetch("grace-fo", lambda: GraceExplorerAdapter(self.cache, self.session).fetch(field, request.start, request.end))
            for r in nasa_extra.values():
                if r is not None:
                    self.sources.append(self._source_entry(r))

        cropharvest = self._fetch_cropharvest()
        if cropharvest is not None:
            self.sources.append(self._source_entry(cropharvest))

        # ------------------------------------------------------------------ derived climate metrics
        # Open-Meteo provides derived heat/frost/extreme-precip/ET0/water-trend metrics
        # and local moisture/temperature profiles; merge them into context when available.
        om_meta = om.metadata if om and getattr(om, "metadata", None) else {}
        heat_stress = om_meta.get("heat_stress", {}) if isinstance(om_meta.get("heat_stress"), dict) else {}
        frost_risk = om_meta.get("frost_risk", {}) if isinstance(om_meta.get("frost_risk"), dict) else {}
        extreme_precip = om_meta.get("extreme_precipitation", {}) if isinstance(om_meta.get("extreme_precipitation"), dict) else {}
        et0_proxy = om_meta.get("et0_hargreaves_proxy", {}) if isinstance(om_meta.get("et0_hargreaves_proxy"), dict) else {}
        long_term_water = om_meta.get("long_term_water_trend", {})

        context, frame_raw = build_context(
            weather,
            soil,
            terrain,
            lat=field.centroid_lonlat[1],
            start=request.start,
            end=request.end,
            seasons=cycles.get("seasons", []),
        )

        # Augment context with Open-Meteo-derived metrics when available.
        if om and getattr(om, "status", None) and getattr(om.status, "status", "") in ("ok", "partial"):
            if isinstance(context.weather, dict):
                if context.weather.get("available") is False:
                    # No NASA POWER frame; still expose Open-Meteo derived context.
                    context.weather["available"] = True
                    context.weather["source"] = context.weather.get("source") or (om.source_id if om else None)
                    context.weather.setdefault("spatial_representativeness", "Open-Meteo reanalysis/grid data, not a field station")
                context.weather["heat_stress_days"] = heat_stress.get("heat_stress_days")
                context.weather["heat_stress_pct"] = heat_stress.get("heat_stress_pct")
                context.weather["frost_risk_days"] = frost_risk.get("frost_risk_days")
                context.weather["frost_risk_pct"] = frost_risk.get("frost_risk_pct")
                context.weather["extreme_precip_days"] = extreme_precip.get("extreme_precip_days")
                context.weather["extreme_precip_pct"] = extreme_precip.get("extreme_precip_pct")
                context.weather["max_1day_rain"] = extreme_precip.get("max_1day_rain")
                if et0_proxy.get("et0_mm_per_year") is not None and context.weather.get("et0_hargreaves_mm_per_year") is None:
                    context.weather["et0_hargreaves_mm_per_year"] = et0_proxy.get("et0_mm_per_year")
                context.weather["et0_hargreaves_quality"] = et0_proxy.get("et0_quality") or context.weather.get("et0_hargreaves_quality")
                if long_term_water:
                    context.weather["long_term_water_trend"] = long_term_water

                # Additional local moisture/temperature/precipitation profiles from Open-Meteo.
                for key in ("soil_moisture", "soil_temperature", "precipitation", "air_temperature"):
                    if key in om_meta:
                        context.weather[key] = om_meta[key]
                if om_meta.get("note"):
                    context.caveats.append(str(om_meta.get("note"))) if isinstance(getattr(context, "caveats", None), list) else None

        # Integrate explored NASA datasets (SMAP, GPM IMERG, ECOSTRESS, GRACE/GRACE-FO)
        try:
            nasa_context = {}
            smap_r = nasa_extra.get("smap") if 'nasa_extra' in locals() else None
            gpm_r = nasa_extra.get("gpm-imerg") if 'nasa_extra' in locals() else None
            eco_r = nasa_extra.get("ecostress") if 'nasa_extra' in locals() else None
            grace_r = nasa_extra.get("grace-fo") if 'nasa_extra' in locals() else None
            if gpm_r is not None:
                nasa_context["gpm_imerg"] = {"status": getattr(gpm_r.status, 'status', None), **(gpm_r.metadata or {})}
            if smap_r is not None:
                nasa_context["smap"] = {"status": getattr(smap_r.status, 'status', None), **(smap_r.metadata or {})}
                if isinstance(context.weather, dict) and smap_r.metadata.get("mean_soil_moisture_proxy_pct") is not None:
                    context.weather.setdefault("soil_moisture_proxy_pct", smap_r.metadata.get("mean_soil_moisture_proxy_pct"))
            if eco_r is not None:
                nasa_context["ecostress"] = {"status": getattr(eco_r.status, 'status', None), **(eco_r.metadata or {})}
                if isinstance(context.weather, dict):
                    if eco_r.metadata.get("heat_stress_days_ge_35c_proxy") is not None and context.weather.get("heat_stress_days") is None:
                        context.weather["heat_stress_days"] = eco_r.metadata.get("heat_stress_days_ge_35c_proxy")
                    if eco_r.metadata.get("mean_et_demand_proxy_mm_day") is not None:
                        context.weather.setdefault("evaporation_demand_proxy_mm_day", eco_r.metadata.get("mean_et_demand_proxy_mm_day"))
            if grace_r is not None:
                nasa_context["grace_fo"] = {"status": getattr(grace_r.status, 'status', None), **(grace_r.metadata or {})}
                if isinstance(context.weather, dict) and grace_r.metadata.get("trend_signal") and not context.weather.get("long_term_water_trend"):
                    context.weather["long_term_water_trend"] = {
                        "signal": grace_r.metadata.get("trend_signal"),
                        "slope_mm_per_month_step": grace_r.metadata.get("trend_slope_mm_per_month_step"),
                        "source": "grace-fo-explored/nasa-power-trend",
                    }
            if nasa_context and isinstance(context.weather, dict):
                context.weather["nasa_datasets_explored"] = nasa_context
                context.caveats.append(
                    "SMAP, GPM IMERG, ECOSTRESS, and GRACE/GRACE-FO were explored via NASA CMR; where direct retrieval was unavailable, indicators were derived from accessible NASA inputs and are labeled accordingly."
                )
        except Exception:
            pass

        evaluations = evaluate_rotations(
            self.rotations,
            self.catalog,
            context,
            self.priorities,
            self.scoring_config,
            baseline_id=self.baseline_id,
        )
        rotations = evaluation_summary(evaluations, baseline_id=self.baseline_id)
        rotation_rows = comparison_rows(evaluations)

        vegetation = self._vegetation_payload(vegetation_result, observations, frame, cycles)
        overall = self._overall_status(vegetation, context)
        assumptions = self._assumptions()
        missing = self._missing_information(context, evaluations)

        field_payload = field.to_dict()
        sampling = self._sampling()
        request_payload = {
            "start": request.start,
            "end": request.end,
            "n_days": request.n_days,
            "source": request.source,
            "cloud_cover_max": request.cloud_cover_max,
            "min_valid_fraction": request.min_valid_fraction,
            "min_valid_pixels": sampling["min_valid_pixels"],
            "max_scenes": sampling["max_scenes"],
            "baseline_id": self.baseline_id,
            "priorities": {k: round(v, 4) for k, v in self.priorities.items()},
            "priorities_text": request.priorities_text,
            "include_environment": request.include_environment,
            "include_cropharvest": request.include_cropharvest,
            "cropharvest_radius_km": request.cropharvest_radius_km if request.include_cropharvest else None,
            "seed": request.seed if request.data_mode == "synthetic" else None,
        }

        field_summary = {
            "schema_version": "1.0",
            "run": {
                "run_id": self.run_id,
                "generated_at": utc_now_iso(),
                "data_mode": request.data_mode,
                "overall_status": overall,
                "output_dir": str(self.output_dir),
                "command": request.command,
            },
            "field": field_payload,
            "request": request_payload,
            "vegetation": vegetation,
            "cycles": {
                "status": cycles.get("status"),
                "n_seasons": cycles.get("n_seasons", 0),
                "n_bare_soil_windows": len(cycles.get("bare_soil_windows", [])),
                "method": cycles.get("method", {}).get("name"),
            },
            "environment": {
                "available": {
                    "weather": bool(context.weather.get("available")),
                    "soil": bool(context.soil.get("available")),
                    "terrain": bool(context.terrain.get("available")),
                },
                "data_completeness": context.data_completeness,
                "missing": context.missing,
            },
            "rotation": {
                "baseline_id": rotations.get("baseline_id"),
                "ranking": rotations.get("ranking", []),
                "disclaimer": rotations.get("disclaimer"),
            },
            "sources": self.sources,
            "warnings": self.warnings,
            "assumptions": assumptions,
            "missing_information": missing,
            "software": software_versions(),
            "disclaimer": DISCLAIMER,
        }

        # ------------------------------------------------------------- write
        write_csv(self.output_dir / "vegetation_timeseries.csv", frame)
        write_json(
            self.output_dir / "crop_cycles.json",
            {
                "schema_version": "1.0",
                "field_id": field.field_id,
                "run_id": self.run_id,
                "data_mode": request.data_mode,
                "requested_start": request.start,
                "requested_end": request.end,
                "source_id": vegetation_result.source_id,
                "generated_at": utc_now_iso(),
                **cycles,
            },
        )
        write_json(
            self.output_dir / "environmental_summary.json",
            {
                "schema_version": "1.0",
                "field_id": field.field_id,
                "run_id": self.run_id,
                "data_mode": request.data_mode,
                "requested_start": request.start,
                "requested_end": request.end,
                "sources": {
                    "weather": weather.source_id if weather else None,
                    "soil": soil.source_id if soil else None,
                    "terrain": terrain.source_id if terrain else None,
                },
                "generated_at": utc_now_iso(),
                **context.to_dict(),
            },
        )
        write_csv(self.output_dir / "rotation_comparison.csv", pd.DataFrame(rotation_rows))
        write_json(
            self.output_dir / "rotation_analysis.json",
            {
                "schema_version": "1.0",
                "field_id": field.field_id,
                "run_id": self.run_id,
                "data_mode": request.data_mode,
                "generated_at": utc_now_iso(),
                **rotations,
            },
        )
        write_json(self.output_dir / "field_summary.json", field_summary)

        plot_ndvi(
            frame,
            cycles,
            field_id=field.field_id,
            data_mode=request.data_mode,
            path=self.output_dir / "ndvi_timeseries.png",
        )
        plot_rotations(
            evaluations,
            path=self.output_dir / "rotation_comparison.png",
            baseline_id=self.baseline_id,
        )

        cropharvest_payload = None
        if cropharvest is not None:
            cropharvest_payload = {**cropharvest.status.to_dict(), **cropharvest.metadata}

        run_config = {
            "run_id": self.run_id,
            "generated_at": utc_now_iso(),
            "command": request.command,
            "output_dir": str(self.output_dir),
            "field": field_payload,
            "request": request_payload,
            "data_mode": request.data_mode,
            "software": software_versions(),
        }
        write_json(self.output_dir / "run_config.json", run_config)

        report_ctx = {
            "run": {
                "run_id": self.run_id,
                "generated_at": utc_now_iso(),
                "data_mode": request.data_mode,
                "overall_status": overall,
                "command": request.command,
                "ndvi_plot": "ndvi_timeseries.png",
            },
            "field": field_payload,
            "request": request_payload,
            "vegetation": vegetation,
            "cycles": cycles,
            "environment": context.to_dict(),
            "rotations": rotations,
            "sources": self.sources,
            "assumptions": assumptions,
            "missing_information": missing,
            "software": software_versions(),
            "disclaimer": DISCLAIMER,
        }

        # Inventory every data output. report.md and report_context.json are
        # rendered last and would otherwise carry stale hashes on reruns, so
        # their entries defer to manifest.json (which is hashed after writing).
        outputs = describe_outputs(self.output_dir, DESCRIPTIONS)
        known = {entry["path"] for entry in outputs}
        for name in ("report.md", "report_context.json"):
            if name not in known:
                outputs.append({"path": name, "description": DESCRIPTIONS.get(name, "")})
        for entry in outputs:
            if entry["path"] in {"report.md", "report_context.json"}:
                entry.pop("sha256", None)
                entry.pop("bytes", None)
        outputs.sort(key=lambda entry: entry["path"])
        report_ctx["outputs"] = outputs

        write_json(self.output_dir / "report_context.json", report_ctx)
        write_report(self.output_dir / "report.md", report_ctx)

        manifest = build_manifest(
            self.output_dir,
            run_id=self.run_id,
            data_mode=request.data_mode,
            command=request.command,
            field_id=field.field_id,
            source_id=vegetation_result.source_id,
            descriptions=DESCRIPTIONS,
            extra={
                "overall_status": overall,
                "software": software_versions(),
                "assumptions": assumptions,
            },
        )
        write_manifest(self.output_dir, manifest)

        return {
            "run_id": self.run_id,
            "output_dir": str(self.output_dir),
            "overall_status": overall,
            "field_summary": field_summary,
            "report_ctx": report_ctx,
            "manifest": manifest,
        }

    # --------------------------------------------------------------- helpers
    def _vegetation_payload(
        self,
        result: SourceResult,
        observations: pd.DataFrame,
        frame: pd.DataFrame,
        cycles: dict[str, Any],
    ) -> dict[str, Any]:
        quality = quality_summary(result.observations)
        quality.update(
            {
                key: cycles.get("quality", {}).get(key)
                for key in (
                    "span_days",
                    "median_gap_days",
                    "max_gap_days",
                    "gaps_over_30_days",
                    "largest_gap_start",
                    "largest_gap_end",
                )
            }
        )
        observed = observations[observations["record_type"] == "observation"] if not observations.empty else observations
        values = observed["ndvi"].dropna() if not observed.empty else pd.Series(dtype=float)
        return {
            "source_id": result.source_id,
            "status": result.status.status,
            "message": result.status.message,
            "n_observations": int(len(observed)),
            "n_daily_values": int((frame["record_type"] == "daily_smoothed").sum()) if not frame.empty else 0,
            "first_observation": observed["date"].min().date().isoformat() if not observed.empty else None,
            "last_observation": observed["date"].max().date().isoformat() if not observed.empty else None,
            "ndvi_min": round(float(values.min()), 4) if len(values) else None,
            "ndvi_max": round(float(values.max()), 4) if len(values) else None,
            "ndvi_median": round(float(values.median()), 4) if len(values) else None,
            "quality": quality,
            "coverage": coverage_summary(frame, self.request.start, self.request.end) if not frame.empty else {},
            "metadata": result.metadata,
        }

    def _overall_status(self, vegetation: dict[str, Any], context) -> str:
        has_vegetation = vegetation.get("n_observations", 0) > 0
        has_environment = context.data_completeness > 0
        if has_vegetation and has_environment:
            degraded = (
                vegetation.get("status") == "partial"
                or bool(context.missing)
                or context.data_completeness < 0.9
            )
            return "partial" if degraded else "ok"
        if has_vegetation or has_environment:
            return "partial"
        return "failed"

    def _assumptions(self) -> list[str]:
        request = self.request
        assumptions = [
            "NDVI is a vegetation index; it does not identify crop species or management.",
            "Each scene value is the median NDVI of valid field pixels; values are not yield proxies.",
            "Daily values are interpolated/smoothed model output and are flagged separately from observations.",
            f"Cycle detection uses NDVI thresholds {CYCLE_DEFAULTS['ndvi_threshold_for_approx_season_start']}/"
            f"{CYCLE_DEFAULTS['ndvi_threshold_for_approx_season_end']}, minimum season length "
            f"{CYCLE_DEFAULTS['min_season_length']} days and a {CYCLE_DEFAULTS['season_window_days']}-day "
            "peak window.",
            f"Low-NDVI windows use NDVI < {BARE_SOIL_NDVI_MAX} for at least {BARE_SOIL_MIN_DAYS} days.",
            (
                "Weather, soil and terrain are synthetic generators, not NASA POWER, SoilGrids or SRTM."
                if request.data_mode == "synthetic"
                else "Weather is a NASA POWER grid cell (~55 x 60 km); soil is SoilGrids 250 m; terrain is SRTM ~90 m."
            ),
            "Rotation scoring uses effective growing-season rain = 70% of precipitation, GDD base 5 °C, "
            "and Hargreaves ET0 rather than Penman-Monteith.",
            "Rotation scores are relative heuristics in [0, 1]; they are not yields, profits, or emissions.",
            "No pest, disease, salinity, labour, machinery, irrigation-efficiency, or market-price model is included.",
        ]
        if request.data_mode == "synthetic":
            assumptions.insert(
                0,
                "SYNTHETIC RUN: all vegetation, weather, soil and terrain values are generated for "
                "pipeline testing and are not observations of any real field.",
            )
        return assumptions

    def _missing_information(self, context, evaluations) -> list[str]:
        missing = list(context.missing)
        for evaluation in evaluations:
            missing.extend(evaluation.missing_information)
        missing.extend(
            [
                "field boundary or management-unit geometry (point buffers are approximations)",
                "actual crop history, varieties, planting and harvest dates",
                "irrigation source, water rights and application efficiency",
                "local soil test (pH, organic matter, salinity, nutrients)",
                "local prices, contracts, labour and machinery costs",
            ]
        )
        return list(dict.fromkeys(missing))


def run_from_config(path: Path, *, output_dir: Path | None = None, command: str = "") -> dict[str, Any]:
    """Re-run an analysis from a saved ``run_config.json``."""
    payload = read_json(path)
    from .preprocessing.geometry import build_geojson_field

    field_payload = payload["field"]
    field = build_geojson_field(
        {
            "type": "Feature",
            "geometry": field_payload["geometry"],
            "properties": {"field_id": field_payload["field_id"]},
        },
        field_id=field_payload["field_id"],
    )
    request_payload = payload["request"]
    request = AnalysisRequest(
        field=field,
        start=request_payload["start"],
        end=request_payload["end"],
        source=request_payload["source"],
        output_dir=output_dir or Path(payload.get("output_dir") or RESULTS_DIR / payload["run_id"]),
        cloud_cover_max=request_payload.get("cloud_cover_max", DEFAULT_CLOUD_COVER_MAX),
        min_valid_fraction=request_payload.get("min_valid_fraction", DEFAULT_MIN_VALID_FRACTION),
        min_valid_pixels=request_payload.get("min_valid_pixels"),
        max_scenes=request_payload.get("max_scenes"),
        priorities=request_payload.get("priorities"),
        priorities_text=request_payload.get("priorities_text", "configured defaults"),
        baseline_id=request_payload.get("baseline_id"),
        include_environment=request_payload.get("include_environment", True),
        include_cropharvest=request_payload.get("include_cropharvest", False),
        cropharvest_radius_km=request_payload.get("cropharvest_radius_km") or 25.0,
        seed=request_payload.get("seed") or 42,
        command=command,
        run_id=payload.get("run_id"),
    )
    return AnalysisPipeline(request).run()


def default_field(lat: float, lon: float, buffer_m: float = DEFAULT_BUFFER_M) -> FieldGeometry:
    from .preprocessing.geometry import build_point_field

    return build_point_field(lat, lon, buffer_m=buffer_m)