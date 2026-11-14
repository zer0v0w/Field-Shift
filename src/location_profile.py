"""Location-aware environmental profile and crop suitability.

The Groq analysis is only as good as the location context it receives.  This
module fetches the real data the project already knows how to ingest
(NASA POWER weather, SoilGrids soil, SRTM terrain) for a single point, turns it
into a compact profile, and scores the farmer's own crop list against it.

Everything here is decision support, not farming advice.  Values are modelled
or predicted (grid cells / 250 m soil predictions / ~90 m DEM), not field
measurements, and are labelled as such in the returned payload.

Public API
----------
build_location_profile(lat, lon, ...) -> dict
assess_crops_for_location(crop_list, profile) -> list[dict]
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = Path(
    os.environ.get("AGRI_CACHE_DIR", PROJECT_ROOT / "data" / "downloads" / "cache")
)
PROFILE_DIR = CACHE_ROOT / "location_profiles"
CATALOG_PATH = PROJECT_ROOT / "config" / "crop_catalog.json"
REGIONAL_CROPS_PATH = PROJECT_ROOT / "config" / "regional_crops.json"
SCORING_PATH = PROJECT_ROOT / "config" / "scoring.json"

PROFILE_TTL_SECONDS = int(
    os.environ.get("AGRI_PROFILE_TTL_S", str(30 * 24 * 3600))
)

_MEAN_TEMP_MIN_C = 20.0  # thermal threshold for tropical crops

# Frozen soil-physics realities, not preferences.  max_frost_days = 0 means the
# crop is killed or badly damaged by any frost.  Indicative values from general
# agronomy references; used only as a hard feasibility gate.
_TROPICAL_TRAITS: dict[str, dict[str, float]] = {
    "banana": {"min_mean_temp_c": 20.0, "min_tmin_c": 15.0, "max_frost_days": 0.0,
               "water_demand_mm": (1000.0, 2500.0)},
    "mango": {"min_mean_temp_c": 21.0, "min_tmin_c": 15.0, "max_frost_days": 0.0,
              "water_demand_mm": (700.0, 2000.0)},
    "papaya": {"min_mean_temp_c": 21.0, "min_tmin_c": 16.0, "max_frost_days": 0.0,
               "water_demand_mm": (1000.0, 2000.0)},
    "avocado": {"min_mean_temp_c": 18.0, "min_tmin_c": 12.0, "max_frost_days": 2.0,
                "water_demand_mm": (800.0, 1600.0)},
    "coffee": {"min_mean_temp_c": 17.0, "min_tmin_c": 10.0, "max_frost_days": 0.0,
               "water_demand_mm": (1000.0, 2000.0)},
    "cocoa": {"min_mean_temp_c": 21.0, "min_tmin_c": 18.0, "max_frost_days": 0.0,
              "water_demand_mm": (1200.0, 2500.0)},
    "sugarcane": {"min_mean_temp_c": 20.0, "min_tmin_c": 14.0, "max_frost_days": 0.0,
                  "water_demand_mm": (1200.0, 2500.0)},
    "citrus": {"min_mean_temp_c": 15.0, "min_tmin_c": 8.0, "max_frost_days": 5.0,
               "water_demand_mm": (600.0, 1200.0)},
}

_CACHE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _round(v: Any, ndigits: int = 1) -> float | None:
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _recent_period(years: int = 3) -> tuple[str, str]:
    """Last N complete calendar years (NASA POWER needs a closed period)."""
    end_year = date.today().year - 1
    start_year = end_year - (years - 1)
    return f"{start_year}-01-01", f"{end_year}-12-31"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _catalog_lookup() -> dict[str, dict[str, Any]]:
    """Map normalised catalog names/aliases to crop payloads."""
    raw = _load_json(CATALOG_PATH) or {}
    lookup: dict[str, dict[str, Any]] = {}
    for crop_id, payload in (raw.get("crops") or {}).items():
        names = {
            _norm(crop_id),
            _norm(payload.get("common_name", "")),
            _norm(payload.get("scientific_name", "")),
        }
        for name in names:
            if name:
                lookup[name] = payload
        # common short aliases
        common = _norm(payload.get("common_name", ""))
        for alias in {
            "wheat": "durum_wheat",
            "durum": "durum_wheat",
            "corn": "maize",
            "bean": "faba_bean",
            "vetch": "common_vetch",
            "bare": "fallow",
        }.items():
            if crop_id == alias[1]:
                lookup[_norm(alias[0])] = payload
    return lookup


def _find_catalog_crop(crop_name: str, lookup: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    key = _norm(crop_name)
    if key in lookup:
        return lookup[key]
    # fuzzy contains match, longest key first to avoid "bean" beating "faba_bean"
    for name, payload in sorted(lookup.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(name) >= 4 and (name in key or key in name):
            return payload
    return None


_REGIONAL_LOCK = threading.Lock()
_REGIONAL_LOOKUP: dict[str, dict[str, Any]] | None = None


def _regional_lookup() -> dict[str, dict[str, Any]]:
    """Map normalised names/aliases from regional_crops.json to trait payloads.

    This is the crop knowledge base that is *not* restricted to the original
    ten-crop evaluation catalog: it covers cereals, pulses, oilseeds, roots,
    vegetables and perennials grown from the Jordan Valley to northern Russia.
    """
    global _REGIONAL_LOOKUP
    with _REGIONAL_LOCK:
        if _REGIONAL_LOOKUP is not None:
            return _REGIONAL_LOOKUP
        raw = _load_json(REGIONAL_CROPS_PATH) or {}
        lookup: dict[str, dict[str, Any]] = {}
        for crop_id, payload in (raw.get("crops") or {}).items():
            entry = {"id": crop_id, **(payload or {})}
            names = {_norm(crop_id), _norm(payload.get("common_name", ""))}
            names.update(_norm(alias) for alias in (payload.get("aliases") or []))
            for name in names:
                if name:
                    lookup[name] = entry
        _REGIONAL_LOOKUP = lookup
        return lookup


def _find_regional_crop(crop_name: str, lookup: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    key = _norm(crop_name)
    if key in lookup:
        return lookup[key]
    for name, payload in sorted(lookup.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(name) >= 4 and (name in key or key in name):
            return payload
    return None


def _traits_for(crop_name: str, catalog_lookup: dict[str, dict[str, Any]], regional_lookup: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Return a normalised trait dict, preferring the regional knowledge base."""
    regional = _find_regional_crop(crop_name, regional_lookup)
    if regional:
        return {
            "common_name": regional.get("common_name") or crop_name,
            "group": regional.get("group"),
            "water_demand_mm": regional.get("water_demand_mm"),
            "min_mean_temp_c": regional.get("min_mean_temp_c"),
            "min_tmin_c": regional.get("min_tmin_c"),
            "max_frost_days": regional.get("max_frost_days"),
            "max_mean_temp_c": regional.get("max_mean_temp_c"),
            "ph_range": regional.get("ph_range"),
            "textures": regional.get("textures") or [],
            "drought_tolerance": regional.get("drought_tolerance"),
            "season_days": regional.get("season_days"),
            "lifecycle": regional.get("lifecycle")
            or ("perennial" if (regional.get("group") or "") == "fruit" else "annual"),
            "hardiness_min_c": regional.get("hardiness_min_c"),
            "zones": regional.get("zones") or [],
            "core_zones": regional.get("core_zones") or [],
            "source": "regional",
        }

    catalog_crop = _find_catalog_crop(crop_name, catalog_lookup)
    if not catalog_crop:
        return None
    attrs = catalog_crop.get("attributes") or {}
    demand = (attrs.get("water_demand_mm") or {}).get("value")
    req = (attrs.get("soil_requirements") or {}).get("value") or {}
    return {
        "common_name": catalog_crop.get("common_name") or crop_name,
        "group": catalog_crop.get("type"),
        "water_demand_mm": demand if isinstance(demand, list) and len(demand) == 2 else None,
        "min_mean_temp_c": None,
        "min_tmin_c": None,
        "max_frost_days": None,
        "max_mean_temp_c": None,
        "ph_range": req.get("ph_range"),
        "textures": req.get("textures") or [],
        "drought_tolerance": (attrs.get("drought_tolerance") or {}).get("score"),
        "season_days": (attrs.get("duration_days") or {}).get("value"),
        "lifecycle": "annual",
        "hardiness_min_c": None,
        "zones": [],
        "core_zones": [],
        "source": "catalog",
    }


