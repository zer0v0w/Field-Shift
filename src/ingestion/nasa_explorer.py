"""Best-effort adapters for requested NASA datasets: SMAP, GPM IMERG, ECOSTRESS, GRACE/GRACE-FO.

These adapters probe NASA CMR for dataset availability and, when direct granule
retrieval is not possible in this environment (credentials/HDF5/NetCDF access),
provide transparently derived indicators from accessible NASA products already
used by the pipeline (e.g. NASA POWER). Outputs are clearly labeled in metadata
and must not be presented as direct sensor retrievals.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import requests

from ..preprocessing.geometry import FieldGeometry
from .base import Observation, SourceResult, SourceStatus, utc_now_iso
from .cache import Cache
from .nasa_power import NasaPowerAdapter

CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.json"


def _cmr_count(session: requests.Session, short_name: str, bbox, start: str, end: str, timeout: float = 25) -> int | None:
    params = {
        "short_name": short_name,
        "bounding_box": ",".join(f"{v:.5f}" for v in bbox),
        "temporal": f"{start}T00:00:00Z,{end}T23:59:59Z",
        "page_size": 0,
    }
    try:
        r = session.get(CMR_GRANULES, params=params, timeout=timeout)
        r.raise_for_status()
        h = r.headers.get("CMR-Hits") or r.headers.get("cmr-hits")
        if h is not None:
            try:
                return int(h)
            except Exception:
                pass
        hits = (r.json().get("feed") or {}).get("hits")
        if hits is not None:
            return int(hits)
        # fallback: query one entry
        params["page_size"] = 1
        r2 = session.get(CMR_GRANULES, params=params, timeout=timeout)
        r2.raise_for_status()
        h2 = r2.headers.get("CMR-Hits") or r2.headers.get("cmr-hits")
        if h2 is not None:
            return int(h2)
        return None
    except Exception:
        return None


def _status(source_id: str, status: str, message: str, records: int = 0, **meta) -> SourceResult:
    return SourceResult(
        source_id=source_id,
        status=SourceStatus(source_id=source_id, status=status, message=message, records=records),
        metadata=meta,
    )


class GpmImergExplorerAdapter:
    """Explore/use GPM IMERG precipitation (probe + accessible NASA precipitation analysis)."""
    source_id = "gpm-imerg"
    short_name = "GPM_3IMERGDF"

    def __init__(self, cache: Cache, session: requests.Session, timeout: float = 60):
        self.cache = cache
        self.session = session
        self.timeout = timeout
        self.power = NasaPowerAdapter(cache, session, timeout=90)

    def fetch(self, field: FieldGeometry, start: str, end: str, **_) -> SourceResult:
        hits = _cmr_count(self.session, self.short_name, field.bbox_padded, start, end)
        # Direct IMERG HDF5 retrieval requires Earthdata + parsing libs; use NASA POWER precip (NASA) as accessible input.
        power_res = self.power.fetch(field, start, end)
        if getattr(power_res.status, "status", "") in ("ok", "partial") and power_res.observations:
            obs = [
                Observation(
                    date=o.date,
                    value=o.value,
                    variable="precipitation_mm",
                    source=self.source_id,
                    quality_flag="derived_nasa_power",
                    metadata={"proxy_for": "GPM IMERG daily precipitation", "nasa_input": "NASA_POWER_PRECTOTCORR"},
                )
                for o in power_res.observations
                if getattr(o, "variable", "") == "PRECTOTCORR"
            ]
            vals = [o.value for o in obs]
            total = float(np.nansum(vals)) if vals else None
            return SourceResult(
                source_id=self.source_id,
                status=SourceStatus(
                    source_id=self.source_id,
                    status="ok" if obs else "partial",
                    message=(
                        f"GPM IMERG explored (CMR candidate granules: {hits}). "
                        "Direct IMERG retrieval unavailable without Earthdata/HDF5 in this environment; "
                        "daily precipitation analyzed from NASA POWER (NASA reanalysis/assimilation)."
                    ),
                    records=len(obs),
                ),
                observations=obs,
                metadata={
                    "dataset": "GPM IMERG (explored)",
                    "cmr_short_name": self.short_name,
                    "candidate_granules": hits,
                    "direct_retrieval": "unavailable_no_auth_or_hdf5",
                    "analysis_input": "NASA POWER PRECTOTCORR (NASA)",
                    "total_precip_mm_period": round(total, 2) if total is not None else None,
                    "accessed_at": utc_now_iso(),
                },
            )
        return _status(
            self.source_id,
            "unavailable",
            f"GPM IMERG explored (CMR candidate granules: {hits}) but precipitation inputs unavailable.",
            candidate_granules=hits,
            direct_retrieval="unavailable",
        )


class SmapExplorerAdapter:
    """Explore/use SMAP soil moisture (probe + NASA water-balance derived soil moisture indicator)."""
    source_id = "smap"
    short_name = "SPL3SMP_E"

    def __init__(self, cache: Cache, session: requests.Session, timeout: float = 60):
        self.cache = cache
        self.session = session
        self.timeout = timeout
        self.power = NasaPowerAdapter(cache, session, timeout=90)

    def fetch(self, field: FieldGeometry, start: str, end: str, **_) -> SourceResult:
        hits = _cmr_count(self.session, self.short_name, field.bbox_padded, start, end)
        power_res = self.power.fetch(field, start, end)
        if getattr(power_res.status, "status", "") not in ("ok", "partial") or not power_res.observations:
            return _status(self.source_id, "unavailable", f"SMAP explored (CMR granules: {hits}) but NASA inputs unavailable for soil moisture analysis.", candidate_granules=hits)

        # Build daily frame from NASA POWER
        rows = {}
        for o in power_res.observations:
            rows.setdefault(o.date, {})[o.variable] = o.value
        if not rows:
            return _status(self.source_id, "unavailable", f"SMAP explored (CMR granules: {hits}) but no usable NASA POWER records.", candidate_granules=hits)
        df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
        for c in ["PRECTOTCORR", "T2M", "T2M_MIN", "T2M_MAX", "ALLSKY_SFC_SW_DWN"]:
            if c not in df:
                df[c] = np.nan
        # Simple antecedent precipitation index / bucket model as soil moisture indicator (NASA-derived)
        precip = pd.to_numeric(df["PRECTOTCORR"], errors="coerce").fillna(0.0).clip(lower=0)
        tmean = pd.to_numeric(df["T2M"], errors="coerce")
        # crude loss factor increasing with temp (proxy evap demand)
        loss_factor = (0.85 + 0.01 * (tmean.fillna(20) - 20)).clip(0.75, 0.98)
        sm = []
        state = 20.0  # mm equivalent proxy
        cap = 100.0
        for p, lf in zip(precip.values, loss_factor.values):
            state = min(cap, (state + float(p)) * float(lf))
            sm.append(state / cap * 100.0)  # percent proxy
        obs = [
            Observation(
                date=str(idx),
                value=float(val),
                variable="soil_moisture_proxy_pct",
                source=self.source_id,
                quality_flag="derived_nasa_power_balance",
                metadata={"proxy_for": "SMAP surface/root-zone soil moisture patterns", "nasa_input": "NASA POWER precip/temp"},
            )
            for idx, val in zip(df.index, sm)
        ]
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status="ok",
                message=(
                    f"SMAP explored (CMR candidate granules: {hits}). "
                    "Direct SMAP retrieval unavailable without Earthdata/HDF5 in this environment; "
                    "soil moisture patterns analyzed via NASA POWER water-balance indicator."
                ),
                records=len(obs),
            ),
            observations=obs,
            metadata={
                "dataset": "SMAP (explored)",
                "cmr_short_name": self.short_name,
                "candidate_granules": hits,
                "direct_retrieval": "unavailable_no_auth_or_hdf5",
                "analysis_input": "NASA POWER PRECTOTCORR/T2M (NASA) water-balance proxy",
                "mean_soil_moisture_proxy_pct": round(float(np.mean(sm)), 2) if sm else None,
                "note": "Proxy indicator for soil moisture patterns, not direct SMAP measurement.",
                "accessed_at": utc_now_iso(),
            },
        )


class GraceExplorerAdapter:
    """Explore/use GRACE/GRACE-FO for long-term water trends (probe + NASA POWER trend indicator)."""
    source_id = "grace-fo"
    short_name = "TELLUS_GRAC-GRFO_MASCON_CRI_GRID_RL06.3_V4"

    def __init__(self, cache: Cache, session: requests.Session, timeout: float = 60):
        self.cache = cache
        self.session = session
        self.timeout = timeout
        self.power = NasaPowerAdapter(cache, session, timeout=90)

    def fetch(self, field: FieldGeometry, start: str, end: str, **_) -> SourceResult:
        hits = _cmr_count(self.session, self.short_name, field.bbox_padded, start, end)
        power_res = self.power.fetch(field, start, end)
        if getattr(power_res.status, "status", "") not in ("ok", "partial") or not power_res.observations:
            return _status(self.source_id, "unavailable", f"GRACE/GRACE-FO explored (CMR granules: {hits}) but inputs unavailable for trend analysis.", candidate_granules=hits)
        rows = {}
        for o in power_res.observations:
            rows.setdefault(o.date, {})[o.variable] = o.value
        df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
        precip = pd.to_numeric(df.get("PRECTOTCORR"), errors="coerce").fillna(0.0)
        # Monthly aggregation for trend
        try:
            idx = pd.to_datetime(df.index)
            monthly = precip.groupby([idx.year, idx.month]).sum()
            y = monthly.values.astype(float)
            if len(y) >= 6:
                x = np.arange(len(y), dtype=float)
                slope = float(np.polyfit(x, y, 1)[0])  # mm per month-step trend (proxy)
            else:
                slope = 0.0
        except Exception:
            slope = 0.0
            monthly = None
        obs = [
            Observation(
                date=str(d),
                value=float(v),
                variable="monthly_precip_mm",
                source=self.source_id,
                quality_flag="derived_nasa_power_trend",
                metadata={"proxy_for": "GRACE/GRACE-FO terrestrial water storage long-term trend context"},
            )
            for d, v in (monthly.items() if monthly is not None else [])
        ]
        trend_label = "stable_or_unclear"
        if slope < -0.5:
            trend_label = "decreasing_water_availability_signal"
        elif slope > 0.5:
            trend_label = "increasing_water_availability_signal"
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status="ok",
                message=(
                    f"GRACE/GRACE-FO explored (CMR candidate granules: {hits}). "
                    "Direct TWS mascon retrieval unavailable without Earthdata/netCDF in this environment; "
                    "long-term water availability trend analyzed from NASA POWER precipitation."
                ),
                records=len(obs),
            ),
            observations=obs,
            metadata={
                "dataset": "GRACE/GRACE-FO (explored)",
                "cmr_short_name": self.short_name,
                "candidate_granules": hits,
                "direct_retrieval": "unavailable_no_auth_or_netcdf",
                "analysis_input": "NASA POWER PRECTOTCORR (NASA) long-term trend indicator",
                "trend_slope_mm_per_month_step": round(slope, 4),
                "trend_signal": trend_label,
                "note": "Precipitation-based long-term trend indicator, not direct GRACE TWS anomaly.",
                "accessed_at": utc_now_iso(),
            },
        )


class EcostressExplorerAdapter:
    """Explore/use ECOSTRESS heat/evaporation stress (probe + NASA POWER-derived indicators)."""
    source_id = "ecostress"
    short_names = ["ECO2LSTE.001", "ECO4ESIPTJPL.001", "ECO3ETPTJPL.001"]

    def __init__(self, cache: Cache, session: requests.Session, timeout: float = 60):
        self.cache = cache
        self.session = session
        self.timeout = timeout
        self.power = NasaPowerAdapter(cache, session, timeout=90)

    def fetch(self, field: FieldGeometry, start: str, end: str, **_) -> SourceResult:
        # probe a couple of known ECOSTRESS short names
        hits = None
        used = None
        for sn in self.short_names:
            h = _cmr_count(self.session, sn, field.bbox_padded, start, end)
            if h is not None:
                hits = h
                used = sn
                if h > 0:
                    break
        power_res = self.power.fetch(field, start, end)
        if getattr(power_res.status, "status", "") not in ("ok", "partial") or not power_res.observations:
            return _status(self.source_id, "unavailable", f"ECOSTRESS explored (CMR granules: {hits}) but NASA inputs unavailable for stress analysis.", candidate_granules=hits, cmr_short_name=used)
        rows = {}
        for o in power_res.observations:
            rows.setdefault(o.date, {})[o.variable] = o.value
        df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
        tmax = pd.to_numeric(df.get("T2M_MAX"), errors="coerce")
        tmean = pd.to_numeric(df.get("T2M"), errors="coerce")
        # Heat stress proxy (days >=35C) and evaporation demand proxy via simple ET0-like factor from temp/radiation
        heat_days = int((tmax >= 35.0).sum()) if len(tmax) else 0
        rad = pd.to_numeric(df.get("ALLSKY_SFC_SW_DWN"), errors="coerce")
        et_proxy = (0.0023 * 0.408 * (tmean.fillna(20) + 17.8) * np.sqrt(((tmax - pd.to_numeric(df.get('T2M_MIN'), errors='coerce')).clip(lower=0)).fillna(8)) * rad.fillna(15)).clip(lower=0)
        obs = []
        for idx, val in et_proxy.items():
            obs.append(Observation(date=str(idx), value=float(val), variable="et_demand_proxy_mm_day", source=self.source_id, quality_flag="derived_nasa_power", metadata={"proxy_for": "ECOSTRESS ET/evaporation stress context"}))
        return SourceResult(
            source_id=self.source_id,
            status=SourceStatus(
                source_id=self.source_id,
                status="ok",
                message=(
                    f"ECOSTRESS explored (CMR candidate granules: {hits}, {used}). "
                    "Direct ECOSTRESS retrieval unavailable without Earthdata in this environment; "
                    "heat/evaporation stress indicators analyzed from NASA POWER temperature/radiation."
                ),
                records=len(obs),
            ),
            observations=obs,
            metadata={
                "dataset": "ECOSTRESS (explored)",
                "cmr_short_name": used,
                "candidate_granules": hits,
                "direct_retrieval": "unavailable_no_auth",
                "analysis_input": "NASA POWER T2M/T2M_MAX/T2M_MIN/ALLSKY_SFC_SW_DWN (NASA)",
                "heat_stress_days_ge_35c_proxy": heat_days,
                "mean_et_demand_proxy_mm_day": round(float(et_proxy.mean()), 3) if len(et_proxy) else None,
                "note": "Proxy indicators for heat/evaporation stress, not direct ECOSTRESS LST/ET retrieval.",
                "accessed_at": utc_now_iso(),
            },
        )
