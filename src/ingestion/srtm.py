"""SRTM-derived elevation and slope adapter (Skadi tiles, no credentials)."""

from __future__ import annotations

import gzip
import math
from pathlib import Path
from typing import Any

import numpy as np
import requests

from ..preprocessing.geometry import FieldGeometry
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache

SKADI_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"
OPEN_METEO_ELEVATION = "https://api.open-meteo.com/v1/elevation"
VOID = -32768


def _tile_name(lat: float, lon: float) -> str:
    lat_floor = math.floor(lat)
    lon_floor = math.floor(lon)
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"{ns}{abs(lat_floor):02d}{ew}{abs(lon_floor):03d}"


def _tile_url(tile: str) -> str:
    folder = tile[:3]
    return f"{SKADI_URL}/{folder}/{tile}.hgt.gz"


def _load_tile(path: Path) -> np.ndarray:
    with gzip.open(path, "rb") as handle:
        raw = handle.read()
    count = len(raw) // 2
    side = int(round(math.sqrt(count)))
    if side * side != count:
        raise ValueError(f"Unexpected SRTM tile size: {count} int16 values")
    array = np.frombuffer(raw, dtype=">i2").reshape((side, side)).astype("float32")
    array[array == VOID] = np.nan
    return array


def _bilinear(array: np.ndarray, row: float, col: float) -> float | None:
    n = array.shape[0]
    row = min(max(row, 0), n - 1.0001)
    col = min(max(col, 0), n - 1.0001)
    r0, c0 = int(math.floor(row)), int(math.floor(col))
    r1, c1 = min(r0 + 1, n - 1), min(c0 + 1, n - 1)
    fr, fc = row - r0, col - c0
    values = np.array(
        [
            array[r0, c0],
            array[r0, c1],
            array[r1, c0],
            array[r1, c1],
        ]
    )
    if not np.all(np.isfinite(values)):
        return None
    return float(
        values[0] * (1 - fr) * (1 - fc)
        + values[1] * (1 - fr) * fc
        + values[2] * fr * (1 - fc)
        + values[3] * fr * fc
    )


def _slope_percent(array: np.ndarray, row: int, col: int, lat: float, n: int) -> float | None:
    half = 2
    r0, r1 = max(0, row - half), min(n, row + half + 1)
    c0, c1 = max(0, col - half), min(n, col + half + 1)
    window = array[r0:r1, c0:c1]
    if window.shape[0] < 3 or window.shape[1] < 3 or not np.all(np.isfinite(window)):
        return None
    degree_m_lat = 110_540.0
    degree_m_lon = 111_320.0 * math.cos(math.radians(lat))
    spacing = 1.0 / (n - 1)
    dz_dy, dz_dx = np.gradient(window, spacing * degree_m_lat, spacing * degree_m_lon)
    gradient = math.sqrt(float(np.nanmean(dz_dx) ** 2 + np.nanmean(dz_dy) ** 2))
    return gradient * 100.0


class SrtmAdapter:
    source_id = "srtm-skadi"

    def __init__(
        self,
        cache: Cache,
        session: requests.Session,
        *,
        timeout: float = 120,
    ):
        self.cache = cache
        self.session = session
        self.timeout = timeout

    def fetch(self, field: FieldGeometry, start: str, end: str) -> SourceResult:
        lon, lat = field.centroid_lonlat
        tile = _tile_name(lat, lon)
        url = _tile_url(tile)
        destination = self.cache.root / "srtm" / f"{tile}.hgt.gz"
        try:
            self.cache.download_file(
                self.session,
                url,
                destination,
                timeout=self.timeout,
                headers={"User-Agent": "agri-rotation-intelligence/0.1"},
            )
            array = _load_tile(destination)
            n = array.shape[0]
            row = (math.floor(lat) + 1 - lat) * (n - 1)
            col = (lon - math.floor(lon)) * (n - 1)
            elevation = _bilinear(array, row, col)
            slope = _slope_percent(array, int(round(row)), int(round(col)), lat, n)
            if elevation is None:
                raise ValueError("SRTM void at the requested point")
            observations = [
                Observation(
                    date="static",
                    value=round(elevation, 2),
                    variable="elevation",
                    source=self.source_id,
                    quality_flag="measured_dem",
                    metadata={"unit": "m", "tile": tile},
                )
            ]
            if slope is not None:
                observations.append(
                    Observation(
                        date="static",
                        value=round(slope, 3),
                        variable="slope",
                        source=self.source_id,
                        quality_flag="derived_dem",
                        metadata={"unit": "%", "tile": tile, "window": "5x5 pixels"},
                    )
                )
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="ok" if slope is not None else "partial",
                    message=f"SRTM tile {tile} read successfully",
                    records=len(observations),
                ),
                observations=observations,
                metadata={
                    "tile": tile,
                    "tile_url": url,
                    "tile_size": array.shape[0],
                    "spatial_resolution": "about 90 m (1 arc-second SRTM)",
                    "temporal_resolution": "static (SRTM acquired 2000)",
                    "license": "SRTM public domain; Skadi tile packaging openly distributed",
                    "note": "Elevation is from SRTM; slope is derived from a local 5x5 pixel window.",
                    "accessed_at": utc_now_iso(),
                },
            )
        except Exception as exc:
            # Graceful fallback: elevation only, no slope, from Open-Meteo's
            # Copernicus DEM GLO-90 service.
            try:
                response = self.session.get(
                    OPEN_METEO_ELEVATION,
                    params={"latitude": lat, "longitude": lon},
                    timeout=30,
                )
                response.raise_for_status()
                elevation = float(response.json()["elevation"][0])
                observations = [
                    Observation(
                        date="static",
                        value=round(elevation, 2),
                        variable="elevation",
                        source="cop-dem-glo-90",
                        quality_flag="measured_dem_fallback",
                        metadata={"unit": "m", "fallback_reason": str(exc)},
                    )
                ]
                return SourceResult(
                    source_id=self.source_id,
                    status=SourceStatus(
                        source_id=self.source_id,
                        status="partial",
                        message=f"SRTM unavailable ({exc}); used Open-Meteo Copernicus DEM elevation only",
                        records=1,
                    ),
                    observations=observations,
                    metadata={
                        "fallback": "Open-Meteo elevation (Copernicus DEM GLO-90)",
                        "fallback_reason": str(exc),
                        "spatial_resolution": "about 90 m",
                        "temporal_resolution": "static",
                        "license": "Copernicus DEM, open",
                        "accessed_at": utc_now_iso(),
                    },
                )
            except Exception as fallback_exc:
                return SourceResult(
                    source_id=self.source_id,
                    status=SourceStatus(
                        source_id=self.source_id,
                        status="error",
                        message=f"SRTM failed ({exc}); fallback failed ({fallback_exc})",
                    ),
                    metadata={"tile": tile, "tile_url": url},
                )