def _climate_zone(precip: float | None, mean_t: float | None, tmin: float | None) -> str:
    if precip is None or mean_t is None:
        return "unknown"
    if tmin is not None and tmin >= 18 and precip >= 1000:
        return "tropical humid"
    if tmin is not None and tmin >= 15 and precip >= 700:
        return "subtropical humid"
    if precip < 200:
        moisture = "arid"
    elif precip < 400:
        moisture = "semi-arid"
    elif precip < 650:
        moisture = "Mediterranean (dry summer)"
    else:
        moisture = "humid temperate"
    if mean_t >= 22:
        thermal = "hot"
    elif mean_t >= 18:
        thermal = "warm"
    elif mean_t >= 13:
        thermal = "mild"
    elif mean_t >= 8:
        thermal = "cool"
    else:
        thermal = "cold"
    return f"{thermal} {moisture}"


def _zone_key(climate_zone: str | None) -> str | None:
    """Map the human-readable climate zone to a regional-crop zone key."""
    if not climate_zone or climate_zone == "unknown":
        return None
    zone = climate_zone.lower()
    if "tropical" in zone or "subtropical" in zone:
        return "tropical" if "tropical" in zone and "sub" not in zone else "warm_temperate"
    if "mediterranean" in zone:
        return "mediterranean"
    if "arid" in zone or "semi-arid" in zone:
        return "hot_arid" if ("hot" in zone or "warm" in zone) else "warm_temperate"
    if "temperate" in zone:
        if "cold" in zone:
            return "cold_continental"
        if "cool" in zone:
            return "cool_temperate"
        return "warm_temperate"
    return None


