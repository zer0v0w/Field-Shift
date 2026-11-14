"""NASA POWER daily agroclimatology adapter (no credentials)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import requests

from ..preprocessing.geometry import FieldGeometry
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
PARAMETERS = [
    "T2M",
    "T2M_MIN",
    "T2M_MAX",
    "PRECTOTCORR",
    "ALLSKY_SFC_SW_DWN",
    "RH2M",
    "WS2M",
]
PARAMETER_LABELS = {
    "T2M": "mean air temperature at 2 m",
    "T2M_MIN": "minimum air temperature at 2 m",
    "T2M_MAX": "maximum air temperature at 2 m",
    "PRECTOTCORR": "corrected precipitation",
    "ALLSKY_SFC_SW_DWN": "all-sky shortwave downward irradiance",
    "RH2M": "relative humidity at 2 m",
    "WS2M": "wind speed at 2 m",
}
FILL_VALUES = {-999.0, -999, -999.0}


class NasaPowerAdapter:
    source_id = "nasa-power"

    def __init__(
        self,
        cache: Cache,
        session: requests.Session,
        *,
        timeout: float = 90,
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
        params = {
            "parameters": ",".join(PARAMETERS),
            "community": "AG",
            "longitude": round(lon, 5),
            "latitude": round(lat, 5),
            "start": start.replace("-", ""),
            "end": end.replace("-", ""),
            "format": "JSON",
        }
        key = f"{lat:.5f}:{lon:.5f}:{start}:{end}:{','.join(PARAMETERS)}"
        try:
            data, cached = self.cache.fetch_json(
                self.session,
                POWER_URL,
                namespace="nasa_power",
                key=key,
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
                    message=f"NASA POWER request failed: {exc}",
                ),
                metadata={"requested_start": start, "requested_end": end},
            )

        try:
            parameters = data["properties"]["parameter"]
            units = {
                name: data.get("parameters", {}).get(name, {}).get("units", "")
                for name in PARAMETERS
            }
        except (KeyError, TypeError) as exc:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="error",
                    message=f"Unexpected NASA POWER response: {exc}",
                ),
                metadata={"requested_start": start, "requested_end": end},
            )

        observations: list[Observation] = []
        missing_counts: dict[str, int] = {}
        for name in PARAMETERS:
            series = parameters.get(name, {})
            missing = 0
            for date, value in series.items():
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    missing += 1
                    continue
                if numeric in FILL_VALUES:
                    missing += 1
                    continue
                observations.append(
                    Observation(
                        date=f"{date[:4]}-{date[4:6]}-{date[6:8]}",
                        value=numeric,
                        variable=name,
                        source=self.source_id,
                        quality_flag="reanalysis_daily",
                        metadata={"unit": units.get(name, ""), "label": PARAMETER_LABELS.get(name, name)},
                    )
                )
            missing_counts[name] = missing

        observations.sort(key=lambda o: (o.date, o.variable))
        status_name = "ok" if observations else "error"
        if observations and any(missing_counts.values()):
            status_name = "partial"
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status=status_name,
                message=(
                    f"{len(observations)} daily values for {len(PARAMETERS)} parameters"
                    if observations
                    else "No NASA POWER values returned"
                ),
                records=len(observations),
                cached=cached,
            ),
            observations=observations,
            metadata={
                "requested_start": start,
                "requested_end": end,
                "requested_point": {"lat": lat, "lon": lon},
                "parameters": PARAMETERS,
                "parameter_labels": {k: PARAMETER_LABELS[k] for k in PARAMETERS},
                "units": units,
                "missing_value_counts": missing_counts,
                "spatial_resolution": "0.5 degree latitude x 0.625 degree longitude (about 55 x 60 km)",
                "temporal_resolution": "daily",
                "product": "NASA POWER agroclimatology (MERRA-2 based)",
                "license": "NASA open data",
                "representativeness": "Regional grid cell, not a field weather station.",
                "accessed_at": utc_now_iso(),
                "cached": cached,
            },
        )

    @staticmethod
    def to_frame(result: SourceResult) -> pd.DataFrame:
        if not result.observations:
            return pd.DataFrame(columns=["date", "variable", "value", "unit"])
        rows = [
            {
                "date": o.date,
                "variable": o.variable,
                "value": o.value,
                "unit": o.metadata.get("unit", ""),
            }
            for o in result.observations
        ]
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.pivot_table(
            index="date", columns="variable", values="value", aggfunc="mean"
        ).sort_index()
