"""Sentinel-2 L2A NDVI adapter (AWS Earth Search, no credentials)."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import requests
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject
from shapely.geometry.base import BaseGeometry

from ..preprocessing.geometry import FieldGeometry
from ..preprocessing.quality import classify_ndvi_quality
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache
from .cog import read_window, read_window_to_grid
from .stac import EARTH_SEARCH, StacClient

SCL_VALID = {4, 5, 7}  # vegetation, bare soil, unclassified (low probability)
SCL_LABELS = {
    0: "no_data",
    1: "saturated",
    2: "dark_area",
    3: "cloud_shadow",
    4: "vegetation",
    5: "bare_soil",
    6: "water",
    7: "unclassified",
    8: "cloud_medium",
    9: "cloud_high",
    10: "thin_cirrus",
    11: "snow",
}


def _field_key(field: FieldGeometry) -> str:
    return hashlib.sha256(field.geometry.wkb).hexdigest()[:16]


def _align_to_red(
    source: np.ndarray,
    source_mask: np.ndarray,
    source_transform: Any,
    source_crs: Any,
    red_shape: tuple[int, int],
    red_transform: Any,
    red_crs: Any,
) -> tuple[np.ndarray, np.ndarray]:
    if source.shape == red_shape:
        return source, source_mask
    destination = np.zeros(red_shape, dtype="float32")
    reproject(
        source=source,
        destination=destination,
        src_transform=source_transform,
        src_crs=source_crs,
        src_nodata=0,
        dst_transform=red_transform,
        dst_crs=red_crs,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    mask_dest = np.zeros(red_shape, dtype="uint8")
    reproject(
        source=source_mask.astype("uint8"),
        destination=mask_dest,
        src_transform=source_transform,
        src_crs=source_crs,
        src_nodata=0,
        dst_transform=red_transform,
        dst_crs=red_crs,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    return destination, mask_dest.astype(bool)


def _sample_scene(
    item: dict[str, Any],
    field: FieldGeometry,
    min_valid_fraction: float,
    min_valid_pixels: int,
) -> tuple[Observation | None, str | None]:
    scene_id = item.get("id", "unknown")
    red_href = StacClient.asset_href(item, "red")
    nir_href = StacClient.asset_href(item, "nir")
    scl_href = StacClient.asset_href(item, "scl")
    if not red_href or not nir_href or not scl_href:
        return None, "missing red/nir/scl asset"

    red_meta = StacClient.asset_band_meta(item, "red")
    nir_meta = StacClient.asset_band_meta(item, "nir")
    red_scale = float(red_meta.get("scale", 0.0001))
    red_offset = float(red_meta.get("offset", 0.0))
    nir_scale = float(nir_meta.get("scale", 0.0001))
    nir_offset = float(nir_meta.get("offset", 0.0))

    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_USE_HEAD="YES",
        GDAL_HTTP_MULTIRANGE="YES",
        AWS_NO_SIGN_REQUEST="YES",
    ):
        red = read_window(red_href, field.geometry, band=1)
        nir = read_window(nir_href, field.geometry, band=1)
        scl = read_window_to_grid(
            scl_href,
            field.geometry,
            dst_shape=red.shape,
            dst_transform=red.transform,
            dst_crs=red.crs,
            band=1,
            fill_value=0,
            resampling=Resampling.nearest,
        )

    nir_array, nir_mask = _align_to_red(
        nir.array, nir.valid_mask, nir.transform, nir.crs, red.shape, red.transform, red.crs
    )
    red_ref = red.array * red_scale + red_offset
    nir_ref = nir_array * nir_scale + nir_offset
    valid = red.valid_mask & nir_mask
    valid &= np.isin(scl.astype(int), list(SCL_VALID))
    valid &= np.isfinite(red_ref) & np.isfinite(nir_ref)
    valid &= (red_ref > 0) & (nir_ref > 0)
    valid &= (red_ref < 1.5) & (nir_ref < 1.5)

    n_total = int(red.geom_mask.sum())
    n_valid = int(valid.sum())
    valid_fraction = n_valid / n_total if n_total else 0.0
    if n_valid < min_valid_pixels or valid_fraction < min_valid_fraction:
        return None, f"insufficient clear pixels ({n_valid}/{n_total})"

    denominator = nir_ref[valid] + red_ref[valid]
    ndvi = (nir_ref[valid] - red_ref[valid]) / np.where(denominator == 0, np.nan, denominator)
    ndvi = ndvi[np.isfinite(ndvi)]
    if ndvi.size == 0:
        return None, "no finite NDVI values"
    value = float(np.median(ndvi))
    cloud_cover = StacClient.cloud_cover(item)
    quality = classify_ndvi_quality(valid_fraction, cloud_cover, min_valid_fraction)
    date = StacClient.item_date(item)
    if date is None:
        return None, "no scene date"

    scl_counts = {
        SCL_LABELS.get(int(cls), str(int(cls))): int((scl == cls).sum())
        for cls in np.unique(scl.astype(int))
    }
    observation = Observation(
        date=date,
        value=round(value, 6),
        variable="NDVI",
        source="sentinel-2-l2a",
        scene_id=scene_id,
        cloud_cover=cloud_cover,
        valid_fraction=round(valid_fraction, 4),
        n_valid_pixels=n_valid,
        quality_flag=quality,
        metadata={
            "platform": item.get("properties", {}).get("platform"),
            "processing_baseline": item.get("properties", {}).get("s2:processing_baseline"),
            "tile": item.get("properties", {}).get("grid:code"),
            "n_total_pixels": n_total,
            "ndvi_min": round(float(np.min(ndvi)), 4),
            "ndvi_max": round(float(np.max(ndvi)), 4),
            "ndvi_p25": round(float(np.percentile(ndvi, 25)), 4),
            "ndvi_p75": round(float(np.percentile(ndvi, 75)), 4),
            "scl_class_counts": scl_counts,
            "red_scale": red_scale,
            "red_offset": red_offset,
            "scl_valid_classes": sorted(SCL_VALID),
        },
    )
    return observation, None


def _limit_evenly(items: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    if len(items) <= max_items:
        return items
    step = len(items) / max_items
    return [items[min(int(i * step), len(items) - 1)] for i in range(max_items)]


class Sentinel2Adapter:
    source_id = "sentinel-2-l2a"

    def __init__(
        self,
        cache: Cache,
        session: requests.Session,
        *,
        workers: int = 8,
        timeout: float = 60,
    ):
        self.cache = cache
        self.session = session
        self.workers = max(1, workers)
        self.stac = StacClient(cache, session, timeout=timeout)

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
        try:
            items, search_meta = self.stac.search(
                self.source_id,
                field.bbox_padded,
                start,
                end,
                cloud_cover_max=cloud_cover_max,
                limit=100,
                max_items=max_scenes * 2,
                endpoint=EARTH_SEARCH,
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="error",
                    message=f"STAC search failed: {exc}",
                ),
                metadata={"requested_start": start, "requested_end": end},
            )

        items = StacClient.sort_items(items)
        items = _limit_evenly(items, max_scenes)
        observations: list[Observation] = []
        failures: dict[str, int] = {}
        field_key = _field_key(field)

        def cached_or_fetch(item: dict[str, Any]) -> tuple[Observation | None, str | None, bool]:
            scene_id = item.get("id", "unknown")
            key = f"{field_key}:{scene_id}:{cloud_cover_max}:{min_valid_fraction}:{min_valid_pixels}"
            cached = self.cache.get_json("s2_scene_ndvi", key)
            if cached is not None:
                return Observation(**cached), None, True
            observation, error = _sample_scene(
                item, field, min_valid_fraction, min_valid_pixels
            )
            if observation is not None:
                self.cache.set_json("s2_scene_ndvi", key, observation.to_dict())
            return observation, error, False

        cached_count = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(cached_or_fetch, item): item for item in items}
            for future in as_completed(futures):
                try:
                    observation, error, was_cached = future.result()
                except Exception as exc:  # pragma: no cover - network dependent
                    failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
                    continue
                if observation is not None:
                    observations.append(observation)
                    cached_count += int(was_cached)
                elif error:
                    failures[error] = failures.get(error, 0) + 1

        observations.sort(key=lambda o: o.date)
        status_name = "ok" if observations else "error"
        if observations and (failures or len(observations) < len(items)):
            status_name = "partial"
        message = (
            f"{len(observations)} usable scenes from {len(items)} candidates "
            f"({len(failures)} distinct rejection reasons)"
            if items
            else "No Sentinel-2 scenes matched the search"
        )
        metadata = {
            "requested_start": start,
            "requested_end": end,
            "cloud_cover_max": cloud_cover_max,
            "min_valid_fraction": min_valid_fraction,
            "min_valid_pixels": min_valid_pixels,
            "max_scenes": max_scenes,
            "scenes_matched": len(items),
            "scenes_used": len(observations),
            "cached_scenes": cached_count,
            "rejection_reasons": failures,
            "scl_valid_classes": sorted(SCL_VALID),
            "search": search_meta,
            "spatial_resolution": "10 m (B04/B08); 20 m SCL",
            "temporal_resolution": "about 5 days constellation revisit",
            "license": "Copernicus open data, attribution required",
            "accessed_at": utc_now_iso(),
        }
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status=status_name,
                message=message,
                records=len(observations),
                cached=cached_count > 0,
                metadata={"scenes_matched": len(items)},
            ),
            observations=observations,
            metadata=metadata,
        )
