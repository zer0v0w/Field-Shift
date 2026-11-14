"""Windowed reads from remote Cloud-Optimised GeoTIFFs.

Only the pixels intersecting the field are fetched.  This keeps a four-year
Sentinel-2 analysis to a few megabytes of range requests instead of hundreds of
full scenes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject
from rasterio.windows import Window, from_bounds
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_USE_HEAD": "YES",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


@dataclass
class WindowRead:
    array: np.ndarray
    valid_mask: np.ndarray
    geom_mask: np.ndarray
    transform: Any
    crs: Any
    nodata: float | None

    @property
    def shape(self) -> tuple[int, int]:
        return self.array.shape


@dataclass
class SampleResult:
    value: float | None
    n_valid: int
    n_total: int
    valid_fraction: float
    array: np.ndarray
    valid_mask: np.ndarray
    transform: Any
    crs: Any
    min: float | None = None
    max: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "n_valid": self.n_valid,
            "n_total": self.n_total,
            "valid_fraction": round(self.valid_fraction, 4),
            "min": self.min,
            "max": self.max,
        }


def _project_geometry(geometry: BaseGeometry, source_crs: str, target_crs: Any) -> BaseGeometry:
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    return shapely_transform(transformer.transform, geometry)


def _window_for_geometry(
    dataset: rasterio.DatasetReader, geometry: BaseGeometry
) -> Window:
    geom = _project_geometry(geometry, "EPSG:4326", dataset.crs)
    minx, miny, maxx, maxy = geom.bounds
    window = from_bounds(minx, miny, maxx, maxy, transform=dataset.transform)
    window = window.round_offsets().round_lengths()
    # Intersect with the raster extent.
    full = Window(0, 0, dataset.width, dataset.height)
    col_off = max(0, int(window.col_off))
    row_off = max(0, int(window.row_off))
    width = min(dataset.width - col_off, max(1, int(window.width)))
    height = min(dataset.height - row_off, max(1, int(window.height)))
    _ = full
    return Window(col_off, row_off, width, height)


def read_window(
    href: str,
    geometry: BaseGeometry,
    *,
    band: int = 1,
    all_touched: bool = True,
) -> WindowRead:
    with rasterio.Env(**GDAL_ENV):
        with rasterio.open(href) as dataset:
            window = _window_for_geometry(dataset, geometry)
            array = dataset.read(band, window=window).astype("float32")
            transform = dataset.window_transform(window)
            geom = _project_geometry(geometry, "EPSG:4326", dataset.crs)
            inside = geometry_mask(
                [geom],
                out_shape=array.shape,
                transform=transform,
                invert=True,
                all_touched=all_touched,
            )
            nodata = dataset.nodata
            valid = inside & np.isfinite(array)
            if nodata is not None and np.isfinite(nodata):
                valid &= array != nodata
            return WindowRead(
                array=array,
                valid_mask=valid,
                geom_mask=inside,
                transform=transform,
                crs=dataset.crs,
                nodata=nodata,
            )


def read_window_to_grid(
    href: str,
    geometry: BaseGeometry,
    *,
    dst_shape: tuple[int, int],
    dst_transform: Any,
    dst_crs: Any,
    band: int = 1,
    fill_value: float = 255.0,
    resampling: Resampling = Resampling.nearest,
) -> np.ndarray:
    """Reproject a source window onto an existing destination grid.

    Used to align the 20 m Sentinel-2 SCL layer with the 10 m red/NIR window.
    """
    read = read_window(href, geometry, band=band, all_touched=True)
    destination = np.full(dst_shape, fill_value, dtype="float32")
    source = read.array.copy()
    source[~read.valid_mask] = fill_value
    reproject(
        source=source,
        destination=destination,
        src_transform=read.transform,
        src_crs=read.crs,
        src_nodata=fill_value,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        dst_nodata=fill_value,
        resampling=resampling,
    )
    return destination


def sample_stat(
    href: str,
    geometry: BaseGeometry,
    *,
    band: int = 1,
    stat: str = "median",
    scale: float = 1.0,
    offset: float = 0.0,
    min_value: float | None = None,
    max_value: float | None = None,
    all_touched: bool = True,
) -> SampleResult:
    read = read_window(href, geometry, band=band, all_touched=all_touched)
    values = read.array[read.valid_mask]
    values = values * scale + offset
    if min_value is not None:
        values = values[values >= min_value]
    if max_value is not None:
        values = values[values <= max_value]
    raw_valid = int((read.valid_mask & read.geom_mask).sum())
    n_valid = int(values.size)
    n_total = int(read.geom_mask.sum())
    if n_valid == 0:
        value = None
    elif stat == "mean":
        value = float(np.mean(values))
    elif stat == "min":
        value = float(np.min(values))
    elif stat == "max":
        value = float(np.max(values))
    else:
        value = float(np.median(values))
    return SampleResult(
        value=value,
        n_valid=n_valid,
        n_total=n_total,
        valid_fraction=(raw_valid / n_total) if n_total else 0.0,
        array=read.array,
        valid_mask=read.valid_mask,
        transform=read.transform,
        crs=read.crs,
        min=float(np.min(values)) if n_valid else None,
        max=float(np.max(values)) if n_valid else None,
    )
