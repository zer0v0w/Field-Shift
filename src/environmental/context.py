"""Turn environmental source results into an analysis-ready context object.

The context is deliberately explicit about what is measured, what is modelled,
and what is missing.  Rotation scoring consumes this object and downgrades its
confidence when inputs are absent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..ingestion.base import SourceResult
from ..ingestion.nasa_power import NasaPowerAdapter

WEATHER_VARIABLES = [
    "T2M",
    "T2M_MIN",
    "T2M_MAX",
    "PRECTOTCORR",
    "ALLSKY_SFC_SW_DWN",
    "RH2M",
    "WS2M",
]


@dataclass
class EnvironmentalContext:
    weather: dict[str, Any] = field(default_factory=dict)
    soil: dict[str, Any] = field(default_factory=dict)
    terrain: dict[str, Any] = field(default_factory=dict)
    data_completeness: float = 0.0
    missing: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weather": self.weather,
            "soil": self.soil,
            "terrain": self.terrain,
            "data_completeness": round(self.data_completeness, 3),
            "missing": self.missing,
            "caveats": self.caveats,
        }


def _weather_frame(result: SourceResult) -> pd.DataFrame:
    if not result.observations:
        return pd.DataFrame(columns=WEATHER_VARIABLES)
    rows = [
        {"date": o.date, "variable": o.variable, "value": o.value}
        for o in result.observations
        if o.variable in WEATHER_VARIABLES
    ]
    if not rows:
        return pd.DataFrame(columns=WEATHER_VARIABLES)
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    pivot = frame.pivot_table(index="date", columns="variable", values="value", aggfunc="mean")
    pivot = pivot.sort_index()
    for column in WEATHER_VARIABLES:
        if column not in pivot:
            pivot[column] = np.nan
    return pivot[WEATHER_VARIABLES]


def _extraterrestrial_radiation(lat: float, doy: np.ndarray) -> np.ndarray:
    """FAO-56 extraterrestrial radiation, MJ m-2 day-1."""
    phi = math.radians(lat)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365.0)
    delta = 0.409 * np.sin(2 * np.pi * doy / 365.0 - 1.39)
    argument = np.clip(-np.tan(phi) * np.tan(delta), -1, 1)
    omega = np.arccos(argument)
    return (
        (24 * 60 / np.pi)
        * 0.0820
        * dr
        * (omega * np.sin(phi) * np.sin(delta) + np.cos(phi) * np.cos(delta) * np.sin(omega))
    )


def _hargreaves_et0(frame: pd.DataFrame, lat: float) -> pd.Series:
    if frame.empty or "T2M" not in frame:
        return pd.Series(dtype=float)
    doy = frame.index.day_of_year.to_numpy()
    ra = _extraterrestrial_radiation(lat, doy)
    tmean = frame["T2M"].to_numpy(dtype=float)
    tmin = frame["T2M_MIN"].to_numpy(dtype=float)
    tmax = frame["T2M_MAX"].to_numpy(dtype=float)
    spread = np.clip(tmax - tmin, 0, None)
    # FAO-56 Hargreaves: ET0 = 0.0023 (Tmean + 17.8) sqrt(Tmax - Tmin) Ra,
    # where Ra is in mm/day.  Convert the MJ m-2 day-1 radiation by 0.408.
    et0 = 0.0023 * 0.408 * (tmean + 17.8) * np.sqrt(spread) * ra
    return pd.Series(np.clip(et0, 0, None), index=frame.index, name="ET0_mm_day")


def _season_windows(seasons: list[dict[str, Any]], start: str, end: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for season in seasons:
        s = season.get("approx_season_start") or season.get("start")
        e = season.get("approx_season_end") or season.get("end")
        if s and e:
            try:
                windows.append((pd.Timestamp(s), pd.Timestamp(e)))
            except Exception:
                continue
    if not windows:
        # Mediterranean default: November to May.
        first = pd.Timestamp(start)
        last = pd.Timestamp(end)
        for year in range(first.year, last.year + 1):
            s = pd.Timestamp(f"{year}-11-01")
            e = pd.Timestamp(f"{year + 1}-05-31")
            if s <= last and e >= first:
                windows.append((max(s, first), min(e, last)))
    return windows


def _texture_class(clay: float | None, sand: float | None, silt: float | None) -> str | None:
    if clay is None or sand is None or silt is None:
        return None
    # Approximate USDA texture classes; adequate for a coarse context flag.
    if clay >= 40 and sand <= 45 and silt <= 40:
        return "clay"
    if clay >= 35 and sand > 45:
        return "sandy clay"
    if clay >= 35 and silt > 40:
        return "silty clay"
    if 27 <= clay < 40 and 20 <= sand <= 45:
        return "clay loam"
    if 27 <= clay < 40 and sand < 20:
        return "silty clay loam"
    if 20 <= clay < 35 and silt >= 40:
        return "silt loam"
    if 7 <= clay < 27 and 43 <= sand <= 85:
        return "loam" if silt < 40 else "silt loam"
    if clay < 20 and sand >= 70:
        return "sandy loam" if silt < 30 else "loam"
    if clay < 15 and silt >= 80:
        return "silt"
    if sand >= 85:
        return "sand"
    return "loam"


def _aridity_class(index: float | None) -> str | None:
    if index is None:
        return None
    if index < 0.05:
        return "hyper-arid"
    if index < 0.20:
        return "arid"
    if index < 0.50:
        return "semi-arid"
    if index < 0.65:
        return "dry sub-humid"
    return "sub-humid / humid"


def build_context(
    weather_result: SourceResult | None,
    soil_result: SourceResult | None,
    terrain_result: SourceResult | None,
    *,
    lat: float,
    start: str,
    end: str,
    seasons: list[dict[str, Any]] | None = None,
) -> tuple[EnvironmentalContext, pd.DataFrame]:
    context = EnvironmentalContext()
    frame = _weather_frame(weather_result) if weather_result else pd.DataFrame()
    synthetic = bool(weather_result and weather_result.source_id.startswith("mock"))

    # Weather
    if not frame.empty:
        expected_days = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
        present_days = int(frame["T2M"].notna().sum()) if "T2M" in frame else 0
        completeness = min(1.0, present_days / max(expected_days, 1))
        windows = _season_windows(seasons or [], start, end)
        in_season = pd.Series(False, index=frame.index)
        for s, e in windows:
            in_season |= (frame.index >= s) & (frame.index <= e)
        precip = frame["PRECTOTCORR"].fillna(0)
        et0 = _hargreaves_et0(frame, lat)
        years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1 / 365.25)
        annual_precip = float(precip.sum() / years)
        season_precip = float(precip[in_season].sum() / max(len(windows), 1))
        et0_total = float(et0.sum() / years) if not et0.empty else None
        aridity = (annual_precip / et0_total) if et0_total and et0_total > 0 else None
        gdd = float(
            np.clip(
                ((frame["T2M_MAX"] + frame["T2M_MIN"]) / 2.0) - 5.0,
                0,
                None,
            ).sum()
            / years
        )
        season_lengths = [max((e - s).days, 1) for s, e in windows]
        available_season_days = (
            float(np.median(season_lengths)) if season_lengths else 180.0
        )
        monthly = (
            frame.assign(month=frame.index.month)
            .groupby("month")
            .agg(
                precip_mm=("PRECTOTCORR", "sum"),
                temp_c=("T2M", "mean"),
                solar_mj=("ALLSKY_SFC_SW_DWN", "mean"),
                rh_pct=("RH2M", "mean"),
            )
            .reset_index()
        )
        context.weather = {
            "available": True,
            "source": weather_result.source_id if weather_result else None,
            "requested_start": start,
            "requested_end": end,
            "days_present": present_days,
            "days_expected": expected_days,
            "completeness": round(completeness, 3),
            "annual_precip_mm": round(annual_precip, 1),
            "growing_season_precip_mm": round(season_precip, 1),
            "growing_season_windows": [
                {"start": s.date().isoformat(), "end": e.date().isoformat()} for s, e in windows
            ],
            "available_season_days": round(available_season_days, 1),
            "mean_temp_c": round(float(frame["T2M"].mean()), 2),
            "mean_tmin_c": round(float(frame["T2M_MIN"].mean()), 2),
            "mean_tmax_c": round(float(frame["T2M_MAX"].mean()), 2),
            "gdd_base5_c_days_per_year": round(gdd, 1),
            "et0_hargreaves_mm_per_year": round(et0_total, 1) if et0_total else None,
            "aridity_index": round(aridity, 3) if aridity is not None else None,
            "aridity_class": _aridity_class(aridity),
            "mean_solar_mj_m2_day": round(float(frame["ALLSKY_SFC_SW_DWN"].mean()), 2),
            "mean_rh_pct": round(float(frame["RH2M"].mean()), 1),
            "mean_wind_ms": round(float(frame["WS2M"].mean()), 2),
            "monthly_climatology": monthly.round(2).to_dict(orient="records"),
            "spatial_representativeness": (
                "SYNTHETIC generator — not a real weather grid cell"
                if synthetic
                else "NASA POWER grid cell (~55 x 60 km), not a field station"
            ),
            "method_note": (
                "SYNTHETIC values generated for pipeline testing; the GDD/ET0 formulas are "
                "real but the inputs are not."
                if synthetic
                else (
                    "GDD uses base 5 C. ET0 uses the Hargreaves equation with FAO-56 "
                    "extraterrestrial radiation; it is an approximation, not a "
                    "Penman-Monteith reference ET."
                )
            ),
        }
        if completeness < 0.9:
            context.missing.append(f"Weather completeness is {completeness:.0%}; some daily values are missing.")
    else:
        context.missing.append("No weather data available; water-balance scoring is disabled.")
        context.weather = {"available": False}

    # Soil
    if soil_result and soil_result.metadata.get("properties"):
        props = soil_result.metadata["properties"]
        values = {key: item.get("value") for key, item in props.items()}
        context.soil = {
            "available": any(v is not None for v in values.values()),
            "source": soil_result.source_id,
            "ph": values.get("ph"),
            "soc_g_kg": values.get("soc"),
            "nitrogen_g_kg": values.get("nitrogen"),
            "clay_pct": values.get("clay"),
            "sand_pct": values.get("sand"),
            "silt_pct": values.get("silt"),
            "texture_class": _texture_class(
                values.get("clay"), values.get("sand"), values.get("silt")
            ),
            "depth": "0-30 cm, thickness-weighted",
            "spatial_representativeness": (
                "SYNTHETIC values — not SoilGrids predictions or a soil test"
                if synthetic
                else "SoilGrids 250 m predicted values, not a soil test"
            ),
            "uncertainty_note": (
                "SYNTHETIC values generated for pipeline testing."
                if synthetic
                else "SoilGrids reports prediction uncertainty; this prototype does not propagate it."
            ),
        }
        if not context.soil["available"]:
            context.missing.append("SoilGrids returned no values at this point.")
    else:
        context.missing.append("No soil data available; agronomic-fit scoring is partial.")
        context.soil = {"available": False}

    # Terrain
    if terrain_result and terrain_result.observations:
        values = {o.variable: o.value for o in terrain_result.observations}
        context.terrain = {
            "available": True,
            "source": terrain_result.source_id,
            "elevation_m": values.get("elevation"),
            "slope_pct": values.get("slope"),
            "spatial_representativeness": (
                "SYNTHETIC values — not a DEM measurement"
                if synthetic
                else "SRTM ~90 m elevation and local slope"
            ),
        }
        if "slope" not in values:
            context.missing.append("Slope unavailable; erosion scoring uses a neutral slope.")
    else:
        context.missing.append("No terrain data available; erosion scoring uses a neutral slope.")
        context.terrain = {"available": False}

    # Composite completeness for confidence.  Fixed weights sum to 1.0 so a
    # missing component genuinely lowers completeness instead of being
    # renormalised away.
    completeness_total = 0.0
    if context.weather.get("available"):
        completeness_total += 0.40 * float(context.weather.get("completeness", 0))
    if context.soil.get("available"):
        completeness_total += 0.30
    if context.terrain.get("available"):
        completeness_total += 0.30
    context.data_completeness = float(min(1.0, completeness_total))
    context.caveats = (
        [
            "SYNTHETIC RUN: weather, soil and terrain values are generated, not measured or modelled from real data.",
            "Rotation scores on synthetic inputs test the pipeline only; they say nothing about the real field.",
        ]
        if synthetic
        else [
            "Weather, soil and terrain are regional products, not field measurements.",
            "SoilGrids and NASA POWER values carry uncertainty that is not propagated into the scores.",
            "A local soil test, rain gauge, and irrigation records would materially improve the evaluation.",
        ]
    )
    return context, frame
