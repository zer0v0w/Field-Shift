#!/usr/bin/env python3
"""HTTP API for agri-intelligence, compatible with the Nasafarm Flutter frontend.

Serves existing analysis artifacts from results/, triggers new analyses via the
pipeline, and exposes crop catalog, sources, and hazard monitoring endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT_ROOT / "results"
CONFIG_DIR = PROJECT_ROOT / "config"

API_VERSION = "1.1.0"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,160}$")


def _python_executable() -> str:
    """Prefer project venv Python so pipeline deps are available."""
    for rel in (".venv/bin/python", ".venv/bin/python3", "venv/bin/python", "venv/bin/python3"):
        p = PROJECT_ROOT / rel
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return sys.executable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _try_read_json(path: Path) -> Any | None:
    try:
        if path.is_file():
            return _read_json(path)
    except Exception:
        return None
    return None


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _as_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _valid_run_dir(run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"Invalid run id: {run_id!r}")
    candidate = (RESULTS_DIR / run_id).resolve()
    if candidate.parent != RESULTS_DIR.resolve() or not candidate.is_dir():
        raise ValueError(f"Unknown run id: {run_id!r}")
    return candidate


def _list_run_dirs() -> list[Path]:
    if not RESULTS_DIR.is_dir():
        return []
    dirs = [p for p in RESULTS_DIR.iterdir() if p.is_dir() and RUN_ID_RE.match(p.name)]
    def mtime(p: Path) -> float:
        for name in ("field_summary.json", "manifest.json", "report_context.json"):
            fp = p / name
            if fp.exists():
                return fp.stat().st_mtime
        return p.stat().st_mtime
    dirs.sort(key=mtime, reverse=True)
    return dirs


def _load_artifacts(run_dir: Path) -> dict[str, Any]:
    names = [
        "field_summary",
        "manifest",
        "report_context",
        "rotation_analysis",
        "crop_cycles",
        "environmental_summary",
        "run_config",
    ]
    out: dict[str, Any] = {}
    for n in names:
        data = _try_read_json(run_dir / f"{n}.json")
        if data is not None:
            out[n] = data
    return out


def _first(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


def _get(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _parse_point_from_request(req: Any) -> tuple[float | None, float | None]:
    if not isinstance(req, dict):
        return None, None
    # common shapes: {"point": [lat,lon]} or {"lat":..,"lon":..} or {"point":"lat,lon"}
    pt = req.get("point")
    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
        return _as_float(pt[0]), _as_float(pt[1])
    if isinstance(pt, str) and "," in pt:
        parts = [p.strip() for p in pt.split(",")]
        if len(parts) >= 2:
            return _as_float(parts[0]), _as_float(parts[1])
    lat = _as_float(req.get("lat"))
    lon = _as_float(req.get("lon"))
    if lat is not None and lon is not None:
        return lat, lon
    # bbox center fallback
    bbox = req.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        vals = [_as_float(x) for x in bbox]
        if all(v is not None for v in vals):
            # assume [minLon,minLat,maxLon,maxLat] or [minLat,minLon,maxLat,maxLon]? try detect
            a, b, c, d = vals  # type: ignore
            if abs(a) <= 90 and abs(c) <= 90 and abs(b) <= 180 and abs(d) <= 180:
                return (a + c) / 2, (b + d) / 2
            return (b + d) / 2, (a + c) / 2
    return None, None


def _build_summary(run_dir: Path) -> dict[str, Any] | None:
    art = _load_artifacts(run_dir)
    fs = art.get("field_summary") or {}
    rc = art.get("report_context") or {}
    ra = art.get("rotation_analysis") or {}
    cc = art.get("crop_cycles") or {}
    es = art.get("environmental_summary") or {}
    rcfg = art.get("run_config") or {}
    man = art.get("manifest") or {}

    run_id = run_dir.name
    run = fs.get("run") if isinstance(fs.get("run"), dict) else {}
    field = fs.get("field") if isinstance(fs.get("field"), dict) else {}
    rc_field = rc.get("field") if isinstance(rc.get("field"), dict) else {}
    rc_run = rc.get("run") if isinstance(rc.get("run"), dict) else {}
    req = _first(fs.get("request"), rc.get("request"), rcfg.get("request"), rcfg)
    req = req if isinstance(req, dict) else {}

    field_id = _first(fs.get("field_id"), field.get("field_id"), rc_field.get("field_id"), run_id)
    field_name = _first(fs.get("field_name"), field.get("name"), rc_field.get("name"), field.get("field_id"), rc_field.get("field_id"), run_id)

    lat, lon = _parse_point_from_request(req)
    if lat is None:
        lat = _as_float(_get(fs, "location", "lat"))
        lon = _as_float(_get(fs, "location", "lon"))
    if lat is None:
        lat = _as_float(_get(rc, "location", "lat"))
        lon = _as_float(_get(rc, "location", "lon"))

    start = _first(_get(fs, "period", "start"), _get(rc, "request", "start_date"), req.get("start_date"), _get(rc_run, "start_date"), cc.get("requested_start"), es.get("requested_start"))
    end = _first(_get(fs, "period", "end"), _get(rc, "request", "end_date"), req.get("end_date"), _get(rc_run, "end_date"), cc.get("requested_end"), es.get("requested_end"))

    # vegetation
    veg = fs.get("vegetation") if isinstance(fs.get("vegetation"), dict) else {}
    rc_veg = rc.get("vegetation") if isinstance(rc.get("vegetation"), dict) else {}
    veg = veg or rc_veg
    n_obs = _first(_as_int(veg.get("n_observations")), _as_int(_get(rc, "vegetation", "n_observations")))
    ndvi_median = _first(_as_float(veg.get("ndvi_median")), _as_float(_get(rc, "vegetation", "ndvi_median")))

    # seasons/bare windows
    seasons = _get(rc, "cycles", "seasons", default=None)
    if seasons is None:
        seasons = _get(cc, "seasons", default=[])
    bare = _get(rc, "cycles", "bare_soil_windows", default=None)
    if bare is None:
        bare = _get(cc, "bare_soil_windows", default=[])

    # environment completeness & weather/soil quick
    env = es if isinstance(es, dict) and es else (_get(rc, "environment", default={}) or {})
    env_comp = _first(_as_float(_get(env, "data_completeness")), _as_float(_get(fs, "environment", "data_completeness")), _as_float(_get(env, "weather", "completeness")))
    w = _get(env, "weather", default={}) if isinstance(env, dict) else {}
    s = _get(env, "soil", default={}) if isinstance(env, dict) else {}
    weather = {
        "available": bool(w.get("available")) if isinstance(w, dict) and "available" in w else (bool(w) if isinstance(w, dict) else False),
        "mean_temp_c": _as_float(w.get("mean_temp_c")) if isinstance(w, dict) else None,
        "annual_precip_mm": _as_float(w.get("annual_precip_mm")) if isinstance(w, dict) else None,
        "aridity_class": w.get("aridity_class") if isinstance(w, dict) else None,
    }
    soil = {"texture_class": s.get("texture_class") if isinstance(s, dict) else None}

    # top rotation
    top_rotation = None
    ranking = _get(ra, "ranking", default=None)
    rotations = _get(ra, "rotations", default=None)
    if ranking is None:
        ranking = _get(fs, "rotation", "ranking", default=[]) or _get(rc, "rotation", "ranking", default=[])
    if rotations is None:
        rotations = _get(rc, "rotations", default=[]) or _get(rc, "rotation", "rotations", default=[])
    if isinstance(ranking, list) and ranking:
        r0 = ranking[0] if isinstance(ranking[0], dict) else {}
        rid = r0.get("rotation_id")
        # find detail
        match = None
        if isinstance(rotations, list):
            for rr in rotations:
                if isinstance(rr, dict) and rr.get("rotation_id") == rid:
                    match = rr
                    break
        top_rotation = {
            "rotation_id": rid,
            "name": _first(r0.get("name"), (match or {}).get("name"), rid),
            "score": _first(_as_float(r0.get("total_score")), _as_float(r0.get("score")), _as_float((match or {}).get("total_score"))),
            "confidence": _first(_as_float(r0.get("confidence")), _as_float((match or {}).get("confidence"))),
        }
    elif isinstance(rotations, list) and rotations:
        r0 = rotations[0] if isinstance(rotations[0], dict) else {}
        top_rotation = {
            "rotation_id": r0.get("rotation_id"),
            "name": r0.get("name"),
            "score": _as_float(r0.get("total_score")),
            "confidence": _as_float(r0.get("confidence")),
        }

    source = _first(req.get("source"), fs.get("source"), _get(run, "source"), "unknown")
    data_mode = _first(fs.get("data_mode"), man.get("data_mode"), rc_run.get("data_mode"), run.get("data_mode"), "mock" if str(source) == "mock" or req.get("mock") else "unknown")
    status = _first(fs.get("status"), run.get("overall_status"), man.get("overall_status"), "ok")
    generated_at = _first(fs.get("generated_at"), run.get("generated_at"), rc_run.get("generated_at"), man.get("generated_at"), _now_iso())

    return {
        "run_id": run_id,
        "field_id": str(field_id),
        "field_name": str(field_name),
        "data_mode": str(data_mode),
        "status": str(status),
        "generated_at": generated_at,
        "area_ha": _as_float(_first(fs.get("area_ha"), field.get("area_ha"), rc_field.get("area_ha"))),
        "location": {"lat": lat, "lon": lon} if lat is not None and lon is not None else None,
        "source": source,
        "source_status": _first(_get(fs, "source_status"), _get(run, "source_status")),
        "period": {"start": start, "end": end} if start or end else None,
        "n_observations": n_obs,
        "ndvi_median": ndvi_median,
        "n_seasons": len(seasons) if isinstance(seasons, list) else _as_int(_get(fs, "cycles", "n_seasons")),
        "n_bare_soil_windows": len(bare) if isinstance(bare, list) else _as_int(_get(fs, "cycles", "n_bare_soil_windows")),
        "environment_completeness": env_comp,
        "weather": weather,
        "soil": soil,
        "top_rotation": top_rotation,
    }


def _build_detail(run_dir: Path) -> dict[str, Any]:
    art = _load_artifacts(run_dir)
    fs = art.get("field_summary") or {}
    rc = art.get("report_context") or {}
    ra = art.get("rotation_analysis") or {}
    cc = art.get("crop_cycles") or {}
    es = art.get("environmental_summary") or {}
    rcfg = art.get("run_config") or {}
    man = art.get("manifest") or {}

    run_id = run_dir.name
    summary = _build_summary(run_dir) or {"run_id": run_id, "field_id": run_id, "field_name": run_id, "data_mode": "unknown", "status": "unknown"}

    req = _first(fs.get("request"), rc.get("request"), rcfg.get("request"), rcfg)
    req = req if isinstance(req, dict) else {}
    field = fs.get("field") if isinstance(fs.get("field"), dict) else {}
    rc_field = rc.get("field") if isinstance(rc.get("field"), dict) else {}
    run = fs.get("run") if isinstance(fs.get("run"), dict) else {}

    # vegetation (prefer rich from summary/report_context)
    vegetation = fs.get("vegetation") if isinstance(fs.get("vegetation"), dict) else {}
    if not vegetation:
        vegetation = rc.get("vegetation") if isinstance(rc.get("vegetation"), dict) else {}

    # cycles
    seasons = _get(rc, "cycles", "seasons", default=None)
    if seasons is None:
        seasons = cc.get("seasons", [])
    bare = _get(rc, "cycles", "bare_soil_windows", default=None)
    if bare is None:
        bare = cc.get("bare_soil_windows", [])
    cycles = {"seasons": seasons if isinstance(seasons, list) else [], "bare_soil_windows": bare if isinstance(bare, list) else []}

    # environment
    env = es if isinstance(es, dict) and es else {}
    if not env:
        env = rc.get("environment") if isinstance(rc.get("environment"), dict) else {}
    environment = {
        "weather": env.get("weather") if isinstance(env.get("weather"), dict) else {},
        "soil": env.get("soil") if isinstance(env.get("soil"), dict) else {},
        "terrain": env.get("terrain") if isinstance(env.get("terrain"), dict) else {},
        "data_completeness": _first(env.get("data_completeness"), _get(fs, "environment", "data_completeness")),
        "caveats": env.get("caveats") if isinstance(env.get("caveats"), list) else [],
    }

    # rotation
    rotations = ra.get("rotations") if isinstance(ra.get("rotations"), list) else None
    if rotations is None:
        rotations = rc.get("rotations") if isinstance(rc.get("rotations"), list) else []
    # build ranking if missing
    ranking = ra.get("ranking") if isinstance(ra.get("ranking"), list) else []
    if not ranking and isinstance(rotations, list):
        ranking = []
        for i, r in enumerate(rotations):
            if isinstance(r, dict):
                ranking.append({
                    "rank": r.get("rank", i + 1),
                    "rotation_id": r.get("rotation_id"),
                    "name": r.get("name"),
                    "total_score": r.get("total_score"),
                    "confidence": r.get("confidence"),
                })
    baseline_id = _first(ra.get("baseline_id"), _get(rc, "rotation", "baseline_id"), _get(fs, "rotation", "baseline_id"))
    rotation = {"rotations": rotations, "ranking": ranking, "baseline_id": baseline_id}

    # geometry
    geometry = _first(field.get("geometry"), rc_field.get("geometry"))

    # warnings/disclaimer/missing
    warnings = []
    for src in (fs.get("warnings"), rc.get("warnings"), man.get("warnings")):
        if isinstance(src, list):
            warnings.extend([str(x) for x in src])
    # de-dup keep order
    seen = set()
    warnings_u = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            warnings_u.append(w)

    missing = []
    for src in (fs.get("missing_information"), rc.get("missing_information"), ra.get("missing_information")):
        if isinstance(src, list):
            missing.extend([str(x) for x in src])

    disclaimer = _first(ra.get("disclaimer"), fs.get("disclaimer"), rc.get("disclaimer"), "")

    loc = summary.get("location")
    period = summary.get("period") or ({"start": cc.get("requested_start") or es.get("requested_start"), "end": cc.get("requested_end") or es.get("requested_end")} if (cc.get("requested_start") or es.get("requested_start")) else None)

    detail = {
        "run_id": run_id,
        "field_id": summary.get("field_id"),
        "field_name": summary.get("field_name"),
        "data_mode": summary.get("data_mode"),
        "status": summary.get("status"),
        "generated_at": summary.get("generated_at"),
        "area_ha": summary.get("area_ha"),
        "location": loc,
        "period": period,
        "vegetation": vegetation,
        "cycles": cycles,
        "environment": environment,
        "rotation": rotation,
        "geometry": geometry,
        "warnings": warnings_u,
        "missing_information": missing,
        "disclaimer": disclaimer,
    }
    return detail


# ---------------------------------------------------------------------------
# Analyze: try pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    location_type = str(payload.get("location_type") or "point")
    location_value = str(payload.get("location_value") or "")
    start_date = str(payload.get("start_date") or "")
    end_date = str(payload.get("end_date") or "")
    source = str(payload.get("source") or "sentinel-2-l2a")
    mock = bool(payload.get("mock", False))
    buffer_m = payload.get("buffer_m", 100)
    priorities = payload.get("priorities") if isinstance(payload.get("priorities"), dict) else None
    include_environment = bool(payload.get("include_environment", True))

    if mock:
        source = "mock"
    if not location_value or not start_date or not end_date:
        raise ValueError("location_value, start_date and end_date are required")

    # sanitize for run id hint
    def clean(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", s.strip())

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    loc_tag = clean(location_value.replace(" ", ""))
    run_id = f"{clean(location_type)}_{loc_tag}_{clean(start_date)}_{clean(end_date)}_{ts}"
    out_dir = RESULTS_DIR / run_id

    cmd = [_python_executable(), "main.py", "analyze"]
    if location_type == "point":
        cmd += ["--point", location_value]
    elif location_type == "bbox":
        cmd += ["--bbox", location_value]
    else:
        # fallback to point semantics
        cmd += ["--point", location_value]
    cmd += ["--start", start_date, "--end", end_date]
    if buffer_m is not None:
        cmd += ["--buffer-m", str(buffer_m)]
    cmd += ["--output-dir", str(out_dir)]
    if mock or source == "mock":
        cmd += ["--mock"]
    else:
        cmd += ["--source", source]
    if not include_environment:
        cmd += ["--no-environment"]
    if priorities:
        try:
            parts = []
            for k, v in priorities.items():
                try:
                    parts.append(f"{k}:{float(v)}")
                except Exception:
                    continue
            if parts:
                cmd += ["--priorities", ",".join(parts)]
        except Exception:
            pass

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode == 0 and out_dir.is_dir():
            return {
                "run_id": run_id,
                "output_dir": str(out_dir),
                "data_mode": "synthetic" if (mock or source == "mock") else "real",
                "status": "ok",
                "priorities": priorities or {},
            }
        # fall through to minimal run on failure
        err = (proc.stderr or proc.stdout or "").strip()[-800:]
    except Exception as e:
        err = str(e)

    # Minimal fallback run so frontend remains functional
    out_dir.mkdir(parents=True, exist_ok=True)
    lat, lon = (None, None)
    if "," in location_value:
        parts = [p.strip() for p in location_value.split(",")]
        if len(parts) >= 2:
            lat, lon = _as_float(parts[0]), _as_float(parts[1])
    data_mode = "synthetic" if (mock or source == "mock") else "unknown"
    field_summary = {
        "schema_version": "1.0",
        "run": {"run_id": run_id, "generated_at": _now_iso(), "data_mode": data_mode, "overall_status": "partial", "command": "api_fallback"},
        "field": {"field_id": run_id, "name": run_id, "source": location_type},
        "request": {"location_type": location_type, "location_value": location_value, "start_date": start_date, "end_date": end_date, "source": source, "mock": mock},
        "vegetation": {"status": "unavailable", "message": "Analysis pipeline could not complete in this environment; showing a placeholder run."},
        "cycles": {"seasons": [], "bare_soil_windows": []},
        "environment": {"weather": {"available": False}, "soil": {"available": False}, "terrain": {"available": False}, "data_completeness": 0},
        "rotation": {"rotations": [], "ranking": [], "baseline_id": None},
        "warnings": [f"Pipeline fallback used. {err[:300]}" if err else "Pipeline fallback used."],
        "missing_information": ["Full analysis artifacts unavailable"],
        "disclaimer": "Placeholder run created because the analysis pipeline was unavailable.",
        "location": {"lat": lat, "lon": lon} if lat is not None and lon is not None else None,
        "period": {"start": start_date, "end": end_date},
        "data_mode": data_mode,
        "status": "partial",
        "generated_at": _now_iso(),
    }
    (out_dir / "field_summary.json").write_text(json.dumps(field_summary, indent=2), encoding="utf-8")
    (out_dir / "run_config.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    manifest = {"run_id": run_id, "generated_at": _now_iso(), "data_mode": data_mode, "overall_status": "partial", "outputs": [{"path": "field_summary.json"}, {"path": "run_config.json"}]}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"run_id": run_id, "output_dir": str(out_dir), "data_mode": data_mode, "status": "partial", "priorities": priorities or {}, "warning": err[:300] if err else None}


# ---------------------------------------------------------------------------
# Hazard enrichment
# ---------------------------------------------------------------------------

def _risk_label(event_type: str, distance_km: float | None) -> str:
    et = (event_type or "").lower()
    near = distance_km is not None and distance_km <= 25
    mid = distance_km is not None and distance_km <= 75
    if "earthquake" in et:
        return "Potential earthquake impact nearby" if near else ("Earthquake reported in the wider area" if mid else "Earthquake reported in region")
    if "fire" in et or "wildfire" in et:
        return "Wildfire risk/activity nearby" if near else "Wildfire reported in the wider area"
    if "flood" in et:
        return "Flooding risk nearby" if near else "Flooding reported in the wider area"
    if "storm" in et or "cyclone" in et:
        return "Severe weather nearby" if near else "Severe weather reported in the wider area"
    return "Hazard reported nearby" if near else "Hazard reported in the wider area"


def _what_to_do(event_type: str) -> str:
    et = (event_type or "").lower()
    if "earthquake" in et:
        return "Check people and animals are safe. Stay away from damaged structures, expect aftershocks, and follow local civil protection and emergency instructions. Inspect water lines, slopes, and field access when it is safe."
    if "fire" in et or "wildfire" in et:
        return "Follow evacuation and fire authority instructions. Keep clear of smoke and fire fronts, secure livestock/equipment if safe, and avoid field operations in affected or smoky areas until authorities say it is safe."
    if "flood" in et:
        return "Avoid flooded areas and moving water. Move people, animals, and equipment to higher ground if needed, and follow local flood warnings. Check field access, erosion, and contamination before re-entering."
    if "storm" in et or "cyclone" in et:
        return "Secure loose equipment and shelter if needed. Avoid exposed work during severe winds/rain, and follow local weather warnings. Check for debris, lodging, and drainage issues after conditions improve."
    return "Stay alert, monitor official local warnings, and avoid unnecessary exposure. Check people, animals, and critical field infrastructure when conditions are safe."


# ── Fake soil sensor data ───────────────────────────────────────────────── #

def _get_fake_soil_data(lat: float, lon: float) -> dict[str, Any]:
    """Return simulated local soil sensor data for a given field location.

    This is placeholder/demo data. Replace with real IoT sensor readings
    when hardware is connected.
    """
    import random
    random.seed(int(lat * 1000 + lon * 1000))  # deterministic per location

    moisture = round(random.uniform(18.0, 45.0), 1)
    temperature = round(random.uniform(12.0, 30.0), 1)
    ph = round(random.uniform(6.0, 8.0), 1)
    nitrogen = round(random.uniform(15.0, 45.0), 1)   # ppm
    phosphorus = round(random.uniform(10.0, 40.0), 1)  # ppm
    potassium = round(random.uniform(80.0, 250.0), 1)  # ppm
    ec = round(random.uniform(0.2, 2.0), 2)            # dS/m
    organic_matter = round(random.uniform(1.0, 5.0), 1)  # %

    return {
        "location": {"lat": lat, "lon": lon},
        "sensor_id": f"soil-{int(lat*1000)}-{int(lon*1000)}",
        "status": "online",
        "last_updated": _now_iso(),
        "data": {
            "soil_moisture_percent": moisture,
            "soil_temperature_celsius": temperature,
            "ph": ph,
            "nitrogen_ppm": nitrogen,
            "phosphorus_ppm": phosphorus,
            "potassium_ppm": potassium,
            "ec_ds_per_m": ec,
            "organic_matter_percent": organic_matter,
        },
        "interpretation": {
            "moisture_status": "adequate" if 25 <= moisture <= 40 else ("low" if moisture < 25 else "high"),
            "ph_status": "optimal" if 6.0 <= ph <= 7.5 else ("acidic" if ph < 6.0 else "alkaline"),
            "nitrogen_status": "sufficient" if nitrogen > 25 else "deficient",
            "phosphorus_status": "sufficient" if phosphorus > 20 else "deficient",
            "potassium_status": "sufficient" if potassium > 150 else "deficient",
        },
        "note": "Simulated sensor data — replace with real IoT readings.",
    }


def _fetch_hazards(lat: float, lon: float, radius_km: float) -> dict[str, Any]:
    try:
        from src.hazard_monitoring import HazardMonitor  # type: ignore
    except Exception as e:
        return {"available": False, "events": [], "count": 0, "radius_km": radius_km, "message": f"Hazard monitoring unavailable: {e}"}

    try:
        monitor = HazardMonitor(alert_radius_km=float(radius_km))
        monitor.set_field(float(lat), float(lon))
        # prefer active alerts; try check_for_hazards/refresh if available
        alerts = []
        for method in ("check_for_hazards", "refresh", "update"):
            fn = getattr(monitor, method, None)
            if callable(fn):
                try:
                    fn()
                    break
                except Exception:
                    pass
        if hasattr(monitor, "get_active_alerts"):
            alerts = monitor.get_active_alerts() or []
        elif hasattr(monitor, "active_alerts"):
            alerts = getattr(monitor, "active_alerts") or []

        events = []
        for a in alerts:
            try:
                d = a.to_dict() if hasattr(a, "to_dict") else (dict(a) if isinstance(a, dict) else {})
            except Exception:
                d = {}
            et = str(d.get("event_type") or d.get("type") or "unknown")
            dist = _as_float(d.get("distance_km"))
            ev = {
                "id": d.get("id") or d.get("event_id") or "",
                "source": d.get("source") or "unknown",
                "title": d.get("title") or d.get("name") or "Hazard event",
                "event_type": et,
                "description": d.get("description"),
                "latitude": _as_float(d.get("latitude") or d.get("lat")),
                "longitude": _as_float(d.get("longitude") or d.get("lon")),
                "timestamp": d.get("timestamp") or d.get("time") or d.get("date"),
                "magnitude": _as_float(d.get("magnitude")),
                "depth_km": _as_float(d.get("depth_km") or d.get("depth")),
                "status": d.get("status"),
                "distance_km": dist,
                "source_url": d.get("source_url") or d.get("url") or d.get("link"),
            }
            ev["risk_label"] = d.get("risk_label") or _risk_label(et, dist)
            ev["what_to_do"] = d.get("what_to_do") or _what_to_do(et)
            events.append(ev)
        # sort by distance then recency-ish (unknown distance last)
        events.sort(key=lambda e: (e.get("distance_km") is None, e.get("distance_km") or 1e9))
        return {"available": True, "events": events, "count": len(events), "radius_km": float(radius_km)}
    except Exception as e:
        return {"available": False, "events": [], "count": 0, "radius_km": float(radius_km), "message": f"Could not retrieve hazards: {e}"}


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"AgriIntelligenceAPI/{API_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": {"status": int(status), "message": message}}, status)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path in ("/", "/api", "/api/"):
                self.send_json({
                    "service": "agri-intelligence-api",
                    "version": API_VERSION,
                    "endpoints": [
                        "GET /api/health",
                        "GET /api/farms",
                        "GET /api/farms/<run-id>",
                        "POST /api/analyze",
                        "GET /api/crop-catalog",
                        "GET /api/sources",
                        "GET /api/hazards/active",
                        "GET /api/hazards/history",
                        "GET /api/soil",
                        "GET /api/sensors/soil",
                        "GET /api/storage",
                        "GET /api/storage/items",
                        "GET /api/storage/items/<id>",
                        "GET /api/storage/summary",
                        "POST /api/storage/items",
                        "PUT /api/storage/items/<id>",
                        "DELETE /api/storage/items/<id>",
                        "POST /api/storage/items/<id>/adjust",
                        "POST /api/gemini/analyze",
                        "POST /api/groq/analyze",
                    ],
                })
                return

            if path == "/api/health":
                runs = _list_run_dirs()
                self.send_json({
                    "status": "ok",
                    "version": API_VERSION,
                    "n_runs": len(runs),
                    "results_dir": str(RESULTS_DIR),
                    "served_at": _now_iso(),
                })
                return

            # Root-level /health for frontend compat
            if path == "/health":
                self.send_json({"status": "ok", "version": API_VERSION})
                return

            if path == "/api/farms":
                farms = []
                for rd in _list_run_dirs():
                    try:
                        s = _build_summary(rd)
                        if s:
                            farms.append(s)
                    except Exception:
                        continue
                self.send_json({"farms": farms})
                return

            if path.startswith("/api/farms/"):
                remainder = path[len("/api/farms/"):]
                parts = [unquote(p) for p in remainder.split("/") if p]
                if not parts:
                    raise ValueError("Missing run id")
                run_id = parts[0]
                run_dir = _valid_run_dir(run_id)
                if len(parts) == 1:
                    self.send_json(_build_detail(run_dir))
                    return
                # sub-resources (best-effort)
                seg = parts[1]
                art = _load_artifacts(run_dir)
                if seg == "summary":
                    self.send_json(_build_summary(run_dir) or {"run_id": run_id})
                    return
                if seg == "cycles":
                    cc = art.get("crop_cycles") or _get(art.get("report_context") or {}, "cycles", default={})
                    self.send_json(cc if isinstance(cc, dict) else {"cycles": cc})
                    return
                if seg == "environment":
                    es = art.get("environmental_summary") or _get(art.get("report_context") or {}, "environment", default={})
                    self.send_json(es if isinstance(es, dict) else {"environment": es})
                    return
                if seg == "rotations":
                    ra = art.get("rotation_analysis") or {}
                    self.send_json({
                        "rotations": ra.get("rotations") or _get(art.get("report_context") or {}, "rotations", default=[]),
                        "ranking": ra.get("ranking") or [],
                        "baseline_id": ra.get("baseline_id"),
                    })
                    return
                raise ValueError(f"Unknown farm resource: {seg}")

            if path == "/api/crop-catalog":
                data = _try_read_json(CONFIG_DIR / "crop_catalog.json")
                self.send_json(data if data is not None else {"crops": {}, "error": "crop_catalog.json not found"})
                return

            if path == "/api/sources":
                data = _try_read_json(CONFIG_DIR / "sources.json")
                self.send_json(data if data is not None else {"sources": [], "error": "sources.json not found"})
                return

            # Hazards (frontend: /api/hazards/active?lat=&lon=&radius_km=)
            if path in ("/api/hazards/active", "/api/hazard/active"):
                lat = _as_float((qs.get("lat") or [None])[0])
                lon = _as_float((qs.get("lon") or [None])[0])
                radius_km = _as_float((qs.get("radius_km") or [100])[0]) or 100.0
                # Default to Ramallah area if not provided
                if lat is None:
                    lat = 31.9
                if lon is None:
                    lon = 35.2
                self.send_json(_fetch_hazards(lat, lon, radius_km))
                return

            if path in ("/api/hazards/history", "/api/hazard/history"):
                lat = _as_float((qs.get("lat") or [None])[0])
                lon = _as_float((qs.get("lon") or [None])[0])
                radius_km = _as_float((qs.get("radius_km") or [100])[0]) or 100.0
                # Default to Ramallah area if not provided
                if lat is None:
                    lat = 31.9
                if lon is None:
                    lon = 35.2
                res = _fetch_hazards(lat, lon, radius_km)
                res["history"] = True
                self.send_json(res)
                return

            # ── Fake soil sensor data ──
            if path in ("/api/soil", "/api/sensors/soil"):
                lat = _as_float((qs.get("lat") or [None])[0])
                lon = _as_float((qs.get("lon") or [None])[0])
                if lat is None:
                    lat = 31.9
                if lon is None:
                    lon = 35.2
                self.send_json(_get_fake_soil_data(lat, lon))
                return

            # ── Storage inventory (seeds & plants) ──
            if path in ("/api/storage", "/api/storage/items", "/api/storage/summary"):
                from src.storage import get_store  # local import: optional subsystem

                store = get_store()
                if path == "/api/storage/summary":
                    self.send_json(store.summary())
                    return

                item_type = (qs.get("type") or [None])[0]
                if item_type and item_type not in ("seed", "plant"):
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "type must be 'seed' or 'plant'")
                    return
                crop = (qs.get("crop") or [None])[0]
                query = (qs.get("q") or [None])[0]
                include_empty = (qs.get("include_empty") or ["true"])[0].lower() not in ("false", "0", "no")

                items = store.list(
                    item_type=item_type, crop=crop, query=query, include_empty=include_empty
                )
                result = {"items": items, "count": len(items)}
                if path == "/api/storage":
                    result["summary"] = store.summary()
                self.send_json(result)
                return

            if path.startswith("/api/storage/items/"):
                from src.storage import get_store, ItemNotFound

                item_id = unquote(path[len("/api/storage/items/") :]).strip("/")
                if not item_id or "/" in item_id:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid storage item id")
                    return
                try:
                    self.send_json(get_store().get(item_id))
                except ItemNotFound:
                    self.send_error_json(HTTPStatus.NOT_FOUND, f"Storage item not found: {item_id}")
                return

            self.send_error_json(HTTPStatus.NOT_FOUND, f"Unknown path: {path}")

        except ValueError as e:
            msg = str(e)
            status = HTTPStatus.NOT_FOUND if "Unknown run" in msg or "Invalid run" in msg else HTTPStatus.BAD_REQUEST
            self.send_error_json(status, msg)
        except Exception as e:
            traceback.print_exc()
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
                return
            if not isinstance(payload, dict):
                payload = {}

            if path == "/api/analyze":
                # support both direct payload and wrapped forms
                body = payload
                result = _run_pipeline(body)
                self.send_json(result)
                return

            if path == "/api/priorities":
                # accept and echo (no persistent store needed for frontend compat)
                priorities = payload.get("priorities") if isinstance(payload.get("priorities"), dict) else payload
                self.send_json({"status": "ok", "priorities": priorities})
                return

            # ── Groq AI: crop suggestions & rotation analysis ──
            if path in ("/api/gemini/analyze", "/api/groq/analyze"):
                # Accept user-provided data
                requested_crops = payload.get("crop_list") or payload.get("crops")
                if requested_crops and not isinstance(requested_crops, list):
                    requested_crops = [requested_crops]
                requested_crops = [
                    str(c).strip() for c in (requested_crops or []) if str(c).strip()
                ]

                priorities = payload.get("priorities") if isinstance(payload.get("priorities"), dict) else None
                field_location = None
                lat = payload.get("lat")
                lon = payload.get("lon")
                location_obj = payload.get("location")
                if (lat is None or lon is None) and isinstance(location_obj, dict):
                    lat = location_obj.get("lat", lat)
                    lon = location_obj.get("lon", lon)
                pipeline_data = payload.get("pipeline_data") or payload.get("environmental_data") or payload.get("data") or {}
                if not isinstance(pipeline_data, dict):
                    pipeline_data = {}
                if lat is None:
                    lat = pipeline_data.get("lat")
                if lon is None:
                    lon = pipeline_data.get("lon")
                if lat is not None and lon is not None:
                    try:
                        field_location = (float(lat), float(lon))
                    except (ValueError, TypeError):
                        field_location = None

                analysis_type = payload.get("analysis_type") or payload.get("type") or "crop_suggestion"
                focus_crop = payload.get("focus_crop") or payload.get("must_include") or payload.get("target_crop")

                # ── Location-aware context (authoritative over payload guesses) ──
                location_profile = None
                if field_location:
                    try:
                        from src.location_profile import build_location_profile

                        location_profile = build_location_profile(
                            field_location[0],
                            field_location[1],
                            irrigation_mm=float(payload.get("irrigation_mm") or 0.0),
                            force_refresh=bool(payload.get("refresh_profile", False)),
                            timeout_s=float(
                                os.environ.get("AGRI_ANALYZE_PROFILE_TIMEOUT_S", "50")
                            ),
                        )
                    except Exception:
                        traceback.print_exc()
                        location_profile = None

                # Seeds/plants the farmer already owns
                storage_items: list[dict[str, Any]] = []
                try:
                    from src.storage import get_store

                    storage_items = get_store().list()
                except Exception:
                    storage_items = []
                storage_crops = []
                for item in storage_items:
                    crop_name = item.get("crop") if isinstance(item, dict) else None
                    if crop_name and crop_name not in storage_crops:
                        storage_crops.append(str(crop_name))

                # Farmer-owned crops = explicitly requested + already in storage
                farmer_crops: list[str] = []
                for name in requested_crops + storage_crops:
                    if name and name not in farmer_crops:
                        farmer_crops.append(name)
                if focus_crop and focus_crop not in farmer_crops:
                    farmer_crops.append(focus_crop)

                crop_suitability: list[dict[str, Any]] = []
                regional_crops: list[dict[str, Any]] = []
                allowed_crops = list(farmer_crops)
                location_summary = None

                if location_profile:
                    from src.location_profile import (
                        assess_crops_for_location,
                        rank_crops_for_location,
                    )

                    scored_farmer = assess_crops_for_location(farmer_crops, location_profile)
                    crop_suitability = scored_farmer
                    ranked = rank_crops_for_location(location_profile, limit=24)
                    regional_crops = [
                        entry for entry in ranked if entry.get("verdict") != "not_suitable"
                    ][:18]
                    for entry in regional_crops:
                        name = entry.get("crop")
                        if name and name not in allowed_crops:
                            allowed_crops.append(str(name))
                    for entry in scored_farmer:
                        name = entry.get("crop")
                        if name and name not in allowed_crops:
                            allowed_crops.append(str(name))

                    climate = location_profile.get("climate") or {}
                    soil = location_profile.get("soil") or {}
                    location_summary = {
                        "lat": field_location[0],
                        "lon": field_location[1],
                        "climate_zone": climate.get("climate_zone"),
                        "annual_precip_mm": climate.get("annual_precip_mm"),
                        "effective_rain_mm": climate.get("effective_rain_mm"),
                        "mean_temp_c": climate.get("mean_temp_c"),
                        "warm_season_mean_c": climate.get("warm_season_mean_c"),
                        "cold_extreme_min_c": climate.get("cold_extreme_min_c"),
                        "frost_days_per_year": climate.get("frost_days_per_year"),
                        "soil_available": bool(soil.get("available")),
                        "soil_ph": soil.get("ph"),
                        "soil_texture": soil.get("texture_class"),
                        "data_quality": (location_profile.get("data_quality") or {}).get("status"),
                    }

                try:
                    from src.groq_integration import get_crop_suggestions, get_rotation_analysis

                    common_kwargs = dict(
                        pipeline_data=pipeline_data,
                        farmer_priorities=priorities,
                        field_location=field_location,
                        crop_list=allowed_crops or None,
                        focus_crop=focus_crop,
                        location_profile=location_profile,
                        crop_suitability=crop_suitability,
                        storage_items=storage_items,
                        farmer_crops=farmer_crops,
                        regional_crops=regional_crops,
                    )
                    if analysis_type == "rotation":
                        result = get_rotation_analysis(**common_kwargs)
                    else:
                        result = get_crop_suggestions(**common_kwargs)

                    self.send_json({
                        "status": "ok",
                        "analysis_type": analysis_type,
                        "focus_crop": focus_crop,
                        "summary": result.get("summary", ""),
                        "crop_suggestions": result.get("crop_suggestions", []),
                        "recommendations": result.get("recommendations", ""),
                        "priority_adjustments": result.get("priority_adjustments", {}),
                        "model_used": result.get("model_used"),
                        "success": result.get("success", False),
                        "location": location_summary,
                        "farmer_crops": farmer_crops,
                        "storage_crops": storage_crops,
                        "crop_suitability": crop_suitability,
                        "regional_crops": regional_crops,
                        "allowed_crops": allowed_crops,
                    })
                except Exception as e:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Groq analysis failed: {str(e)}")
                return

            # ── Storage inventory (seeds & plants) ──
            if path == "/api/storage/items":
                from src.storage import get_store, StorageError

                try:
                    item = get_store().create(payload)
                except StorageError as exc:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self.send_json({"status": "created", "item": item}, HTTPStatus.CREATED)
                return

            if path.startswith("/api/storage/items/") and path.endswith("/adjust"):
                from src.storage import get_store, StorageError, ItemNotFound

                item_id = unquote(path[len("/api/storage/items/") : -len("/adjust")]).strip("/")
                delta = payload.get("delta", payload.get("change"))
                try:
                    item = get_store().adjust(item_id, delta)
                except ItemNotFound:
                    self.send_error_json(HTTPStatus.NOT_FOUND, f"Storage item not found: {item_id}")
                    return
                except StorageError as exc:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self.send_json({"status": "updated", "item": item})
                return

            # legacy hazard location set (best-effort no-op)
            if path.startswith("/api/hazard"):
                self.send_json({"status": "ok"})
                return

            self.send_error_json(HTTPStatus.NOT_FOUND, f"Unknown POST path: {path}")
        except Exception as e:
            traceback.print_exc()
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_storage_write(delete=False)

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_storage_write(delete=False)

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_storage_write(delete=True)

    def _handle_storage_write(self, *, delete: bool) -> None:
        """PUT/PATCH/DELETE for /api/storage/items/<id>."""
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            if not path.startswith("/api/storage/items/"):
                self.send_error_json(HTTPStatus.NOT_FOUND, f"Unknown path: {path}")
                return

            item_id = unquote(path[len("/api/storage/items/") :]).strip("/")
            if not item_id or "/" in item_id:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid storage item id")
                return

            from src.storage import get_store, StorageError, ItemNotFound

            store = get_store()
            if delete:
                try:
                    removed = store.delete(item_id)
                except ItemNotFound:
                    self.send_error_json(HTTPStatus.NOT_FOUND, f"Storage item not found: {item_id}")
                    return
                self.send_json({"status": "deleted", "item": removed})
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
                return
            if not isinstance(payload, dict):
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
                return

            try:
                item = store.update(item_id, payload)
            except ItemNotFound:
                self.send_error_json(HTTPStatus.NOT_FOUND, f"Storage item not found: {item_id}")
                return
            except StorageError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self.send_json({"status": "updated", "item": item})
        except Exception as e:
            traceback.print_exc()
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))


def main() -> None:
    parser = argparse.ArgumentParser(description="Agri-Intelligence API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host address")
    parser.add_argument("--port", type=int, default=8000, help="Port number")
    args = parser.parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AgriIntelligenceAPI/{API_VERSION} running on http://{args.host}:{args.port} (results: {RESULTS_DIR})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