# --------------------------------------------------------------------------- #
# profile cache
# --------------------------------------------------------------------------- #

def _cache_path(lat: float, lon: float) -> Path:
    key = f"{lat:.4f}_{lon:.4f}".replace("-", "m")
    return PROFILE_DIR / f"profile_{key}.json"


def _read_cache(lat: float, lon: float, ttl: int) -> dict[str, Any] | None:
    payload = _load_json(_cache_path(lat, lon))
    if not payload:
        return None
    fetched = payload.get("data_quality", {}).get("fetched_at_epoch")
    if fetched is None or (time.time() - float(fetched)) > ttl:
        return None
    payload.setdefault("data_quality", {})["cached"] = True
    return payload


def _write_cache(lat: float, lon: float, payload: dict[str, Any]) -> None:
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(lat, lon)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# live ingestion
# --------------------------------------------------------------------------- #

def _fetch_sources(lat: float, lon: float, timeout_s: float) -> dict[str, Any]:
    """Fetch weather/soil/terrain in parallel. Each failure is isolated."""
    import requests

    from .ingestion.cache import Cache
    from .ingestion.nasa_power import NasaPowerAdapter
    from .ingestion.soilgrids import SoilGridsAdapter
    from .ingestion.srtm import SrtmAdapter
    from .preprocessing.geometry import build_point_field

    field = build_point_field(lat, lon, buffer_m=100.0, field_id=f"loc_{lat:.5f}_{lon:.5f}")
    cache = Cache(CACHE_ROOT)
    start, end = _recent_period(3)

    def run(adapter_cls, source_id, own_timeout):
        session = requests.Session()
        session.headers.update({"User-Agent": "agri-location-profile/1.0"})
        try:
            return adapter_cls(cache, session, timeout=own_timeout).fetch(field, start, end)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return {"__error__": f"{type(exc).__name__}: {exc}", "source_id": source_id}

    jobs = {
        "weather": (NasaPowerAdapter, timeout_s),
        "soil": (SoilGridsAdapter, timeout_s),
        "terrain": (SrtmAdapter, min(timeout_s, 60.0)),
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            key: pool.submit(run, cls, key, own_timeout)
            for key, (cls, own_timeout) in jobs.items()
        }
        for key, future in futures.items():
            try:
                results[key] = future.result(timeout=timeout_s + 15)
            except Exception as exc:  # noqa: BLE001
                results[key] = {"__error__": f"{type(exc).__name__}: {exc}", "source_id": key}

    # SoilGrids masks urban/water pixels.  If a town-centre coordinate returns
    # no values, retry a few kilometres away so the profile still gets soil.
    def _soil_has_values(result: Any) -> bool:
        metadata = getattr(result, "metadata", None) or {}
        return bool(metadata.get("properties"))

    if not _soil_has_values(results.get("soil")):
        for dlat, dlon in ((0.06, 0.0), (0.0, 0.06), (-0.06, 0.0), (0.0, -0.06)):
            shifted = build_point_field(
                lat + dlat,
                lon + dlon,
                buffer_m=100.0,
                field_id=f"loc_{lat + dlat:.5f}_{lon + dlon:.5f}",
            )
            # run() closes over the original field; sample a shifted point directly.
            session = requests.Session()
            session.headers.update({"User-Agent": "agri-location-profile/1.0"})
            try:
                candidate = SoilGridsAdapter(cache, session, timeout=min(timeout_s, 25.0)).fetch(
                    shifted, start, end
                )
            except Exception as exc:  # noqa: BLE001
                candidate = {"__error__": f"{type(exc).__name__}: {exc}", "source_id": "soil"}
            if isinstance(candidate, dict):
                continue
            if _soil_has_values(candidate):
                candidate.metadata = {
                    **(candidate.metadata or {}),
                    "fallback_offset_deg": {"lat": round(dlat, 2), "lon": round(dlon, 2)},
                }
                results["soil"] = candidate
                break

    results["start"] = start
    results["end"] = end
    return results


