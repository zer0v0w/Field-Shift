"""Observation quality flags and gap diagnostics."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable

import numpy as np


def classify_ndvi_quality(
    valid_fraction: float | None,
    cloud_cover: float | None,
    min_valid_fraction: float = 0.20,
) -> str:
    if valid_fraction is None:
        return "unknown"
    if valid_fraction < min_valid_fraction:
        return "rejected_insufficient_clear_pixels"
    if valid_fraction >= 0.70 and (cloud_cover is None or cloud_cover <= 30):
        return "observed_clear"
    return "observed_marginal"


def gap_statistics(dates: Iterable[str]) -> dict[str, Any]:
    parsed = sorted(
        {
            datetime.fromisoformat(str(d)[:10])
            for d in dates
            if d is not None and str(d) not in ("", "NaT", "None")
        }
    )
    if len(parsed) < 2:
        return {
            "n_observations": len(parsed),
            "span_days": 0,
            "median_gap_days": None,
            "max_gap_days": None,
            "gaps_over_30_days": 0,
            "largest_gap_start": None,
            "largest_gap_end": None,
        }
    gaps = [(b - a).days for a, b in zip(parsed[:-1], parsed[1:])]
    largest_idx = int(np.argmax(gaps))
    return {
        "n_observations": len(parsed),
        "span_days": (parsed[-1] - parsed[0]).days,
        "median_gap_days": float(np.median(gaps)),
        "max_gap_days": int(max(gaps)),
        "gaps_over_30_days": int(sum(g > 30 for g in gaps)),
        "largest_gap_start": parsed[largest_idx].date().isoformat(),
        "largest_gap_end": parsed[largest_idx + 1].date().isoformat(),
    }


def quality_summary(observations: Iterable[Any]) -> dict[str, Any]:
    obs = list(observations)
    flags = Counter(getattr(o, "quality_flag", "unknown") for o in obs)
    fractions = [
        float(o.valid_fraction)
        for o in obs
        if getattr(o, "valid_fraction", None) is not None
    ]
    clouds = [
        float(o.cloud_cover)
        for o in obs
        if getattr(o, "cloud_cover", None) is not None
    ]
    return {
        "n_observations": len(obs),
        "quality_flag_counts": dict(sorted(flags.items())),
        "valid_fraction": {
            "min": round(min(fractions), 4) if fractions else None,
            "median": round(float(np.median(fractions)), 4) if fractions else None,
            "max": round(max(fractions), 4) if fractions else None,
        },
        "scene_cloud_cover": {
            "min": round(min(clouds), 2) if clouds else None,
            "median": round(float(np.median(clouds)), 2) if clouds else None,
            "max": round(max(clouds), 2) if clouds else None,
        },
    }
