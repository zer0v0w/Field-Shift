"""Adapters for sources that require credentials.

These are intentionally *probe-only* in milestone 1.  They verify that the
collection exists and count candidate granules via public CMR metadata, but
they do not fabricate values and they do not silently fall back to another
product.  Actual retrieval requires Earthdata/CDS credentials plus the relevant
HDF5/NetCDF tooling, and is left as a documented extension.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from ..preprocessing.geometry import FieldGeometry
from .base import SourceResult, SourceStatus, utc_now_iso
from .cache import Cache

CMR_COLLECTIONS = "https://cmr.earthdata.nasa.gov/search/collections.json"
CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.json"


def _cmr_count(
    session: requests.Session,
    short_name: str,
    bbox: tuple[float, float, float, float],
    start: str,
    end: str,
    *,
    timeout: float = 30,
) -> tuple[int | None, str | None]:
    params = {
        "short_name": short_name,
        "bounding_box": ",".join(f"{v:.5f}" for v in bbox),
        "temporal": f"{start}T00:00:00Z,{end}T23:59:59Z",
        "page_size": 0,
    }
    try:
        response = session.get(CMR_GRANULES, params=params, timeout=timeout)
        response.raise_for_status()
        h = response.headers.get("CMR-Hits") or response.headers.get("cmr-hits")
        if h is not None:
            try:
                return int(h), None
            except Exception:
                pass
        payload = response.json()
        hits = payload.get("feed", {}).get("hits")
        if hits is not None:
            return int(hits), None
        params["page_size"] = 1
        r2 = session.get(CMR_GRANULES, params=params, timeout=timeout)
        r2.raise_for_status()
        h2 = r2.headers.get("CMR-Hits") or r2.headers.get("cmr-hits")
        if h2 is not None:
            return int(h2), None
        return None, None
    except Exception as exc:
        return None, str(exc)


class CredentialedProbe:
    source_id = "unknown"
    short_name = ""
    product_name = ""
    required_env = "EARTHDATA_TOKEN"

    def __init__(
        self,
        cache: Cache,
        session: requests.Session,
        *,
        timeout: float = 30,
    ):
        self.cache = cache
        self.session = session
        self.timeout = timeout

    def probe(self, field: FieldGeometry, start: str, end: str) -> SourceResult:
        token = os.environ.get(self.required_env, "")
        hits, error = _cmr_count(
            self.session,
            self.short_name,
            field.bbox_padded,
            start,
            end,
            timeout=self.timeout,
        )
        if error:
            status = "error"
            message = f"CMR probe failed: {error}"
        elif not token:
            status = "unavailable"
            message = (
                f"{self.product_name} is available ({hits} candidate granules) but "
                f"{self.required_env} is not set. Set it to enable retrieval."
            )
        else:
            status = "unavailable"
            message = (
                f"{self.required_env} is set and {hits} candidate granules were found, "
                "but this milestone does not yet implement HDF5/NetCDF retrieval."
            )
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status=status,
                message=message,
                records=0,
            ),
            metadata={
                "short_name": self.short_name,
                "candidate_granules": hits,
                "requires": self.required_env,
                "credential_present": bool(token),
                "accessed_at": utc_now_iso(),
                "note": "Probe only; no values are returned.",
            },
        )


class SmapAdapter(CredentialedProbe):
    source_id = "smap"
    short_name = "SPL3SMP_E"
    product_name = "SMAP L3 soil moisture"


class ImergAdapter(CredentialedProbe):
    source_id = "gpm-imerg"
    short_name = "GPM_3IMERGDF"
    product_name = "GPM IMERG daily precipitation"


class Era5Adapter:
    source_id = "era5-agera5"

    def __init__(self, cache: Cache, session: requests.Session):
        self.cache = cache
        self.session = session

    def probe(self, field: FieldGeometry, start: str, end: str) -> SourceResult:
        configured = bool(os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY"))
        try:
            import cdsapi  # noqa: F401

            installed = True
        except Exception:
            installed = False
        if configured and installed:
            status = "unavailable"
            message = (
                "CDS credentials and cdsapi are configured, but ERA5/AgERA5 "
                "retrieval is not implemented in this milestone."
            )
        elif not installed:
            status = "unavailable"
            message = "ERA5/AgERA5 requires the optional `cdsapi` package and CDS credentials."
        else:
            status = "unavailable"
            message = "ERA5/AgERA5 requires CDSAPI_URL and CDSAPI_KEY; not configured."
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status=status,
                message=message,
                records=0,
            ),
            metadata={
                "configured": configured,
                "cdsapi_installed": installed,
                "accessed_at": utc_now_iso(),
                "note": "Probe only; NASA POWER is the default weather source.",
            },
        )