def _build_from_sources(lat: float, lon: float, sources: dict[str, Any], irrigation_mm: float) -> dict[str, Any]:
    from .environmental.context import build_context

    def usable(key: str):
        value = sources.get(key)
        if value is None:
            return None
        if isinstance(value, dict):
            return None if "__error__" in value else value
        # Adapters return SourceResult objects; build_context needs them intact.
        if hasattr(value, "observations"):
            has_values = bool(value.observations) or bool(
                (getattr(value, "metadata", {}) or {}).get("properties")
            )
            return value if has_values else None
        return None

    weather = usable("weather")
    soil = usable("soil")
    terrain = usable("terrain")

    context, frame = build_context(
        weather,
        soil,
        terrain,
        lat=lat,
        start=sources["start"],
        end=sources["end"],
        seasons=[],
    )

    years = max(
        (
            datetime.fromisoformat(sources["end"]).date()
            - datetime.fromisoformat(sources["start"]).date()
        ).days / 365.25,
        1.0,
    )

    frost_days = heat_days = None
    cold_extreme = heat_extreme = None
    try:
        import pandas as pd  # local import keeps module import cheap

        if frame is not None and not frame.empty:
            if "T2M_MIN" in frame and frame["T2M_MIN"].notna().any():
                frost_days = float((frame["T2M_MIN"] < 0).sum() / years)
                # 1st percentile is a robust "extreme cold" proxy; the absolute
                # minimum is dominated by one-off events.
                cold_extreme = float(frame["T2M_MIN"].quantile(0.01))
            if "T2M_MAX" in frame and frame["T2M_MAX"].notna().any():
                heat_days = float((frame["T2M_MAX"] >= 35).sum() / years)
                heat_extreme = float(frame["T2M_MAX"].quantile(0.99))
    except Exception:
        pass

    weather_info = context.weather or {}
    soil_info = context.soil or {}
    terrain_info = context.terrain or {}

    annual_precip = _round(weather_info.get("annual_precip_mm"))
    mean_t = _round(weather_info.get("mean_temp_c"))
    mean_tmin = _round(weather_info.get("mean_tmin_c"))
    mean_tmax = _round(weather_info.get("mean_tmax_c"))

    monthly = weather_info.get("monthly_climatology") or []
    warm_temps = [
        float(m["temp_c"])
        for m in monthly
        if isinstance(m, dict) and m.get("temp_c") is not None and float(m["temp_c"]) >= 10.0
    ]
    warm_season_mean = _round(sum(warm_temps) / len(warm_temps)) if warm_temps else mean_t

    try:
        scoring = _load_json(SCORING_PATH) or {}
        effective_fraction = float(scoring.get("effective_rain_fraction", 0.7))
    except Exception:
        effective_fraction = 0.7

    effective_rain = _round((annual_precip or 0.0) * effective_fraction)

    climate = {
        "period": {"start": sources["start"], "end": sources["end"], "years": round(years, 1)},
        "annual_precip_mm": annual_precip,
        "effective_rain_mm": effective_rain,
        "mean_temp_c": mean_t,
        "mean_tmin_c": mean_tmin,
        "mean_tmax_c": mean_tmax,
        "warm_season_mean_c": warm_season_mean,
        "frost_days_per_year": _round(frost_days),
        "heat_stress_days_ge35c_per_year": _round(heat_days),
        "cold_extreme_min_c": _round(cold_extreme),
        "heat_extreme_max_c": _round(heat_extreme),
        "et0_mm_per_year": _round(weather_info.get("et0_hargreaves_mm_per_year")),
        "aridity_index": weather_info.get("aridity_index"),
        "aridity_class": weather_info.get("aridity_class"),
        "gdd_base5_c_days_per_year": _round(weather_info.get("gdd_base5_c_days_per_year")),
        "growing_season_days": _round(weather_info.get("available_season_days")),
        "climate_zone": _climate_zone(annual_precip, mean_t, mean_tmin),
        "frost_free": (frost_days or 0.0) == 0.0,
        "monthly_climatology": weather_info.get("monthly_climatology", []),
        "spatial_representativeness": weather_info.get("spatial_representativeness"),
    }

    soil_out = {
        "available": bool(soil_info.get("available")),
        "source": soil_info.get("source"),
        "ph": _round(soil_info.get("ph"), 2),
        "texture_class": soil_info.get("texture_class"),
        "clay_pct": _round(soil_info.get("clay_pct")),
        "sand_pct": _round(soil_info.get("sand_pct")),
        "silt_pct": _round(soil_info.get("silt_pct")),
        "soc_g_kg": _round(soil_info.get("soc_g_kg"), 2),
        "nitrogen_g_kg": _round(soil_info.get("nitrogen_g_kg"), 3),
        "depth": soil_info.get("depth"),
        "sampled_offset_deg": (getattr(soil, "metadata", None) or {}).get("fallback_offset_deg")
        if soil is not None
        else None,
        "spatial_representativeness": soil_info.get("spatial_representativeness"),
    }
    terrain_out = {
        "available": bool(terrain_info.get("available")),
        "source": terrain_info.get("source"),
        "elevation_m": _round(terrain_info.get("elevation_m")),
        "slope_pct": _round(terrain_info.get("slope_pct")),
        "spatial_representativeness": terrain_info.get("spatial_representativeness"),
    }

    status = {
        "weather": "ok" if weather else "unavailable",
        "soil": "ok" if soil else "unavailable",
        "terrain": "ok" if terrain else "unavailable",
    }
    errors = {}
    for key, value in sources.items():
        if isinstance(value, dict) and "__error__" in value:
            errors[key] = value["__error__"]
        elif hasattr(value, "status") and getattr(value.status, "status", "") == "error":
            errors[key] = getattr(value.status, "message", "unknown source error")
    if not weather:
        climate["climate_zone"] = "unknown"

    return {
        "location": {"lat": lat, "lon": lon},
        "climate": climate,
        "soil": soil_out,
        "terrain": terrain_out,
        "irrigation": {
            "assumed_available": irrigation_mm > 0,
            "additional_mm_per_year": _round(irrigation_mm) or 0.0,
        },
        "data_quality": {
            "status": status,
            "errors": errors,
            "completeness": context.data_completeness,
            "missing": context.missing,
            "caveats": context.caveats,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "fetched_at_epoch": time.time(),
            "cached": False,
            "precedence": (
                "Measured/modelled data for the provided coordinates. Treat this as "
                "the authoritative location context and prefer it over caller-supplied "
                "estimates when they disagree."
            ),
            "disclaimer": (
                "Grid-cell weather, 250 m predicted soil, ~90 m DEM. Not a field "
                "measurement or soil test."
            ),
        },
    }


