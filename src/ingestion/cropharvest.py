"""CropHarvest label context adapter.

CropHarvest is reused as a *label database*, not as a runtime field observation
source.  Its published package pins pandas<2 and geopandas==0.9, so this module
reads the published GeoJSON directly with the modern stack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

from ..preprocessing.geometry import FieldGeometry
from .base import SourceResult, SourceStatus, utc_now_iso
from .cache import Cache

ZENODO_LABELS_URL = "https://zenodo.org/records/10251170/files/labels.geojson?download=1"


class CropHarvestAdapter:
    source_id = "cropharvest-labels"

    def __init__(
        self,
        data_dir: Path,
        cache: Cache,
        session: requests.Session,
        *,
        timeout: float = 300,
    ):
        self.data_dir = Path(data_dir)
        self.cache = cache
        self.session = session
        self.timeout = timeout

    @property
    def labels_path(self) -> Path:
        return self.data_dir / "labels.geojson"

    def ensure_labels(self, download: bool = False) -> Path:
        if self.labels_path.exists() and self.labels_path.stat().st_size > 0:
            return self.labels_path
        if not download:
            raise FileNotFoundError(
                f"CropHarvest labels not found at {self.labels_path}. "
                "Run `python main.py cropharvest-labels --download` to fetch them."
            )
        self.cache.download_file(
            self.session,
            ZENODO_LABELS_URL,
            self.labels_path,
            timeout=self.timeout,
            headers={"User-Agent": "agri-rotation-intelligence/0.1"},
        )
        return self.labels_path

    def nearby(
        self,
        field: FieldGeometry,
        *,
        radius_km: float = 25.0,
        download: bool = False,
    ) -> SourceResult:
        try:
            path = self.ensure_labels(download=download)
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="unavailable",
                    message=str(exc),
                ),
                metadata={"zenodo_url": ZENODO_LABELS_URL},
            )

        lon, lat = field.centroid_lonlat
        # Degree padding is generous; exact distance filtering follows.
        pad = radius_km / 111.0
        bbox = (lon - pad, lat - pad, lon + pad, lat + pad)
        try:
            labels = gpd.read_file(path, bbox=bbox)
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="error",
                    message=f"Could not read CropHarvest labels: {exc}",
                ),
                metadata={"labels_path": str(path)},
            )

        if labels.empty:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="ok",
                    message="No CropHarvest labels within the search radius",
                    records=0,
                ),
                metadata={"radius_km": radius_km, "labels_path": str(path)},
            )

        # Local metric CRS for distance filtering.
        zone = min(max(int((lon + 180) / 6) + 1, 1), 60)
        epsg = (32600 if lat >= 0 else 32700) + zone
        local_crs = f"EPSG:{epsg}"
        try:
            projected = labels.to_crs(local_crs)
            centre = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(local_crs).iloc[0]
            distances_km = projected.geometry.distance(centre) / 1000.0
        except Exception:
            distances_km = pd.Series([float("nan")] * len(labels), index=labels.index)
        keep = distances_km <= radius_km
        nearby = labels.loc[keep].copy()
        nearby["distance_km"] = distances_km.loc[keep].round(2)
        if nearby.empty:
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="ok",
                    message=f"No CropHarvest labels within {radius_km} km",
                    records=0,
                ),
                metadata={"radius_km": radius_km, "labels_path": str(path)},
            )

        is_crop = nearby.get("is_crop")
        crop_count = int((is_crop == 1).sum()) if is_crop is not None else None
        label_counts = (
            nearby["label"].dropna().value_counts().head(20).to_dict()
            if "label" in nearby
            else {}
        )
        class_counts = (
            nearby["classification_label"].dropna().value_counts().head(20).to_dict()
            if "classification_label" in nearby
            else {}
        )
        dataset_counts = (
            nearby["dataset"].dropna().value_counts().head(20).to_dict()
            if "dataset" in nearby
            else {}
        )
        metadata: dict[str, Any] = {
            "radius_km": radius_km,
            "n_labels": int(len(nearby)),
            "n_crop_labels": crop_count,
            "crop_fraction": round(crop_count / len(nearby), 3) if crop_count is not None else None,
            "label_counts": label_counts,
            "classification_counts": class_counts,
            "dataset_counts": dataset_counts,
            "nearest_km": float(nearby["distance_km"].min()),
            "collection_date_range": {
                "min": str(nearby["collection_date"].min()) if "collection_date" in nearby else None,
                "max": str(nearby["collection_date"].max()) if "collection_date" in nearby else None,
            },
            "labels_path": str(path),
            "license": "CC-BY-SA-4.0",
            "zenodo_url": ZENODO_LABELS_URL,
            "note": (
                "CropHarvest labels are heterogeneous in space and time. They are "
                "context for model evaluation, not evidence of the selected field's "
                "current crop."
            ),
            "accessed_at": utc_now_iso(),
        }
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status="ok",
                message=f"{len(nearby)} CropHarvest labels within {radius_km} km",
                records=len(nearby),
            ),
            metadata=metadata,
        )
