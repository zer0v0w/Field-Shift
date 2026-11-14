"""Deterministic synthetic data for offline pipeline testing.

Every value produced here is explicitly flagged as synthetic.  The pipeline
must never label a mock run as an analysis of the requested field.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np

from ..preprocessing.geometry import FieldGeometry
from .base import Observation, SourceResult, SourceStatus, utc_now_iso


def _date_range(start: str, end: str) -> list[date]:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    return [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]


def _seasonal_ndvi(doy: int) -> float:
    # Winter cereal curve: low summer, green-up in autumn, peak in early spring.
    peak = 78.0
    sigma = 52.0
    distance = min(abs(doy - peak), 365 - abs(doy - peak))
    winter = 0.13 + 0.66 * math.exp(-0.5 * (distance / sigma) ** 2)
    # A small summer greening component to make the series less artificial.
    summer = 0.05 * math.exp(-0.5 * (((doy - 205) / 30.0) ** 2))
    return min(0.92, winter + summer)


class MockAdapter:
    source_id = "mock"

    def __init__(self, seed: int = 42):
        self.seed = seed

    def fetch(
        self,
        field: FieldGeometry,
        start: str,
        end: str,
        *,
        cloud_cover_max: float = 60.0,
        min_valid_fraction: float = 0.20,
        min_valid_pixels: int = 5,
        max_scenes: int = 400,
    ) -> SourceResult:
        rng = np.random.default_rng(self.seed)
        dates = _date_range(start, end)
        observations: list[Observation] = []
        # Roughly one observation every five days, with random cloud gaps.
        for index, day in enumerate(dates):
            if index % 5 != 0:
                continue
            if rng.random() < 0.16:
                continue
            value = _seasonal_ndvi(day.timetuple().tm_yday)
            value += rng.normal(0, 0.025)
            if rng.random() < 0.04:
                value -= rng.uniform(0.15, 0.30)
            value = float(np.clip(value, 0.02, 0.95))
            cloud = float(rng.uniform(0, 55))
            valid_fraction = float(rng.uniform(0.35, 0.98))
            observations.append(
                Observation(
                    date=day.isoformat(),
                    value=round(value, 6),
                    variable="NDVI",
                    source=self.source_id,
                    scene_id=f"SYNTHETIC_{day.strftime('%Y%m%d')}",
                    cloud_cover=round(cloud, 2),
                    valid_fraction=round(valid_fraction, 4),
                    n_valid_pixels=int(round(valid_fraction * 300)),
                    quality_flag="synthetic",
                    metadata={
                        "synthetic": True,
                        "generator": "MockAdapter._seasonal_ndvi",
                        "seed": self.seed,
                        "note": "Generated value, not a satellite observation.",
                    },
                )
            )
        observations.sort(key=lambda o: o.date)
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status="ok",
                message=(
                    "SYNTHETIC DEMO DATA — these are not observations of the "
                    "requested field and must not be interpreted as a real analysis."
                ),
                records=len(observations),
            ),
            observations=observations,
            metadata={
                "synthetic": True,
                "seed": self.seed,
                "requested_start": start,
                "requested_end": end,
                "generator": "winter-cereal double-Gaussian with noise and gaps",
                "spatial_resolution": "not applicable",
                "temporal_resolution": "5-day synthetic sampling",
                "license": "synthetic",
                "accessed_at": utc_now_iso(),
            },
        )

    def weather(self, field: FieldGeometry, start: str, end: str) -> SourceResult:
        rng = np.random.default_rng(self.seed + 1)
        observations: list[Observation] = []
        for day in _date_range(start, end):
            doy = day.timetuple().tm_yday
            seasonal = math.sin(2 * math.pi * (doy - 105) / 365)
            tmean = 18.0 + 8.5 * seasonal + rng.normal(0, 1.2)
            tmin = tmean - rng.uniform(4, 7)
            tmax = tmean + rng.uniform(4, 8)
            rain_prob = 0.30 if doy < 90 or doy > 300 else 0.03
            rain = float(rng.gamma(1.2, 5.0)) if rng.random() < rain_prob else 0.0
            solar = max(2.0, 17.0 + 6.0 * seasonal + rng.normal(0, 1.5))
            rh = float(np.clip(62 - 12 * seasonal + rng.normal(0, 8), 20, 98))
            wind = float(max(0.2, 1.8 + rng.normal(0, 0.6)))
            for variable, value, unit in [
                ("T2M", tmean, "C"),
                ("T2M_MIN", tmin, "C"),
                ("T2M_MAX", tmax, "C"),
                ("PRECTOTCORR", rain, "mm/day"),
                ("ALLSKY_SFC_SW_DWN", solar, "MJ/m^2/day"),
                ("RH2M", rh, "%"),
                ("WS2M", wind, "m/s"),
            ]:
                observations.append(
                    Observation(
                        date=day.isoformat(),
                        value=round(float(value), 3),
                        variable=variable,
                        source="mock-weather",
                        quality_flag="synthetic",
                        metadata={"unit": unit, "synthetic": True},
                    )
                )
        return SourceResult(
            source_id="mock-weather",
            status=SourceStatus(
                source_id="mock-weather",
                status="ok",
                message="SYNTHETIC weather — not a reanalysis or station record.",
                records=len(observations),
            ),
            observations=observations,
            metadata={"synthetic": True, "seed": self.seed + 1},
        )

    def soil(self, field: FieldGeometry, start: str, end: str) -> SourceResult:
        properties = {
            "ph": {"value": 7.6, "unit": "pH", "confidence": "synthetic"},
            "soc": {"value": 12.0, "unit": "g/kg", "confidence": "synthetic"},
            "nitrogen": {"value": 1.2, "unit": "g/kg", "confidence": "synthetic"},
            "clay": {"value": 38.0, "unit": "%", "confidence": "synthetic"},
            "sand": {"value": 20.0, "unit": "%", "confidence": "synthetic"},
            "silt": {"value": 42.0, "unit": "%", "confidence": "synthetic"},
        }
        observations = [
            Observation(
                date="static",
                value=item["value"],
                variable=name,
                source="mock-soil",
                quality_flag="synthetic",
                metadata={"unit": item["unit"], "synthetic": True},
            )
            for name, item in properties.items()
        ]
        return SourceResult(
            source_id="mock-soil",
            status=SourceStatus(
                source_id="mock-soil",
                status="ok",
                message="SYNTHETIC soil properties — not a soil test.",
                records=len(observations),
            ),
            observations=observations,
            metadata={"synthetic": True, "properties": properties},
        )

    def terrain(self, field: FieldGeometry, start: str, end: str) -> SourceResult:
        observations = [
            Observation(
                date="static",
                value=550.0,
                variable="elevation",
                source="mock-terrain",
                quality_flag="synthetic",
                metadata={"unit": "m", "synthetic": True},
            ),
            Observation(
                date="static",
                value=6.0,
                variable="slope",
                source="mock-terrain",
                quality_flag="synthetic",
                metadata={"unit": "%", "synthetic": True},
            ),
        ]
        return SourceResult(
            source_id="mock-terrain",
            status=SourceStatus(
                source_id="mock-terrain",
                status="ok",
                message="SYNTHETIC terrain — not a DEM measurement.",
                records=len(observations),
            ),
            observations=observations,
            metadata={"synthetic": True},
        )