# --------------------------------------------------------------------------- #
# suitability
# --------------------------------------------------------------------------- #

def _verdict(score: float) -> str:
    if score >= 70:
        return "suitable"
    if score >= 45:
        return "marginal"
    return "not_suitable"


def _tropical_gate(crop_name: str, profile: dict[str, Any]) -> list[str]:
    """Hard physical gates for well-known tropical crops."""
    key = _norm(crop_name)
    traits = None
    for name, t in _TROPICAL_TRAITS.items():
        if name in key:
            traits = t
            break
    if not traits:
        return []

    climate = profile.get("climate") or {}
    reasons: list[str] = []
    frost = climate.get("frost_days_per_year")
    mean_t = climate.get("mean_temp_c")
    tmin = climate.get("mean_tmin_c")

    if frost is not None and frost > traits["max_frost_days"]:
        reasons.append(
            f"frost: {frost:.0f} frost days/yr vs a tolerance of "
            f"{traits['max_frost_days']:.0f} — exposed to lethal cold"
        )
    if mean_t is not None and mean_t < traits["min_mean_temp_c"]:
        reasons.append(
            f"heat: mean {mean_t:.1f}°C is below the {traits['min_mean_temp_c']:.0f}°C "
            "needed for reliable growth"
        )
    if tmin is not None and tmin < traits["min_tmin_c"]:
        reasons.append(
            f"cold nights: mean minimum {tmin:.1f}°C is below the "
            f"{traits['min_tmin_c']:.0f}°C threshold"
        )

    water_needed = traits["water_demand_mm"][0]
    available = (climate.get("effective_rain_mm") or 0.0) + (
        (profile.get("irrigation") or {}).get("additional_mm_per_year") or 0.0
    )
    if available < water_needed:
        reasons.append(
            f"water: ~{available:.0f} mm available vs a ~{water_needed:.0f} mm "
            "minimum demand — irrigation would be required"
        )
    return reasons


