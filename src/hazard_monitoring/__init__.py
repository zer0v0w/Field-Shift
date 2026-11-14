"""Environmental Hazard Monitoring module.

Monitors NASA EONET v3 (wildfires, storms, floods, etc.) and USGS earthquakes
geographically around a farmer's field. Provides alerts, distance calculations,
and event history. Completely separate from the crop-rotation recommendation engine.

Design notes:
- EONET is not a comprehensive real-time warning service; data may be delayed.
- Do not invent severity classifications. Use available magnitude/status/distance.
- Do not classify every nearby event as dangerous; present relevance transparently.
- Handle missing, delayed and unavailable data without raising to callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any, Dict, List, Optional, Sequence, Set

# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #

EONET_BASE = "https://eonet.gsfc.nasa.gov/api/v3"
USGS_EVENTS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two points."""
    lat1_r, lon1_r = radians(lat1), radians(lon1)
    lat2_r, lon2_r = radians(lat2), radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(min(max(a, 0.0), 1.0)))
    return 6371.0 * c


def _parse_iso_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # epoch ms or s
        try:
            v = float(value)
            if v > 1e12:  # ms
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    # try epoch string
    try:
        if s.isdigit():
            v = float(s)
            if v > 1e12:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
    except Exception:
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # common loose formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Event data structures
# --------------------------------------------------------------------------- #


@dataclass
class HazardEvent:
    """Represents a natural hazard event from any source."""

    id: str
    source: str
    title: str
    event_type: str
    description: str | None
    latitude: float | None
    longitude: float | None
    timestamp: datetime
    magnitude: float | None
    depth_km: float | None = None
    status: str = "open"
    source_url: str | None = None
    distance_km: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_recent(self, hours: int = 48) -> bool:
        try:
            age = (_now_utc() - self.timestamp).total_seconds() / 3600
            return age <= hours
        except Exception:
            return False

    def is_near(self, radius_km: float) -> bool:
        return self.distance_km is not None and self.distance_km <= radius_km

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "event_type": self.event_type,
            "description": self.description,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "magnitude": self.magnitude,
            "depth_km": self.depth_km,
            "status": self.status,
            "distance_km": self.distance_km,
            "source_url": self.source_url,
        }
        # include non-conflicting metadata
        for k, v in (self.metadata or {}).items():
            if k not in d:
                d[k] = v
        return d


# --------------------------------------------------------------------------- #
# NASA EONET v3 client
# --------------------------------------------------------------------------- #


def _eonet_event_type(raw: dict[str, Any]) -> str:
    cats = raw.get("categories") or []
    if isinstance(cats, list) and cats:
        c0 = cats[0] or {}
        if isinstance(c0, dict):
            # prefer stable id fragment/title
            cid = c0.get("id") or ""
            title = (c0.get("title") or "").strip()
            if "wildfire" in str(cid).lower() or "wildfire" in title.lower():
                return "wildfire"
            if "storm" in str(cid).lower() or "storm" in title.lower() or "cyclone" in title.lower():
                return "storm"
            if "flood" in str(cid).lower() or "flood" in title.lower():
                return "flood"
            if "volcano" in str(cid).lower() or "volcano" in title.lower():
                return "volcano"
            if "ice" in str(cid).lower() or "ice" in title.lower():
                return "seaLakeIce"
            if "drought" in str(cid).lower() or "drought" in title.lower():
                return "drought"
            if title:
                return title.lower().replace(" ", "_")
    return str(raw.get("event_type") or "unknown")


