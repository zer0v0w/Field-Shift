"""Landsat Collection 2 Level-2 NDVI adapter (Planetary Computer)."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.warp import reproject

from ..preprocessing.geometry import FieldGeometry
from ..preprocessing.quality import classify_ndvi_quality
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache
from .cog import read_window
from .stac import PLANETARY_COMPUTER, StacClient

LANDSAT = "landsat-c2-l2"


def _field_key(field: FieldGeometry) -> str:
    return hashlib.sha256(field.geometry.wkb).hexdigest()[:16]


def _align(
    source: np.ndarray,
    mask: np.ndarray,
    src_transform: Any,
    src_crs: Any,
    dst_shape: tuple[int, int],
    dst_transform: Any,
    dst_crs: Any,
) -> tuple[np.ndarray, np.ndarray]:
    if source.shape == dst_shape:
        return source, mask
    destination = np.zeros(dst_shape, dtype="float32")
    reproject(
        source=source,
        destination=destination,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=0,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    mask_dest = np.zeros(dst_shape, dtype="uint8")
    reproject(
        source=mask.astype("uint8"),
        destination=mask_dest,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=0,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    return destination, mask_dest.astype(bool)


def landsat_clear_mask(qa: np.ndarray) -> np.ndarray:
    """QA_PIXEL clear-sky mask for Landsat Collection 2.

    Bits: 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow,
    5 snow, 6 clear, 7 water.
    """
    qa = qa.astype("uint16")
    bad = (
        ((qa & 1) != 0)
        | ((qa & 2) != 0)
        | ((qa & 4) != 0)
        | ((qa & 8) != 0)
        | ((qa & 16) != 0)
        | ((qa & 32) != 0)
        | ((qa & 128) != 0)
    )
    clear = (qa & 64) != 0
    return clear & ~bad


def _sample_scene(
    item: dict[str, Any],
    field: FieldGeometry,
    min_valid_fraction: float,
    min_valid_pixels: int,
    stac: StacClient,
) -> tuple[Observation | None, str | None]:
    scene_id = item.get("id", "unknown")
    red_href = StacClient.asset_href(item, "red")
    nir_href = StacClient.asset_href(item, "nir08", "nir")
    qa_href = StacClient.asset_href(item, "qa_pixel")
    if not red_href or not nir_href or not qa_href:
        return None, "missing red/nir/qa_pixel asset"
    red_href = stac.sign_pc_asset(LANDSAT, red_href)
    nir_href = stac.sign_pc_asset(LANDSAT, nir_href)
    qa_href = stac.sign_pc_asset(LANDSAT, qa_href)

    red_meta = StacClient.asset_band_meta(item, "red")
    nir_meta = StacClient.asset_band_meta(item, "nir08")
    red_scale = float(red_meta.get("scale", 2.75e-05))
    red_offset = float(red_meta.get("offset", -0.2))
    nir_scale = float(nir_meta.get("scale", 2.75e-05))
    nir_offset = float(nir_meta.get("offset", -0.2))

    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_USE_HEAD="YES",
        GDAL_HTTP_MULTIRANGE="YES",
    ):
        red = read_window(red_href, field.geometry, band=1)
        nir = read_window(nir_href, field.geometry, band=1)
        qa = read_window(qa_href, field.geometry, band=1)

    nir_array, nir_mask = _align(
        nir.array, nir.valid_mask, nir.transform, nir.crs, red.shape, red.transform, red.crs
    )
    qa_array, qa_mask = _align(
        qa.array, qa.valid_mask, qa.transform, qa.crs, red.shape, red.transform, red.crs
    )

    red_ref = red.array * red_scale + red_offset
    nir_ref = nir_array * nir_scale + nir_offset
    valid = red.geom_mask & red.valid_mask & nir_mask & qa_mask
    valid &= landsat_clear_mask(qa_array)
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
    date = StacClient.item_date(item)
    if date is None:
        return None, "no scene date"
    props = item.get("properties", {})
    observation = Observation(
        date=date,
        value=round(value, 6),
        variable="NDVI",
        source=LANDSAT,
        scene_id=scene_id,
        cloud_cover=StacClient.cloud_cover(item),
        valid_fraction=round(valid_fraction, 4),
        n_valid_pixels=n_valid,
        quality_flag=classify_ndvi_quality(valid_fraction, StacClient.cloud_cover(item), min_valid_fraction),
        metadata={
            "platform": props.get("platform"),
            "wrs_path": props.get("landsat:wrs_path"),
            "wrs_row": props.get("landsat:wrs_row"),
            "collection": props.get("landsat:collection_number"),
            "n_total_pixels": n_total,
            "ndvi_min": round(float(np.min(ndvi)), 4),
            "ndvi_max": round(float(np.max(ndvi)), 4),
            "red_scale": red_scale,
            "red_offset": red_offset,
            "note": "Landsat surface reflectance; do not mix with Sentinel-2 without harmonisation.",
        },
    )
    return observation, None


class LandsatAdapter:
    source_id = LANDSAT

    def __init__(
        self,
        cache: Cache,
        session: requests.Session,
        *,
        workers: int = 6,
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
        max_scenes: int = 300,
    ) -> SourceResult:
        try:
            items, search_meta = self.stac.search(
                self.source_id,
                field.bbox_padded,
                start,
                end,
                cloud_cover_max=cloud_cover_max,
                limit=100,
                max_items=max_scenes,
                endpoint=PLANETARY_COMPUTER,
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

        items = StacClient.sort_items(items)[:max_scenes]
        observations: list[Observation] = []
        failures: dict[str, int] = {}
        field_key = _field_key(field)

        def cached_or_fetch(item: dict[str, Any]) -> tuple[Observation | None, str | None, bool]:
            scene_id = item.get("id", "unknown")
            key = f"{field_key}:{scene_id}:{min_valid_fraction}:{min_valid_pixels}"
            cached = self.cache.get_json("landsat_scene_ndvi", key)
            if cached is not None:
                return Observation(**cached), None, True
            observation, error = _sample_scene(
                item, field, min_valid_fraction, min_valid_pixels, self.stac
            )
            if observation is not None:
                self.cache.set_json("landsat_scene_ndvi", key, observation.to_dict())
            return observation, error, False

        cached_count = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(cached_or_fetch, item): item for item in items}
            for future in as_completed(futures):
                try:
                    observation, error, was_cached = future.result()
                except Exception as exc:  # pragma: no cover
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
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status=status_name,
                message=(
                    f"{len(observations)} usable scenes from {len(items)} candidates"
                    if items
                    else "No Landsat scenes matched the search"
                ),
                records=len(observations),
                cached=cached_count > 0,
            ),
            observations=observations,
            metadata={
                "requested_start": start,
                "requested_end": end,
                "scenes_matched": len(items),
                "scenes_used": len(observations),
                "cached_scenes": cached_count,
                "rejection_reasons": failures,
                "search": search_meta,
                "spatial_resolution": "30 m reflective bands",
                "temporal_resolution": "16-day repeat cycle",
                "license": "USGS public domain",
                "accessed_at": utc_now_iso(),
            },
        )
