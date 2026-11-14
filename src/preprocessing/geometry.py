"""Field geometry construction and validation.

The analysis accepts a point with a buffer, a bounding box, or a GeoJSON
polygon.  Everything is normalised to WGS84 (EPSG:4326) for STAC queries and
reprojected locally for area and buffering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union


def _utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180.0) / 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def _project_geometry(geometry: BaseGeometry, source_crs: str, target_crs: str) -> BaseGeometry:
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    return transform(transformer.transform, geometry)


@dataclass
class FieldGeometry:
    field_id: str
    geometry: BaseGeometry
    source: str
    buffer_m: float | None = None
    crs: str = "EPSG:4326"

    def __post_init__(self) -> None:
        if self.geometry.is_empty:
            raise ValueError("Field geometry is empty")
        if not self.geometry.is_valid:
            self.geometry = self.geometry.buffer(0)
        if self.geometry.is_empty:
            raise ValueError("Field geometry is invalid and could not be repaired")
        if self.geometry.geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"Expected a polygon, got {self.geometry.geom_type}")

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return tuple(self.geometry.bounds)  # type: ignore[return-value]

    @property
    def centroid_lonlat(self) -> tuple[float, float]:
        centroid = self.geometry.centroid
        return float(centroid.x), float(centroid.y)

    @property
    def area_ha(self) -> float:
        lon, lat = self.centroid_lonlat
        epsg = _utm_epsg(lon, lat)
        projected = _project_geometry(self.geometry, "EPSG:4326", f"EPSG:{epsg}")
        return float(projected.area / 10_000.0)

    @property
    def bbox_padded(self) -> tuple[float, float, float, float]:
        """Bounding box with a small margin so edge pixels are not lost."""
        minx, miny, maxx, maxy = self.bounds
        pad_x = max((maxx - minx) * 0.02, 0.0005)
        pad_y = max((maxy - miny) * 0.02, 0.0005)
        return (minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y)

    def to_geojson(self) -> dict[str, Any]:
        return {
            "type": "Feature",
            "properties": {"field_id": self.field_id},
            "geometry": mapping(self.geometry),
        }

    def to_dict(self) -> dict[str, Any]:
        lon, lat = self.centroid_lonlat
        return {
            "field_id": self.field_id,
            "source": self.source,
            "crs": self.crs,
            "geometry": mapping(self.geometry),
            "bbox": list(self.bounds),
            "centroid": {"lon": lon, "lat": lat},
            "area_ha": round(self.area_ha, 4),
            "buffer_m": self.buffer_m,
        }


def build_point_field(
    lat: float,
    lon: float,
    buffer_m: float = 100.0,
    field_id: str | None = None,
) -> FieldGeometry:
    if not (-90 <= lat <= 90):
        raise ValueError(f"Latitude out of range: {lat}")
    if not (-180 <= lon <= 180):
        raise ValueError(f"Longitude out of range: {lon}")
    if buffer_m <= 0:
        raise ValueError("buffer_m must be positive")

    epsg = _utm_epsg(lon, lat)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    to_wgs = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    x, y = to_utm.transform(lon, lat)
    buffered = Point(x, y).buffer(buffer_m)
    geometry = transform(to_wgs.transform, buffered)
    fid = field_id or f"point_{lat:.5f}_{lon:.5f}"
    return FieldGeometry(field_id=fid, geometry=geometry, source="point_buffer", buffer_m=buffer_m)


def build_bbox_field(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    field_id: str | None = None,
) -> FieldGeometry:
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("Bounding box must have min < max in both axes")
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ValueError("Longitude out of range")
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ValueError("Latitude out of range")
    geometry = box(min_lon, min_lat, max_lon, max_lat)
    fid = field_id or f"bbox_{min_lat:.5f}_{min_lon:.5f}"
    return FieldGeometry(field_id=fid, geometry=geometry, source="bbox")


def _read_geojson_payload(payload: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    path = Path(payload)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(str(payload))


def build_geojson_field(
    payload: str | Path | dict[str, Any],
    field_id: str | None = None,
) -> FieldGeometry:
    data = _read_geojson_payload(payload)
    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        if not features:
            raise ValueError("GeoJSON FeatureCollection is empty")
        geometries = [shape(feature["geometry"]) for feature in features if feature.get("geometry")]
        if not geometries:
            raise ValueError("No geometries found in GeoJSON")
        geometry = unary_union(geometries)
    elif data.get("type") == "Feature":
        geometry = shape(data["geometry"])
    elif data.get("type") in ("Polygon", "MultiPolygon"):
        geometry = shape(data)
    else:
        raise ValueError(f"Unsupported GeoJSON type: {data.get('type')}")

    # GeoJSON is WGS84 by RFC 7946 unless an explicit legacy CRS is present.
    crs = "EPSG:4326"
    legacy_crs = data.get("crs")
    if isinstance(legacy_crs, dict):
        name = legacy_crs.get("properties", {}).get("name", "")
        if "EPSG:" in name:
            crs = name.split("EPSG:")[-1].strip()
            crs = f"EPSG:{crs}"
    if crs != "EPSG:4326":
        geometry = _project_geometry(geometry, crs, "EPSG:4326")

    fid = field_id or f"geojson_{geometry.centroid.y:.5f}_{geometry.centroid.x:.5f}"
    return FieldGeometry(field_id=fid, geometry=geometry, source="geojson")