def _eonet_coords(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    # v3: geometry is usually a list of {"magnitudeValue":..,"magnitudeUnit":..,"date":..,"type":"Point","coordinates":[lon,lat]}
    geom = raw.get("geometry")
    if isinstance(geom, list) and geom:
        # prefer most recent point
        last = geom[-1] if isinstance(geom[-1], dict) else geom[0]
        if isinstance(last, dict):
            coords = last.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                try:
                    return float(coords[1]), float(coords[0])
                except Exception:
                    pass
        # sometimes flat
        if len(geom) >= 2 and all(isinstance(x, (int, float, str)) for x in geom[:2]):
            try:
                return float(geom[1]), float(geom[0])
            except Exception:
                pass
    return None, None


def _eonet_timestamp(raw: dict[str, Any]) -> datetime:
    geom = raw.get("geometry")
    if isinstance(geom, list) and geom:
        last = geom[-1] if isinstance(geom[-1], dict) else None
        if isinstance(last, dict):
            ts = _parse_iso_ts(last.get("date"))
            if ts:
                return ts
    # fallback: closed date? or sources? v3 has no top-level time list typically
    ts = _parse_iso_ts(raw.get("closed"))
    if ts:
        return ts
    # try sources dates
    for src in raw.get("sources") or []:
        if isinstance(src, dict):
            ts = _parse_iso_ts(src.get("date"))
            if ts:
                return ts
    return _now_utc()


def fetch_eonet_events(days_back: int = 30, limit: int = 200) -> list[HazardEvent]:
    """Fetch events from NASA EONET v3. Returns empty list on failure."""
    try:
        import requests  # type: ignore
    except Exception:
        return []

    params = {"status": "all", "limit": str(int(limit))}
    if days_back and days_back > 0:
        try:
            start = (_now_utc() - timedelta(days=int(days_back))).date().isoformat()
            params["start"] = start
        except Exception:
            pass

    try:
        resp = requests.get(f"{EONET_BASE}/events", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception:
        return []

    events: list[HazardEvent] = []
    seen: Set[str] = set()
    for raw in data.get("events", []) or []:
        if not isinstance(raw, dict):
            continue
        eid = str(raw.get("id") or "").strip()
        if not eid or eid in seen:
            continue
        seen.add(eid)

        lat, lon = _eonet_coords(raw)
        ts = _eonet_timestamp(raw)
        etype = _eonet_event_type(raw)

        title = str(raw.get("title") or "").strip()
        if not title:
            cats = raw.get("categories") or []
            if isinstance(cats, list) and cats and isinstance(cats[0], dict):
                title = str(cats[0].get("title") or "Hazard event")
            else:
                title = "Hazard event"

        status = "open" if not raw.get("closed") else "closed"
        link = None
        try:
            link = (raw.get("link") or None)
        except Exception:
            link = None
        # sources may contain url
        if not link:
            for s in raw.get("sources") or []:
                if isinstance(s, dict) and s.get("url"):
                    link = str(s.get("url"))
                    break

        events.append(
            HazardEvent(
                id=eid,
                source="eonet",
                title=title,
                event_type=etype,
                description=raw.get("description"),
                latitude=lat,
                longitude=lon,
                timestamp=ts,
                magnitude=None,
                depth_km=None,
                status=status,
                source_url=link,
                distance_km=None,
                metadata={},
            )
        )
    return events


# --------------------------------------------------------------------------- #
# USGS Earthquake client
# --------------------------------------------------------------------------- #


def _usgs_query(params: dict[str, Any], timeout: float = 20) -> list[dict[str, Any]]:
    try:
        import requests  # type: ignore
    except Exception:
        return []
    try:
        resp = requests.get(USGS_EVENTS_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json() or {}
        feats = data.get("features") or []
        return [f for f in feats if isinstance(f, dict)]
    except Exception:
        return []


def fetch_usgs_earthquakes(
    days_back: Optional[int] = 30,
    min_mag: Optional[float] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    maxdist_km: Optional[float] = None,
    limit: int = 50,
) -> list[HazardEvent]:
    """Fetch recent earthquakes from USGS. Returns empty list on failure."""
    params: dict[str, Any] = {
        "format": "geojson",
        "orderby": "time",
        "limit": str(int(limit)),
    }
    now = _now_utc()
    if days_back is not None:
        try:
            params["starttime"] = (now - timedelta(days=int(days_back))).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            pass
    params["endtime"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    if min_mag is not None:
        params["minmagnitude"] = str(min_mag)
    if latitude is not None and longitude is not None and maxdist_km is not None:
        params["latitude"] = str(latitude)
        params["longitude"] = str(longitude)
        params["maxradiuskm"] = str(maxdist_km)

    features = _usgs_query(params, timeout=20)
    events: list[HazardEvent] = []
    for feat in features:
        try:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            lon = float(coords[0]) if len(coords) >= 1 else None
            lat = float(coords[1]) if len(coords) >= 2 else None
            depth = float(coords[2]) if len(coords) >= 3 else None
            ts = _parse_iso_ts(props.get("time")) or _now_utc()
            mag = props.get("mag")
            try:
                mag = float(mag) if mag is not None else None
            except Exception:
                mag = None
            eid = str(feat.get("id") or props.get("code") or "").strip()
            if not eid:
                continue
            dist = None
            if latitude is not None and longitude is not None and lat is not None and lon is not None:
                try:
                    dist = _haversine_km(float(latitude), float(longitude), lat, lon)
                except Exception:
                    dist = None
            events.append(
                HazardEvent(
                    id=eid,
                    source="usgs",
                    title=str(props.get("title") or props.get("place") or "Earthquake"),
                    event_type="earthquake",
                    description=props.get("place"),
                    latitude=lat,
                    longitude=lon,
                    timestamp=ts,
                    magnitude=mag,
                    depth_km=depth,
                    status=str(props.get("status") or "reviewed"),
                    source_url=props.get("url"),
                    distance_km=dist,
                    metadata={
                        "mmi": props.get("mmi"),
                        "sig": props.get("sig"),
                        "tsunami": props.get("tsunami"),
                    },
                )
            )
        except Exception:
            continue
    return events


# --------------------------------------------------------------------------- #
# Core monitoring logic
# --------------------------------------------------------------------------- #


@dataclass
class HazardMonitor:
    """Manages event fetching, deduplication, and relevance filtering."""

    field_lat: Optional[float] = None
    field_lon: Optional[float] = None
    alert_radius_km: float = 100.0

    def set_field(self, lat: float, lon: float) -> None:
        self.field_lat = float(lat)
        self.field_lon = float(lon)

    def set_radius(self, km: float) -> None:
        self.alert_radius_km = float(km)

    def update_field(self, lat: float, lon: float, radius_km: float | None = None) -> None:
        self.set_field(lat, lon)
        if radius_km is not None:
            self.set_radius(radius_km)

    def _annotate_distances(self, events: Sequence[HazardEvent]) -> list[HazardEvent]:
        out: list[HazardEvent] = []
        for ev in events:
            try:
                if self.field_lat is not None and self.field_lon is not None and ev.latitude is not None and ev.longitude is not None:
                    ev.distance_km = _haversine_km(self.field_lat, self.field_lon, ev.latitude, ev.longitude)
                else:
                    if ev.distance_km is None:
                        ev.distance_km = None
            except Exception:
                ev.distance_km = None
            out.append(ev)
        return out

    def _within_radius(self, events: Sequence[HazardEvent]) -> list[HazardEvent]:
        if self.field_lat is None or self.field_lon is None:
            # no location context: cannot meaningfully filter by proximity
            return list(events)
        return [e for e in events if e.distance_km is not None and e.distance_km <= float(self.alert_radius_km)]

    def fetch_all_events(
        self,
        *,
        eonet_days_back: int = 30,
        usgs_days_back: Optional[int] = 30,
    ) -> dict[str, list[HazardEvent]]:
        """Fetch from both EONET and USGS, compute distances, return dict keyed by source."""
        result: dict[str, list[HazardEvent]] = {"eonet": [], "usgs": []}
        try:
            eonet = fetch_eonet_events(days_back=eonet_days_back)
        except Exception:
            eonet = []
        try:
            usgs = fetch_usgs_earthquakes(
                days_back=usgs_days_back,
                latitude=self.field_lat,
                longitude=self.field_lon,
                maxdist_km=self.alert_radius_km,
            )
        except Exception:
            usgs = []

        # dedupe and annotate
        seen_e: Set[str] = set()
        e_list: list[HazardEvent] = []
        for ev in self._annotate_distances(eonet):
            if ev.id and ev.id not in seen_e:
                seen_e.add(ev.id)
                e_list.append(ev)
        result["eonet"] = self._within_radius(e_list)

        seen_u: Set[str] = set()
        u_list: list[HazardEvent] = []
        for ev in self._annotate_distances(usgs):
            if ev.id and ev.id not in seen_u:
                seen_u.add(ev.id)
                u_list.append(ev)
        result["usgs"] = self._within_radius(u_list)
        return result

    def get_active_alerts(self) -> List[HazardEvent]:
        """Return relevant nearby events for alerting.

        Includes recent events and currently-open EONET events (even if older),
        since EONET reports ongoing natural events that may remain relevant.
        """
        all_events: List[HazardEvent] = []
        try:
            sources = self.fetch_all_events()
            for src_events in sources.values():
                all_events.extend(src_events)
        except Exception:
            return []

        relevant: List[HazardEvent] = []
        now = _now_utc()
        for ev in all_events:
            try:
                age_h = (now - ev.timestamp).total_seconds() / 3600.0
            except Exception:
                age_h = 9999.0
            status = (ev.status or "").lower()
            # USGS: recent quakes; EONET: open or recent
            if ev.source == "usgs":
                if age_h <= 168 and (ev.distance_km is not None):
                    relevant.append(ev)
                continue
            # EONET and others
            if status == "open":
                # ongoing/recently updated events are relevant if nearby
                if ev.distance_km is not None and age_h <= 24 * 30:
                    relevant.append(ev)
            else:
                if age_h <= 168 and ev.distance_km is not None:
                    relevant.append(ev)

        # If we have location, events are already radius-filtered; if not, keep none to avoid false alarms
        if self.field_lat is None or self.field_lon is None:
            relevant = []

        relevant.sort(key=lambda e: (e.distance_km if e.distance_km is not None else 1e9, e.timestamp.timestamp() * -1))
        return relevant

    def get_recent_history(self, hours: int = 168) -> List[HazardEvent]:
        """Return events from the last *hours* within radius (best-effort)."""
        try:
            sources = self.fetch_all_events()
        except Exception:
            return []
        all_events: List[HazardEvent] = []
        for src_events in sources.values():
            all_events.extend(src_events)
        cutoff = _now_utc() - timedelta(hours=int(hours))
        recent = [e for e in all_events if (e.timestamp >= cutoff)]
        recent.sort(key=lambda e: e.timestamp, reverse=True)
        return recent


# --------------------------------------------------------------------------- #
# Farmer-friendly helpers
# --------------------------------------------------------------------------- #


def event_risk_label(event: HazardEvent) -> str:
    """Farmer-friendly relevance label (not a definitive danger classification)."""
    d = event.distance_km
    etype = (event.event_type or "").lower()
    mag = event.magnitude
    status = (event.status or "").lower()

    if d is None:
        dist_label = "Distance unknown"
    elif d <= 10:
        dist_label = "Very near (<10 km)"
    elif d <= 50:
        dist_label = "Near (10–50 km)"
    elif d <= 200:
        dist_label = "In wider area (50–200 km)"
    else:
        dist_label = "Far (>200 km)"

    parts: list[str] = [dist_label]

    if "earthquake" in etype:
        if mag is not None:
            if mag >= 7.0:
                parts.append("Major earthquake reported")
            elif mag >= 5.0:
                parts.append("Strong earthquake reported")
            elif mag >= 3.0:
                parts.append("Light earthquake reported")
            else:
                parts.append("Minor earthquake reported")
        else:
            parts.append("Earthquake reported")
    elif "fire" in etype or "wildfire" in etype:
        parts.append("Wildfire event reported")
    elif "flood" in etype:
        parts.append("Flood-related event reported")
    elif "storm" in etype or "cyclone" in etype:
        parts.append("Storm/cyclone event reported")
    elif "volcano" in etype:
        parts.append("Volcanic activity reported")
    elif "drought" in etype:
        parts.append("Drought event reported")
    else:
        parts.append(f"Event reported ({event.event_type})")

    if status == "open":
        parts.append("[ongoing]")
    elif status == "forecast":
        parts.append("[forecast]")
    elif status == "closed":
        parts.append("[reported/closed]")

    return " — ".join(parts[:2]) + (" " + " ".join(parts[2:]) if len(parts) > 2 else "")


def event_severity_label(event: HazardEvent) -> str | None:
    """Return a severity label only if the source provides clear info; else None."""
    mag = event.magnitude
    etype = (event.event_type or "").lower()
    if "earthquake" in etype and mag is not None:
        if mag >= 7.0:
            return "Major"
        if mag >= 5.5:
            return "Strong"
        if mag >= 4.0:
            return "Moderate"
        if mag >= 2.5:
            return "Light"
        return "Minor"
    return None
