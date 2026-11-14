"""SoilGrids 2.0 point adapter (ISRIC, no credentials)."""

from __future__ import annotations

from typing import Any

import requests

from ..preprocessing.geometry import FieldGeometry
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache

SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
PROPERTIES = ["phh2o", "soc", "nitrogen", "clay", "sand", "silt"]
DEPTHS = ["0-5cm", "5-15cm", "15-30cm"]
TARGET_NAMES = {
    "phh2o": ("ph", "pH"),
    "soc": ("soc", "g/kg"),
    "nitrogen": ("nitrogen", "g/kg"),
    "clay": ("clay", "%"),
    "sand": ("sand", "%"),
    "silt": ("silt", "%"),
}


def _depth_weighted_mean(layer: dict[str, Any]) -> tuple[float | None, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    weighted_sum = 0.0
    weight_sum = 0.0
    d_factor = float(layer.get("unit_measure", {}).get("d_factor", 1) or 1)
    for depth in layer.get("depths", []):
        value = depth.get("values", {}).get("mean")
        top = depth.get("range", {}).get("top_depth")
        bottom = depth.get("range", {}).get("bottom_depth")
        if value is None or top is None or bottom is None:
            continue
        weight = float(bottom) - float(top)
        converted = float(value) / d_factor
        rows.append(
            {
                "label": depth.get("label"),
                "top_cm": top,
                "bottom_cm": bottom,
                "raw_mean": value,
                "converted_mean": round(converted, 4),
                "weight": weight,
            }
        )
        weighted_sum += converted * weight
        weight_sum += weight
    if weight_sum == 0:
        return None, rows
    return weighted_sum / weight_sum, rows


class SoilGridsAdapter:
    source_id = "soilgrids-2"

    def __init__(
        self,
        cache: Cache,
        session: requests.Session,
        *,
        timeout: float = 60,
    ):
        self.cache = cache
        self.session = session
        self.timeout = timeout

    def fetch(self, field: FieldGeometry, start: str, end: str) -> SourceResult:
        lon, lat = field.centroid_lonlat
        params = {
            "lon": round(lon, 5),
            "lat": round(lat, 5),
            "property": PROPERTIES,
            "depth": DEPTHS,
            "value": "mean",
        }
        key = f"{lat:.5f}:{lon:.5f}:{','.join(PROPERTIES)}:{','.join(DEPTHS)}"
        try:
            data, cached = self.cache.fetch_json(
                self.session,
                SOILGRIDS_URL,
                namespace="soilgrids",
                key=key,
                timeout=self.timeout,
                max_age_s=30 * 24 * 3600,
                params=params,
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="error",
                    message=f"SoilGrids request failed: {exc}",
                ),
                metadata={"requested_point": {"lat": lat, "lon": lon}},
            )

        layers = data.get("properties", {}).get("layers", [])
        properties: dict[str, Any] = {}
        observations: list[Observation] = []
        for layer in layers:
            name = layer.get("name")
            if name not in TARGET_NAMES:
                continue
            value, rows = _depth_weighted_mean(layer)
            target_name, unit = TARGET_NAMES[name]
            properties[target_name] = {
                "value": round(value, 4) if value is not None else None,
                "unit": unit,
                "depth_weighting": "0-30 cm, thickness-weighted",
                "depths": rows,
                "raw_unit": layer.get("unit_measure", {}).get("mapped_units"),
                "d_factor": layer.get("unit_measure", {}).get("d_factor"),
            }
            if value is not None:
                observations.append(
                    Observation(
                        date="static",
                        value=round(value, 4),
                        variable=target_name,
                        source=self.source_id,
                        quality_flag="model_prediction",
                        metadata={"unit": unit, "depth": "0-30cm"},
                    )
                )

        has_values = bool(observations)
        status_name = "ok" if has_values else "partial"
        message = (
            "SoilGrids returned predicted 0-30 cm properties"
            if has_values
            else "SoilGrids returned no values at this point (possibly masked urban/water pixel)"
        )
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status=status_name,
                message=message,
                records=len(observations),
                cached=cached,
            ),
            observations=observations,
            metadata={
                "requested_point": {"lat": lat, "lon": lon},
                "properties": properties,
                "spatial_resolution": "250 m",
                "temporal_resolution": "static prediction",
                "license": "CC-BY-4.0",
                "representativeness": "Predicted soil property, not a soil test.",
                "accessed_at": utc_now_iso(),
                "cached": cached,
            },
        )
