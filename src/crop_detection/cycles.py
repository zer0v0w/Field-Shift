"""Crop-cycle and generic-stage detection wrappers.

The heavy lifting is the vendored NASA Harvest implementation.  This module
normalises its output, adds candidate bare-soil windows, and attaches explicit
quality/method metadata.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from vendor.crop_cycle_detection.crop_cycle import crop_cycle_model
from vendor.crop_stage_detection.crop_stage import estimate_stage_adaptive
from ..preprocessing.quality import gap_statistics

UPSTREAM_COMMIT = "293f90658be64dd1f8fc0ca8d36106f70edfc8f2"
STAGE_COMMIT = "2005f53d53c0aa9c6240800db4afc514d8f8f13d"


def _iso(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return None


def _round(value: Any, digits: int = 4) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _window_stats(daily: pd.DataFrame, start: str | None, end: str | None) -> dict[str, Any]:
    empty = {
        "mean_ndvi": None,
        "min_ndvi": None,
        "max_ndvi": None,
        "integrated_ndvi": None,
        "stats_start": None,
        "stats_end": None,
    }
    if daily.empty:
        return empty
    # A season can begin before the first usable observation.  In that case the
    # boundary is unresolved, but statistics over the observed part are still
    # useful; record the actual statistics window so it is not mistaken for the
    # true season boundary.
    stats_start = pd.Timestamp(start) if start else daily["date"].min()
    stats_end = pd.Timestamp(end) if end else daily["date"].max()
    subset = daily[(daily["date"] >= stats_start) & (daily["date"] <= stats_end)]
    if subset.empty:
        return empty
    values = subset["ndvi_smoothed"].to_numpy(dtype=float)
    return {
        "mean_ndvi": _round(np.nanmean(values)),
        "min_ndvi": _round(np.nanmin(values)),
        "max_ndvi": _round(np.nanmax(values)),
        "integrated_ndvi": _round(np.nansum(np.clip(values - 0.15, 0, None))),
        "stats_start": pd.Timestamp(stats_start).date().isoformat(),
        "stats_end": pd.Timestamp(stats_end).date().isoformat(),
    }


def _stage_at(daily: pd.DataFrame, date: str | None) -> dict[str, Any] | None:
    if not date:
        return None
    subset = daily[daily["date"] <= pd.Timestamp(date)].tail(500)
    if len(subset) < 30:
        return {
            "date": date,
            "stage": "Insufficient Data",
            "stage_description": "Insufficient data for reliable estimation",
            "value": _round(subset["ndvi_smoothed"].iloc[-1]) if not subset.empty else None,
            "velocity": None,
            "peak_date": None,
            "days_since_peak": None,
        }
    try:
        result = estimate_stage_adaptive(
            subset["ndvi_smoothed"].to_numpy(dtype=float),
            dates=subset["date"],
            min_observations=30,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "date": date,
            "stage": "Error",
            "stage_description": f"Stage estimation failed: {exc}",
            "value": None,
            "velocity": None,
            "peak_date": None,
            "days_since_peak": None,
        }
    return {
        "date": date,
        "stage": result.get("Stage"),
        "stage_description": result.get("Stage_description"),
        "value": _round(result.get("Value")),
        "velocity": _round(result.get("Velocity"), 6),
        "peak_date": _iso(result.get("Peak_date")),
        "days_since_peak": result.get("Days_since_peak"),
    }


def _bare_soil_windows(
    daily: pd.DataFrame,
    seasons: list[dict[str, Any]],
    threshold: float,
    min_days: int,
) -> list[dict[str, Any]]:
    if daily.empty:
        return []
    low = (daily["ndvi_smoothed"] < threshold).to_numpy()
    dates = daily["date"].to_numpy()
    values = daily["ndvi_smoothed"].to_numpy(dtype=float)
    windows: list[dict[str, Any]] = []
    start_idx: int | None = None
    for index, flag in enumerate(low):
        if flag and start_idx is None:
            start_idx = index
        if (not flag or index == len(low) - 1) and start_idx is not None:
            end_idx = index - 1 if not flag else index
            if end_idx - start_idx + 1 >= min_days:
                start_date = pd.Timestamp(dates[start_idx]).date().isoformat()
                end_date = pd.Timestamp(dates[end_idx]).date().isoformat()
                overlaps = any(
                    season.get("start")
                    and season.get("end")
                    and pd.Timestamp(season["start"]) <= pd.Timestamp(end_date)
                    and pd.Timestamp(season["end"]) >= pd.Timestamp(start_date)
                    for season in seasons
                )
                relationship = "between_or_outside_seasons"
                if overlaps:
                    relationship = "overlaps_detected_season"
                windows.append(
                    {
                        "start": start_date,
                        "end": end_date,
                        "duration_days": end_idx - start_idx + 1,
                        "min_ndvi": _round(np.nanmin(values[start_idx : end_idx + 1])),
                        "mean_ndvi": _round(np.nanmean(values[start_idx : end_idx + 1])),
                        "max_ndvi": _round(np.nanmax(values[start_idx : end_idx + 1])),
                        "relationship": relationship,
                        "confidence": "candidate_low_ndvi_window",
                        "note": (
                            "Low NDVI is consistent with bare soil, stubble, fallow or sparse "
                            "vegetation; it is not proof of bare soil."
                        ),
                    }
                )
            start_idx = None
    return windows


def detect_cycles(
    observations: pd.DataFrame,
    *,
    field_id: str,
    params: dict[str, Any],
    bare_soil_ndvi_max: float = 0.25,
    bare_soil_min_days: int = 10,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Run the vendored cycle model and return a normalised result + daily series."""
    if observations.empty:
        return (
            {
                "status": "no_data",
                "seasons": [],
                "bare_soil_windows": [],
                "quality": {"n_observations": 0},
                "message": "No observations available for cycle detection.",
            },
            pd.DataFrame(columns=["date", "ndvi_smoothed"]),
        )

    frame = observations[["date", "ndvi"]].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.dropna(subset=["date", "ndvi"]).sort_values("date")
    frame = frame.groupby("date", as_index=False)["ndvi"].mean()
    frame = frame.rename(columns={"ndvi": "NDVI"})

    quality = gap_statistics(frame["date"].dt.date.astype(str).tolist())
    if len(frame) < 10 or quality["span_days"] < 60:
        return (
            {
                "status": "insufficient_data",
                "seasons": [],
                "bare_soil_windows": [],
                "quality": quality,
                "message": (
                    "At least 10 observations spanning 60 days are required for cycle detection."
                ),
                "method": {"upstream_commit": UPSTREAM_COMMIT, "parameters": params},
            },
            pd.DataFrame(columns=["date", "ndvi_smoothed"]),
        )

    try:
        report, processed = crop_cycle_model(frame, **params)
    except Exception as exc:  # pragma: no cover - upstream defensive path
        return (
            {
                "status": "error",
                "seasons": [],
                "bare_soil_windows": [],
                "quality": quality,
                "message": f"Crop-cycle model failed: {exc}",
                "method": {"upstream_commit": UPSTREAM_COMMIT, "parameters": params},
            },
            pd.DataFrame(columns=["date", "ndvi_smoothed"]),
        )

    daily = processed[["date", "NDVI_interpolated"]].rename(
        columns={"NDVI_interpolated": "ndvi_smoothed"}
    )
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.dropna().sort_values("date").reset_index(drop=True)

    seasons: list[dict[str, Any]] = []
    if report is not None and not report.empty:
        for _, row in report.iterrows():
            start = _iso(row.get("approx_season_start"))
            end = _iso(row.get("approx_season_end"))
            peak_date = _iso(row.get("ndvi_peak_date"))
            stats = _window_stats(daily, start, end)
            stages = [
                stage
                for stage in (
                    _stage_at(daily, _iso(row.get("greenup_date"))),
                    _stage_at(daily, peak_date),
                    _stage_at(daily, end),
                )
                if stage is not None
            ]
            seasons.append(
                {
                    "season_id": f"S{int(row.get('season_number', len(seasons) + 1)):02d}",
                    "season_number": int(row.get("season_number", len(seasons) + 1)),
                    "start": start,
                    "end": end,
                    "length_days": _round(row.get("approx_season_length"), 1),
                    "greenup_date": _iso(row.get("greenup_date")),
                    "peak_date": peak_date,
                    "peak_ndvi": _round(row.get("ndvi_peak_value")),
                    "trough_before": _iso(row.get("trough_before_peak")),
                    "trough_after": _iso(row.get("trough_after_peak")),
                    "trough_distance_days": _round(row.get("troughs_days_distance"), 1),
                    "mean_ndvi": stats["mean_ndvi"],
                    "min_ndvi": stats["min_ndvi"],
                    "max_ndvi": stats["max_ndvi"],
                    "integrated_ndvi": stats["integrated_ndvi"],
                    "stats_start": stats["stats_start"],
                    "stats_end": stats["stats_end"],
                    "stages": stages,
                    "confidence": "candidate",
                    "boundary_note": (
                        None
                        if start and end
                        else "At least one season boundary is unresolved within the analysis window; "
                        "the season likely began before the first usable observation. Statistics cover "
                        "the observed part only."
                    ),
                    "note": (
                        "Trough-to-trough heuristic season. Start/end are approximations; "
                        "the season may be split or merged in bimodal rainfall years."
                    ),
                }
            )

    bare_windows = _bare_soil_windows(
        daily, seasons, bare_soil_ndvi_max, bare_soil_min_days
    )
    result = {
        "status": "ok" if seasons else "no_cycles_detected",
        "seasons": seasons,
        "n_seasons": len(seasons),
        "bare_soil_windows": bare_windows,
        "quality": {
            **quality,
            "n_seasons": len(seasons),
            "daily_values": int(len(daily)),
            "thresholds": {
                "bare_soil_ndvi_max": bare_soil_ndvi_max,
                "bare_soil_min_days": bare_soil_min_days,
            },
        },
        "method": {
            "name": "NASA Harvest crop-cycle-detection (trough-to-trough NDVI)",
            "upstream_commit": UPSTREAM_COMMIT,
            "parameters": params,
            "stage_method": {
                "name": "NASA Harvest crop-stage-detection (generic A-E NDVI-relative stages)",
                "upstream_commit": STAGE_COMMIT,
                "note": "Stages are not BBCH/Zadoks phenology and are not crop-specific.",
            },
            "caveats": [
                "Season boundaries are heuristic and depend on the configured thresholds.",
                "Evergreen crops (olive, citrus) violate the rise/peak/decline assumption.",
                "Low NDVI can be bare soil, stubble, fallow, or sparse vegetation; windows are candidates.",
                "Interpolated daily values are not measurements.",
            ],
        },
    }
    if not seasons:
        result["message"] = (
            "No trough-to-trough seasons met the configured thresholds. The series may be "
            "too short, dominated by gaps, bimodal, or evergreen; inspect the NDVI plot and "
            "gap statistics before interpreting this as a fallow year."
        )
    return result, daily
