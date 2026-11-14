"""Build the vegetation time-series table used by outputs and plots.

The table keeps measured observations and daily smoothed/interpolated values in
separate rows so no consumer can mistake an interpolated value for a measurement.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from ..ingestion.base import SourceResult

OUTPUT_COLUMNS = [
    "date",
    "record_type",
    "is_observed",
    "ndvi",
    "ndvi_smoothed",
    "quality_flag",
    "source",
    "scene_id",
    "cloud_cover",
    "valid_fraction",
    "n_valid_pixels",
    "notes",
]


def observations_to_frame(result: SourceResult) -> pd.DataFrame:
    rows = []
    for observation in result.observations:
        rows.append(
            {
                "date": observation.date,
                "record_type": "observation",
                "is_observed": True,
                "ndvi": observation.value,
                "quality_flag": observation.quality_flag,
                "source": observation.source,
                "scene_id": observation.scene_id,
                "cloud_cover": observation.cloud_cover,
                "valid_fraction": observation.valid_fraction,
                "n_valid_pixels": observation.n_valid_pixels,
                "notes": json.dumps(observation.metadata, default=str)
                if observation.metadata
                else "",
            }
        )
    if not rows:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    for column in OUTPUT_COLUMNS:
        if column not in frame:
            frame[column] = np.nan
    return frame[OUTPUT_COLUMNS].sort_values("date").reset_index(drop=True)


def merge_daily(
    observations: pd.DataFrame,
    daily: pd.DataFrame,
    *,
    source_id: str,
) -> pd.DataFrame:
    if daily is None or daily.empty:
        return observations[OUTPUT_COLUMNS].copy()

    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"])
    smoothed_column = "NDVI_interpolated" if "NDVI_interpolated" in daily else "ndvi_smoothed"
    daily = daily[["date", smoothed_column]].rename(columns={smoothed_column: "ndvi_smoothed"})

    observed_dates = set(observations["date"].dt.normalize()) if not observations.empty else set()
    daily["date"] = daily["date"].dt.normalize()
    daily = daily.drop_duplicates("date", keep="last")

    if observations.empty:
        base = pd.DataFrame()
    else:
        base = observations.copy()
        base["date"] = base["date"].dt.normalize()
        base = base.merge(daily, on="date", how="left")

    extra = daily[~daily["date"].isin(observed_dates)].copy()
    extra["record_type"] = "daily_smoothed"
    extra["is_observed"] = False
    extra["ndvi"] = np.nan
    extra["quality_flag"] = "interpolated_or_gap_filled"
    extra["source"] = source_id
    extra["scene_id"] = None
    extra["cloud_cover"] = np.nan
    extra["valid_fraction"] = np.nan
    extra["n_valid_pixels"] = np.nan
    extra["notes"] = "Daily value from PCHIP interpolation and Whittaker smoothing; not a measurement."

    combined = pd.concat([base, extra], ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"])
    combined = combined.sort_values(["date", "is_observed"], ascending=[True, False])
    for column in OUTPUT_COLUMNS:
        if column not in combined:
            combined[column] = np.nan
    return combined[OUTPUT_COLUMNS].reset_index(drop=True)


def coverage_summary(frame: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    observations = frame[frame["record_type"] == "observation"]
    expected_days = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    return {
        "n_observations": int(len(observations)),
        "n_daily_values": int((frame["record_type"] == "daily_smoothed").sum()),
        "observation_span_days": (
            int((observations["date"].max() - observations["date"].min()).days)
            if len(observations) > 1
            else 0
        ),
        "analysis_span_days": expected_days,
        "observation_density_per_30_days": round(
            len(observations) / max(expected_days / 30.0, 1e-9), 2
        ),
    }
