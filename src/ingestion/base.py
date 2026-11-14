"""Shared ingestion types.

Every adapter returns a :class:`SourceResult`.  A failed or unavailable source
is represented explicitly instead of being silently replaced with synthetic
values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SourceState = Literal["ok", "partial", "unavailable", "skipped", "error"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class SourceStatus:
    source_id: str
    status: SourceState
    message: str = ""
    records: int = 0
    accessed_at: str = field(default_factory=utc_now_iso)
    cached: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "message": self.message,
            "records": self.records,
            "accessed_at": self.accessed_at,
            "cached": self.cached,
            "metadata": self.metadata,
        }


@dataclass
class Observation:
    """One measured value for one variable at one time.

    ``quality_flag`` must make clear whether the value is a measurement,
    a composite, or a synthetic value.  Interpolated values are never stored
    here; they belong to the processed series.
    """

    date: str
    value: float
    variable: str
    source: str
    scene_id: str | None = None
    cloud_cover: float | None = None
    valid_fraction: float | None = None
    n_valid_pixels: int | None = None
    quality_flag: str = "observed"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "value": self.value,
            "variable": self.variable,
            "source": self.source,
            "scene_id": self.scene_id,
            "cloud_cover": self.cloud_cover,
            "valid_fraction": self.valid_fraction,
            "n_valid_pixels": self.n_valid_pixels,
            "quality_flag": self.quality_flag,
            "metadata": self.metadata,
        }


@dataclass
class SourceResult:
    source_id: str
    status: SourceStatus
    observations: list[Observation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status.status in ("ok", "partial") and bool(self.observations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status.to_dict(),
            "observations": [o.to_dict() for o in self.observations],
            "metadata": self.metadata,
        }


class SourceError(RuntimeError):
    """Base class for adapter failures that should be reported, not hidden."""


class SourceUnavailable(SourceError):
    """The source cannot be used in this environment (credentials, service, etc.)."""


class SourceDataError(SourceError):
    """The source responded but the data could not be interpreted."""
