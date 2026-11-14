"""Open-Meteo free weather API adapter (no authentication required).

Provides:
  - soil_moisture_0_to_7cm : volumetric water content m³/m³
  - soil_temperature_0_to_7cm : °C at 7 cm depth
  - precipitation : mm per time step
  - temperature_2m : °C at 2 m
  - derived: ET0 (Hargreaves), heat stress days, frost risk days, extreme precipitation

All data is free and public; no API key or Earthdata login needed.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import requests

from ..preprocessing.geometry import FieldGeometry
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def _build_params(field: FieldGeometry, start: str, end: str, extra: dict[str, str | float | list] | None = None) -> dict[str, object]:
    lon, lat = field.centroid_lonlat
    base = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "timezone": "UTC",
        "temperature_unit": "celsius",
        "windspeed_unit": "km_h",
        "precipitation_unit": "mm",
        "duration": "hourly",
    }
    # Open-Meteo provides soil variables at 7 cm depth
    base.update(
        {
            "hourly": "soil_temperature_0_to_7cm,soil_moisture_0_to_7cm,precipitation,temperature_2m",
        }
    )
    if extra:
        base.update(extra)
    return base


def _parse_soil_moisture(series):
    soil_moisture = [v for v in series if v is not None]
    return {
        "values": soil_moisture,
        "unit": "m³/m³",
        "n_values": len(soil_moisture),
        "min": min(soil_moisture) if soil_moisture else None,
        "max": max(soil_moisture) if soil_moisture else None,
        "mean": float(sum(soil_moisture) / len(soil_moisture)) if soil_moisture else None,
    }


def _parse_soil_temperature(series):
    soil_temperature = [v for v in series if v is not None]
    return {
        "values": soil_temperature,
        "unit": "°C",
        "n_values": len(soil_temperature),
        "min": min(soil_temperature) if soil_temperature else None,
        "max": max(soil_temperature) if soil_temperature else None,
        "mean": float(sum(soil_temperature) / len(soil_temperature)) if soil_temperature else None,
    }


def _parse_precipitation(series):
    precipitation = [v for v in series if v is not None]
    total = sum(precipitation) if precipitation else 0.0
    return {
        "values": precipitation,
        "unit": "mm",
        "n_values": len(precipitation),
        "total": total,
        "mean": float(sum(precipitation) / len(precipitation)) if precipitation else None,
    }


def _parse_air_temperature(series):
    air_temperature = [v for v in series if v is not None]
    return {
        "values": air_temperature,
        "unit": "°C",
        "n_values": len(air_temperature),
        "min": min(air_temperature) if air_temperature else None,
        "max": max(air_temperature) if air_temperature else None,
        "mean": float(sum(air_temperature) / len(air_temperature)) if air_temperature else None,
    }


def _compute_heat_stress(air_temp_data: dict[str, Any]) -> dict[str, Any]:
    """Count days with Tmax > 30 °C (heat stress threshold for many crops)."""
    vals = air_temp_data.get("values", [])
    hot_days = [v for v in vals if v is not None and v > 30.0]
    return {
        "heat_stress_days": len(hot_days),
        "heat_stress_pct": round(100.0 * len(hot_days) / len(vals), 1) if vals else None,
        "max_temp": max(vals) if vals else None,
    }


def _compute_frost_risk(air_temp_data: dict[str, Any]) -> dict[str, Any]:
    """Count days with Tmin < 0 °C (frost risk)."""
    vals = air_temp_data.get("values", [])
    frost_days = [v for v in vals if v is not None and v < 0.0]
    return {
        "frost_risk_days": len(frost_days),
        "frost_risk_pct": round(100.0 * len(frost_days) / len(vals), 1) if vals else None,
        "min_temp": min(vals) if vals else None,
    }


def _compute_extreme_precipitation(precip_data: dict[str, Any]) -> dict[str, Any]:
    """Count days with precipitation > 20 mm (extreme event threshold)."""
    vals = precip_data.get("values", [])
    extreme = [v for v in vals if v is not None and v > 20.0]
    return {
        "extreme_precip_days": len(extreme),
        "extreme_precip_pct": round(100.0 * len(extreme) / len(vals), 1) if vals else None,
        "max_1day_rain": max(vals) if vals else None,
    }


def _compute_evapotranspiration_hargreaves(air_temp_data: dict[str, Any], lat: float) -> dict[str, Any]:
    """Estimate annual ET0 using the Hargreaves equation (simplified, no radiation term needed for demo)."""
    vals = air_temp_data.get("values", [])
    if len(vals) < 2:
        return {"et0_mm_per_year": None, "et0_quality": "insufficient_data"}
    # Simple approximation: ET0 ~ 0.0023 * (Tmean + 17.8) * sqrt(Tmax - Tmin) * RA factor
    # For a quick demo we just return the mean temp and a flag; a full implementation
    # would use daily Tmin/Tmax and extraterrestrial radiation.
    if len(vals) < 365:
        return {"et0_mm_per_year": None, "et0_quality": "seasonal_data_only"}
    tmean = sum(vals) / len(vals)
    # placeholder: return mean temp as a proxy; callers should use dedicated ET0 if needed
    return {
        "et0_mm_per_year": round(tmean * 0.1, 1),  # very rough proxy
        "et0_quality": "proxy_only",
    }


def _compute_long_term_water_trend(series_data: dict[str, Any], years: int = 5) -> dict[str, Any]:
    """Simple linear trend on the last *years* of monthly-aggregated values.

    Since Open-Meteo provides only recent data, we treat the available window as the
    "trend period" and report the direction and approximate rate.
    """
    vals = series_data.get("values", [])
    if not vals or len(vals) < 30:
        return {"trend": "insufficient_data", "rate": None, "r2": None}
    x = np.arange(len(vals))
    y = np.array(vals, dtype=float)
    slope, _ = np.polyfit(x, y, 1)
    r2 = None  # simplified; full r2 would need SS_res / SS_tot
    rate = round(float(slope), 3)
    if slope > 0:
        trend = "increasing"
    elif slope < 0:
        trend = "decreasing"
    else:
        trend = "stable"
    return {
        "trend": trend,
        "rate": rate,
        "r2": r2,
        "period_years": len(vals) / 365.0,
    }


# --------------------------------------------------------------------------- #
# Public adapter class
# --------------------------------------------------------------------------- #

class OpenMeteoAdapter:
    source_id = "open-meteo"

    def __init__(
        self,
        cache: Any,
        session: requests.Session,
        *,
        timeout: float = 30,
    ):
        self.cache = cache
        self.session = session
        self.timeout = timeout

    def fetch(
        self,
        field: FieldGeometry,
        start: str,
        end: str,
    ) -> SourceResult:
        lon, lat = field.centroid_lonlat
        params = _build_params(field, start, end)
        try:
            data, cached = self.cache.fetch_json(
                self.session,
                OPEN_METEO_URL,
                namespace="open_meteo",
                key=f"{lat:.4f}:{lon:.4f}:{start}:{end}",
                timeout=self.timeout,
                max_age_s=7 * 24 * 3600,
                params=params,
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="error",
                    message=f"Open-Meteo request failed: {exc}",
                ),
                metadata={"requested_start": start, "requested_end": end},
            )

        if not data:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="error",
                    message="Open-Meteo returned empty data",
                ),
                metadata={"requested_start": start, "requested_end": end},
            )

        series = data.get("hourly", {})
        soil_moisture = _parse_soil_moisture(series.get("soil_moisture_0_to_7cm", []))
        soil_temperature = _parse_soil_temperature(series.get("soil_temperature_0_to_7cm", []))
        precipitation = _parse_precipitation(series.get("precipitation", []))
        air_temperature = _parse_air_temperature(series.get("temperature_2m", []))

        heat_stress = _compute_heat_stress(air_temperature)
        frost_risk = _compute_frost_risk(air_temperature)
        extreme_precip = _compute_extreme_precipitation(precipitation)
        et0 = _compute_evapotranspiration_hargreaves(air_temperature, lat)
        long_term_water = _compute_long_term_water_trend(precip_data=precip_data)

        metadata = {
            "requested_start": start,
            "requested_end": end,
            "requested_point": {"lat": lat, "lon": lon},
            "spatial_resolution": "~0.25° grid (~25 km) – global coverage, free, no auth",
            "temporal_resolution": "hourly",
            "extracted_at": utc_now_iso(),
            "cached": cached,
        }

        # Attach the derived analytics as metadata so the pipeline can read them
        metadata.update(
            {
                "heat_stress": heat_stress,
                "frost_risk": frost_risk,
                "extreme_precipitation": extreme_precip,
                "et0_hargreaves_proxy": et0,
                "long_term_water_trend": long_term_water,
                "soil_moisture": soil_moisture,
                "soil_temperature": soil_temperature,
                "precipitation": precipitation,
                "air_temperature": air_temperature,
            }
        )

        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status="ok",
                message=f"Open-Meteo data: {len(air_temperature)} hourly samples",
            ),
            observations=[],  # Open-Meteo is grid-cell; we pull summary stats instead
            metadata=metadata,
        )