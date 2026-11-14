"""Project-wide settings and paths.

Values here are deliberately conservative defaults.  Analysis-specific
parameters are passed explicitly through the CLI / pipeline config so that
every run records what it used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
PROCESSED_DIR = DATA_DIR / "processed"
SOURCES_DIR = DATA_DIR / "sources"
RESULTS_DIR = PROJECT_ROOT / "results"
DOCS_DIR = PROJECT_ROOT / "docs"
VENDOR_DIR = PROJECT_ROOT / "vendor"

CACHE_DIR = Path(os.environ.get("AGRI_CACHE_DIR", DOWNLOADS_DIR / "cache"))
CROP_CATALOG_PATH = CONFIG_DIR / "crop_catalog.json"
SCORING_CONFIG_PATH = CONFIG_DIR / "scoring.json"
ROTATIONS_PATH = CONFIG_DIR / "rotations.json"
SOURCES_CATALOG_PATH = CONFIG_DIR / "sources.json"

#: Field defaults
DEFAULT_BUFFER_M = 100.0
DEFAULT_CLOUD_COVER_MAX = 60.0
DEFAULT_MIN_VALID_FRACTION = 0.20
DEFAULT_MIN_VALID_PIXELS = 5
DEFAULT_MAX_SCENES = 400
DEFAULT_HTTP_WORKERS = 8
DEFAULT_TIMEOUT_S = 60

#: Crop-cycle defaults (upstream defaults, exposed so runs are reproducible)
CYCLE_DEFAULTS = {
    "ndvi_threshold_for_approx_season_start": 0.35,
    "ndvi_threshold_for_approx_season_end": 0.40,
    "min_season_length": 60,
    "ndvi_peak_percentile": 90.0,
    "ndvi_peak_threshold_ceiling": 0.70,
    "season_start_pct_of_peak": 0.35,
    "season_end_pct_of_peak": 0.35,
    "season_window_days": 90,
    "min_ndvi_for_peak": None,
}

BARE_SOIL_NDVI_MAX = 0.25
BARE_SOIL_MIN_DAYS = 10

#: Optional credentials.  Absence must never crash the default pipeline.
EARTHDATA_TOKEN = os.environ.get("EARTHDATA_TOKEN", "")
CDSAPI_URL = os.environ.get("CDSAPI_URL", "")
CDSAPI_KEY = os.environ.get("CDSAPI_KEY", "")
PRESTO_MODEL_PATH = Path(
    os.environ.get("PRESTO_MODEL_PATH", DOWNLOADS_DIR / "presto" / "default_model.pt")
)

DISCLAIMER = (
    "This prototype is a research experiment, not farming advice. Crop "
    "attributes are indicative ranges compiled from public agronomic "
    "references, not local field trials. Rotation scores are transparent "
    "decision-support heuristics and must not be read as guaranteed yield, "
    "profit, or environmental outcomes. Verify locally before acting."
)


@dataclass
class RuntimeSettings:
    cache_dir: Path = field(default_factory=lambda: CACHE_DIR)
    timeout_s: int = DEFAULT_TIMEOUT_S
    workers: int = DEFAULT_HTTP_WORKERS
    user_agent: str = "agri-rotation-intelligence/0.1 (research prototype)"

    def ensure_dirs(self) -> None:
        for path in (self.cache_dir, DOWNLOADS_DIR, PROCESSED_DIR, RESULTS_DIR):
            path.mkdir(parents=True, exist_ok=True)


def software_versions() -> dict[str, str]:
    """Return versions of libraries that materially affect results."""
    import platform

    versions = {"python": platform.python_version()}
    for name in ("numpy", "pandas", "scipy", "statsmodels", "geopandas", "rasterio", "shapely"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "unknown")
        except Exception:  # pragma: no cover - optional dependency
            versions[name] = "not installed"
    return versions
