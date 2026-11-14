"""MODIS MOD13Q1 16-day NDVI adapter (Planetary Computer)."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import requests

from ..preprocessing.geometry import FieldGeometry
from ..preprocessing.quality import classify_ndvi_quality
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache
from .cog import read_window
from .stac import PLANETARY_COMPUTER, StacClient

MOD13Q1 = "modis-13Q1-061"


def _field_key(field: FieldGeometry) -> str:
    return hashlib.sha256(field.geometry.wkb).hexdigest()[:16]


def _sample_scene(
    item: dict[str, Any],
    field: FieldGeometry,
    min_valid_fraction: float,
    min_valid_pixels: int,
    stac: StacClient,
) -> tuple[Observation | None, str | None]:
    scene_id = item.get("id", "unknown")
    ndvi_href = StacClient.asset_href(item, "250m_16_days_NDVI")
    qa_href = StacClient.asset_href(item, "250m_16_days_VI_Quality")
    if not ndvi_href or not qa_href:
        return None, "missing NDVI or VI quality asset"
    ndvi_href = stac.sign_pc_asset(MOD13Q1, ndvi_href)
    qa_href = stac.sign_pc_asset(MOD13Q1, qa_href)

    ndvi_read = read_window(ndvi_href, field.geometry, band=1)
    qa_read = read_window(qa_href, field.geometry, band=1)
    if qa_read.array.shape != ndvi_read.array.shape:
        return None, "NDVI and QA windows have different shapes"

    # MOD13Q1 detailed QA: summary quality bits 0-1 (00 good, 01 marginal,
    # 10 snow/ice, 11 cloudy).  Only "good" is accepted here.
    qa = qa_read.array.astype("uint16")
    summary_quality = qa & 0b11
    valid = ndvi_read.geom_mask & ndvi_read.valid_mask & qa_read.valid_mask
    valid &= summary_quality == 0
    raw_ndvi = ndvi_read.array.astype("float32")
    ndvi = raw_ndvi * 0.0001
    valid &= (raw_ndvi > -2000) & (raw_ndvi <= 10000)
    valid &= (ndvi >= -0.2) & (ndvi <= 1.0)

    n_total = int(ndvi_read.geom_mask.sum())
    n_valid = int(valid.sum())
    valid_fraction = n_valid / n_total if n_total else 0.0
    if n_valid < min_valid_pixels or valid_fraction < min_valid_fraction:
        return None, f"insufficient good-quality pixels ({n_valid}/{n_total})"

    values = ndvi[valid]
    value = float(np.median(values))
    date = StacClient.item_date(item)
    if date is None:
        return None, "no composite date"
    quality = classify_ndvi_quality(valid_fraction, StacClient.cloud_cover(item), min_valid_fraction)
    props = item.get("properties", {})
    observation = Observation(
        date=date,
        value=round(value, 6),
        variable="NDVI",
        source=MOD13Q1,
        scene_id=scene_id,
        cloud_cover=StacClient.cloud_cover(item),
        valid_fraction=round(valid_fraction, 4),
        n_valid_pixels=n_valid,
        quality_flag=quality,
        metadata={
            "composite_start": props.get("start_datetime"),
            "composite_end": props.get("end_datetime"),
            "platform": props.get("platform"),
            "n_total_pixels": n_total,
            "ndvi_min": round(float(np.min(values)), 4),
            "ndvi_max": round(float(np.max(values)), 4),
            "qa_summary_good_fraction": round(
                float((summary_quality[ndvi_read.geom_mask] == 0).mean()), 4
            )
            if n_total
            else None,
            "note": "16-day composite; date is the composite start",
        },
    )
    return observation, None


class ModisAdapter:
    source_id = MOD13Q1

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
        cloud_cover_max: float | None = None,
        min_valid_fraction: float = 0.50,
        min_valid_pixels: int = 1,
        max_scenes: int = 200,
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
            cached = self.cache.get_json("modis_scene_ndvi", key)
            if cached is not None:
                return Observation(**cached), None, True
            observation, error = _sample_scene(
                item, field, min_valid_fraction, min_valid_pixels, self.stac
            )
            if observation is not None:
                self.cache.set_json("modis_scene_ndvi", key, observation.to_dict())
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
                    f"{len(observations)} usable composites from {len(items)} candidates"
                    if items
                    else "No MODIS composites matched the search"
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
                "spatial_resolution": "250 m",
                "temporal_resolution": "16-day composite",
                "license": "NASA/USGS public domain",
                "note": "A small field occupies only a few 250 m pixels; this is regional context.",
                "accessed_at": utc_now_iso(),
            },
        )