def _score_crop(
    crop_name: str,
    profile: dict[str, Any],
    catalog_lookup: dict[str, dict[str, Any]],
    regional_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Score one crop against the location profile.

    Hard physical limits (lethal frost, insufficient heat, excessive heat) cap
    the verdict regardless of management.  A water deficit is *not* treated as
    lethal: it is reported as "irrigation required" and the crop becomes
    conditional instead of being silently recommended.
    """
    traits = _traits_for(crop_name, catalog_lookup, regional_lookup)
    catalog_crop = _find_catalog_crop(crop_name, catalog_lookup)
    climate = profile.get("climate") or {}
    soil = profile.get("soil") or {}
    irrigation = profile.get("irrigation") or {}

    rain = (climate.get("effective_rain_mm") or 0.0) + (
        irrigation.get("additional_mm_per_year") or 0.0
    )
    irrigation_assumed = bool(irrigation.get("assumed_available"))

    reasons: list[str] = []
    parts: list[tuple[float, float]] = []  # (score 0-1, weight)
    gate_codes: list[str] = []
    irrigation_required = False

    if traits:
        warm_mean = climate.get("warm_season_mean_c")
        mean_t = climate.get("mean_temp_c")
        tmin = climate.get("mean_tmin_c")
        tmax = climate.get("mean_tmax_c")
        cold_extreme = climate.get("cold_extreme_min_c")
        min_mean = traits.get("min_mean_temp_c")
        min_tmin = traits.get("min_tmin_c")
        max_mean = traits.get("max_mean_temp_c")
        lifecycle = traits.get("lifecycle") or "annual"
        hardiness = traits.get("hardiness_min_c")

        # Growing-season heat requirement.  Annual mean temperature badly
        # understates continental summers, so prefer the warm-season mean.
        growth_temp = warm_mean if warm_mean is not None else mean_t
        if growth_temp is not None and min_mean is not None and growth_temp < min_mean:
            deficit = min_mean - growth_temp
            reasons.append(
                f"cold: warm-season mean {growth_temp:.1f}°C is below the "
                f"{min_mean:.0f}°C needed for reliable growth"
            )
            gate_codes.append("cold_severe" if deficit >= 5 else "cold")
        if tmin is not None and min_tmin is not None and tmin < min_tmin:
            reasons.append(
                f"cold nights: mean minimum {tmin:.1f}°C is below the "
                f"{min_tmin:.0f}°C threshold"
            )
            if "cold_severe" not in gate_codes:
                gate_codes.append("cold")

        # Perennials must survive winter extremes.  Annual field crops are
        # planted after the last frost, so annual frost-day counts are not a
        # hard gate for them (their limit is the warm-season temperature above).
        if lifecycle == "perennial":
            limit = hardiness
            if limit is None and min_tmin is not None:
                limit = min_tmin - 10
            if cold_extreme is not None and limit is not None and cold_extreme < limit:
                reasons.append(
                    f"frost: winter extreme {cold_extreme:.1f}°C is below this "
                    f"perennial's hardiness limit of {limit:.0f}°C"
                )
                gate_codes.append("frost_severe")

        if tmax is not None and max_mean is not None and tmax > max_mean:
            reasons.append(
                f"heat: mean daily max {tmax:.1f}°C is above the {max_mean:.0f}°C "
                "comfort limit for this crop"
            )
            gate_codes.append("heat")

        demand = traits.get("water_demand_mm")
        if isinstance(demand, list) and len(demand) == 2:
            low, high = float(demand[0]), float(demand[1])
            if low <= 0:
                water_score = 1.0
            elif rain >= low:
                water_score = 1.0
                reasons.append(
                    f"water: ~{rain:.0f} mm available covers the "
                    f"{low:.0f}-{high:.0f} mm demand"
                )
            elif rain >= 0.7 * low:
                water_score = 0.6
                reasons.append(
                    f"water: ~{rain:.0f} mm is below the {low:.0f}-{high:.0f} mm "
                    "demand (partial deficit)"
                )
            else:
                irrigation_required = True
                if irrigation_assumed:
                    water_score = 0.85
                    reasons.append(
                        f"water: rainfall ~{rain:.0f} mm is below the "
                        f"{low:.0f}-{high:.0f} mm demand, but irrigation is "
                        "assumed available"
                    )
                else:
                    water_score = max(0.03, 0.35 * rain / max(low, 1.0))
                    reasons.append(
                        f"water: ~{rain:.0f} mm is far below the "
                        f"{low:.0f}-{high:.0f} mm demand (irrigation required)"
                    )
            parts.append((water_score, 0.45))

        ph = soil.get("ph")
        ph_range = traits.get("ph_range")
        if ph is not None and isinstance(ph_range, list) and len(ph_range) == 2:
            lo, hi = float(ph_range[0]), float(ph_range[1])
            if lo <= ph <= hi:
                ph_score = 1.0
                reasons.append(f"soil pH {ph:.1f} is inside the {lo:.1f}-{hi:.1f} range")
            elif lo - 0.5 <= ph <= hi + 0.5:
                ph_score = 0.5
                reasons.append(f"soil pH {ph:.1f} is just outside the {lo:.1f}-{hi:.1f} range")
            else:
                ph_score = 0.1
                reasons.append(f"soil pH {ph:.1f} is outside the {lo:.1f}-{hi:.1f} range")
            parts.append((ph_score, 0.25))

        texture = soil.get("texture_class")
        textures = traits.get("textures") or []
        if texture and textures:
            if texture in textures:
                tex_score = 1.0
                reasons.append(f"soil texture '{texture}' is in the preferred list")
            else:
                tex_score = 0.55
                reasons.append(
                    f"soil texture '{texture}' is not preferred "
                    f"({', '.join(textures)})"
                )
            parts.append((tex_score, 0.15))

        drought = traits.get("drought_tolerance")
        if isinstance(drought, (int, float)):
            # Drought tolerance only matters where water is actually scarce.
            # In a 750 mm rainfed climate it should not decide the ranking.
            aridity_weight = max(0.0, min(0.10, 0.10 * (1.0 - rain / 650.0)))
            if aridity_weight > 0.01:
                parts.append((float(drought), aridity_weight))

        # Regional typicality: a crop can be physically possible yet not what
        # farmers actually grow in this climate zone.  "Core" crops get the
        # full reward; merely permitted crops get a smaller one.
        zones = traits.get("zones") or []
        core_zones = traits.get("core_zones") or []
        location_zone = _zone_key(climate.get("climate_zone"))
        if zones and location_zone:
            human_zone = location_zone.replace("_", " ")
            if location_zone in core_zones:
                zone_score = 1.0
                reasons.append(f"zone: a core crop for {human_zone} climates")
            elif location_zone in zones:
                zone_score = 0.6
                reasons.append(f"zone: grown in {human_zone} climates, but not a staple")
            else:
                zone_score = 0.2
                reasons.append(
                    f"zone: possible here, but not a typical {human_zone} crop"
                )
            parts.append((zone_score, 0.22))
    else:
        # Unknown crop: keep the conservative tropical gates as a safety net.
        gates = _tropical_gate(crop_name, profile)
        reasons.extend(gates)
        if any(r.startswith(("frost:", "cold nights:", "heat:")) for r in gates):
            gate_codes.append("frost_severe")
        elif gates:
            gate_codes.append("water")
            irrigation_required = True

    if parts:
        total_weight = sum(w for _, w in parts)
        fit: float | None = 100.0 * sum(s * w for s, w in parts) / total_weight
    else:
        fit = None

    # Cap the verdict by the most serious limit found.
    if "frost_severe" in gate_codes or "cold_severe" in gate_codes:
        fit = min(fit, 15.0) if fit is not None else 12.0
    elif "frost" in gate_codes or "cold" in gate_codes:
        fit = min(fit, 40.0) if fit is not None else 38.0
    elif "heat" in gate_codes:
        fit = min(fit, 55.0) if fit is not None else 50.0
    elif irrigation_required:
        # A water deficit is a management cost, not a hard ceiling.  Keep the
        # relative ranking, but do not let an unirrigated crop read as suitable.
        ceiling = 82.0 if irrigation_assumed else 68.0
        fit = min(fit, ceiling) if fit is not None else 55.0

    verdict = _verdict(fit) if fit is not None else "unknown"
    frost_sensitive = any(code.startswith(("frost", "cold")) for code in gate_codes)

    return {
        "crop": crop_name,
        "catalog_match": (traits or {}).get("common_name")
        or (catalog_crop or {}).get("common_name"),
        "group": (traits or {}).get("group"),
        "traits_source": (traits or {}).get("source"),
        "fit_score": round(fit, 1) if fit is not None else None,
        "verdict": verdict,
        "reasons": reasons,
        "irrigation_required": irrigation_required,
        "frost_sensitive": frost_sensitive,
        "limits": sorted(set(gate_codes)),
    }


def assess_crops_for_location(
    crop_list: list[str] | None,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Score each farmer/candidate crop against the location profile."""
    if not crop_list:
        return []
    catalog_lookup = _catalog_lookup()
    regional_lookup = _regional_lookup()
    return [
        _score_crop(name, profile, catalog_lookup, regional_lookup)
        for name in crop_list
    ]


def rank_crops_for_location(
    profile: dict[str, Any],
    *,
    limit: int = 15,
    include: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rank every crop in the regional knowledge base for this location.

    Returns the best-fit crops first, with the same evidence fields as
    ``assess_crops_for_location``.  This is what makes Jericho and Russia get
    different recommendations from the same code path.
    """
    catalog_lookup = _catalog_lookup()
    regional_lookup = _regional_lookup()
    seen: dict[str, dict[str, Any]] = {}
    for payload in regional_lookup.values():
        crop_id = payload.get("id") or payload.get("common_name") or ""
        if not crop_id or crop_id in seen:
            continue
        seen[crop_id] = payload

    scored = [
        _score_crop(
            payload.get("common_name") or crop_id,
            profile,
            catalog_lookup,
            regional_lookup,
        )
        for crop_id, payload in seen.items()
        if (payload.get("group") or "") != "fallow"
    ]
    if include:
        existing = {_norm(item["crop"]) for item in scored}
        for name in include:
            if _norm(name) not in existing:
                scored.append(_score_crop(name, profile, catalog_lookup, regional_lookup))

    order = {"suitable": 0, "marginal": 1, "not_suitable": 2, "unknown": 3}

    def sort_key(item: dict[str, Any]):
        return (
            order.get(item.get("verdict") or "unknown", 3),
            -(item.get("fit_score") or 0.0),
            (item.get("catalog_match") or item.get("crop") or ""),
        )

    scored.sort(key=sort_key)
    return scored[: max(1, int(limit))]


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #

def build_location_profile(
    lat: float,
    lon: float,
    *,
    irrigation_mm: float = 0.0,
    force_refresh: bool = False,
    timeout_s: float = 60.0,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Return the environmental profile for a point, using cache when fresh."""
    lat = float(lat)
    lon = float(lon)
    ttl = PROFILE_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)

    with _CACHE_LOCK:
        if not force_refresh:
            cached = _read_cache(lat, lon, ttl)
            if cached is not None:
                if irrigation_mm > 0:
                    cached.setdefault("irrigation", {})["assumed_available"] = True
                    cached["irrigation"]["additional_mm_per_year"] = _round(irrigation_mm) or 0.0
                return cached

    sources = _fetch_sources(lat, lon, timeout_s)
    profile = _build_from_sources(lat, lon, sources, irrigation_mm)

    with _CACHE_LOCK:
        _write_cache(lat, lon, profile)
    return profile
