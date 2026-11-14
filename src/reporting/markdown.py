"""Human-readable Markdown report.

The report is generated from the same structured objects that are written to
JSON/CSV, so the narrative and the machine-readable outputs cannot drift apart.
Every number carries its source or is explicitly labelled as a model output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:  # NaN
        return "n/a"
    if digits == 0:
        return f"{number:.0f}{suffix}"
    return f"{number:.{digits}f}{suffix}"


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _clip(text: Any, limit: int = 180) -> str:
    value = "" if text is None else str(text).replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _status_badge(status: str) -> str:
    return {
        "ok": "OK",
        "partial": "PARTIAL",
        "unavailable": "UNAVAILABLE",
        "skipped": "SKIPPED",
        "error": "ERROR",
    }.get(status, status.upper())


def _banner(data_mode: str) -> str:
    if data_mode == "synthetic":
        return (
            "> **SYNTHETIC DEMO RUN — NOT A REAL FIELD ANALYSIS.**\n"
            "> All vegetation, weather, soil and terrain values below were generated "
            "by a random-number generator to exercise the pipeline. They are not "
            "satellite observations, reanalysis, or soil measurements, and must not "
            "be used to make farming decisions.\n"
        )
    return (
        "> **Research prototype output.** Vegetation values are satellite-derived; "
        "weather, soil and terrain are regional products, not field measurements. "
        "Rotation scores are transparent heuristics, not yield or profit predictions.\n"
    )


def _executive_summary(ctx: dict[str, Any]) -> str:
    field = ctx["field"]
    request = ctx["request"]
    vegetation = ctx["vegetation"]
    cycles = ctx["cycles"]
    rotations = ctx["rotations"]
    environment = ctx["environment"]
    centroid = field.get("centroid", {})

    lines = ["## 1. Executive summary", ""]
    observation_label = (
        "synthetic observations" if ctx["run"].get("data_mode") == "synthetic" else "usable satellite observations"
    )
    lines.append(
        f"- **Field:** `{field.get('field_id')}` at {_fmt(centroid.get('lat'), 5)}, "
        f"{_fmt(centroid.get('lon'), 5)}; {_fmt(field.get('area_ha'), 2)} ha "
        f"({field.get('source')}, {_fmt(field.get('buffer_m'), 0, ' m buffer') if field.get('buffer_m') else 'polygon'})."
    )
    lines.append(
        f"- **Analysis window:** {request.get('start')} to {request.get('end')} "
        f"({request.get('n_days')} days), vegetation source "
        f"`{vegetation.get('source_id')}`."
    )
    lines.append(
        f"- **Observations:** {vegetation.get('n_observations', 0)} {observation_label}; "
        f"median gap {_fmt(vegetation.get('quality', {}).get('median_gap_days'), 1, ' days')}; "
        f"largest gap {_fmt(vegetation.get('quality', {}).get('max_gap_days'), 0, ' days')}."
    )
    lines.append(
        f"- **Growing cycles:** {cycles.get('n_seasons', 0)} candidate season(s) and "
        f"{len(cycles.get('bare_soil_windows', []))} low-NDVI window(s) consistent "
        "with bare soil, stubble or fallow."
    )
    lines.append(
        f"- **Environmental context:** data completeness "
        f"{_pct(environment.get('data_completeness'))}; "
        f"weather {'available' if environment.get('weather', {}).get('available') else 'unavailable'}, "
        f"soil {'available' if environment.get('soil', {}).get('available') else 'unavailable'}, "
        f"terrain {'available' if environment.get('terrain', {}).get('available') else 'unavailable'}."
    )
    ranking = rotations.get("ranking", [])
    if ranking:
        top = ranking[0]
        tie_note = ""
        if len(ranking) >= 2 and ranking[0].get("total_score") is not None and ranking[1].get("total_score") is not None:
            gap = abs(ranking[0]["total_score"] - ranking[1]["total_score"])
            if gap < 0.02:
                tie_note = (
                    f" {ranking[1].get('name')} is within {gap:.3f}, so the top two "
                    "are effectively tied given the heuristic inputs."
                )
        lines.append(
            f"- **Highest weighted rotation score:** {top.get('name')} "
            f"(`{top.get('rotation_id')}`) at {_fmt(top.get('total_score'), 2)}/1.00 "
            f"with {_pct(top.get('confidence'))} score confidence.{tie_note} "
            "This is a comparative heuristic, **not** a recommendation or a yield forecast."
        )
    else:
        lines.append("- **Rotation comparison:** not available (no catalog or context).")
    lines.append(
        f"- **Overall run status:** `{ctx['run'].get('overall_status')}`. "
        "See section 8 for limitations and missing information."
    )
    lines.append("")
    return "\n".join(lines)


def _field_and_inputs(ctx: dict[str, Any]) -> str:
    field = ctx["field"]
    request = ctx["request"]
    rows = [
        ["Field id", f"`{field.get('field_id')}`"],
        ["Geometry source", field.get("source")],
        ["Centroid (lon, lat)", f"{_fmt(field.get('centroid', {}).get('lon'), 5)}, {_fmt(field.get('centroid', {}).get('lat'), 5)}"],
        ["Bounding box", ", ".join(_fmt(v, 5) for v in field.get("bbox", []))],
        ["Area", f"{_fmt(field.get('area_ha'), 2)} ha (planar UTM estimate)"],
        ["Buffer", f"{_fmt(field.get('buffer_m'), 0, ' m')}" if field.get("buffer_m") else "not applicable"],
        ["Start date", request.get("start")],
        ["End date", request.get("end")],
        ["Window length", f"{request.get('n_days')} days"],
        ["Vegetation source", f"`{request.get('source')}`"],
        ["Cloud-cover filter", f"≤ {_fmt(request.get('cloud_cover_max'), 0, '%')}"],
        ["Minimum valid fraction", _pct(request.get("min_valid_fraction"))],
        ["Minimum valid pixels", request.get("min_valid_pixels")],
        ["Maximum scenes", request.get("max_scenes")],
        ["Baseline rotation", f"`{request.get('baseline_id')}`"],
        ["Scoring priorities", request.get("priorities_text")],
        ["Data mode", ctx["run"].get("data_mode")],
        ["Run id", f"`{ctx['run'].get('run_id')}`"],
        ["Generated at (UTC)", ctx["run"].get("generated_at")],
    ]
    return "\n".join(
        ["## 2. Field and run inputs", "", _table(["Parameter", "Value"], rows), ""]
    )


def _provenance(ctx: dict[str, Any]) -> str:
    lines = ["## 3. Data provenance", ""]
    if ctx["run"].get("data_mode") == "synthetic":
        lines.append(
            "This is a **synthetic run**. No Earth-observation, reanalysis, soil or DEM "
            "service was queried; the rows below describe the generators used for testing."
        )
    else:
        lines.append(
            "Every source below was queried at run time (or served from the local cache). "
            "Resolution and coverage describe the native product; field-scale values are "
            "aggregates, not field measurements."
        )
    lines.append("")
    rows: list[list[Any]] = []
    for source in ctx.get("sources", []):
        meta = source.get("metadata", {}) or {}
        rows.append(
            [
                f"`{source.get('source_id')}`",
                _status_badge(source.get("status", "unknown")),
                _clip(source.get("message", "")),
                meta.get("spatial_resolution", meta.get("spatial_resolution_note", "n/a")),
                meta.get("temporal_resolution", "n/a"),
                meta.get("license", "n/a"),
                "yes" if source.get("cached") else "no",
            ]
        )
    lines.append(
        _table(
            ["Source", "Status", "Result", "Spatial resolution", "Temporal", "License", "Cached"],
            rows,
        )
    )
    lines.append("")

    vegetation = ctx["vegetation"]
    meta = vegetation.get("metadata", {}) or {}
    lines.append("### Vegetation observations")
    lines.append("")
    lines.append(
        f"- Source: `{vegetation.get('source_id')}` — {meta.get('spatial_resolution', 'n/a')}."
    )
    lines.append(
        f"- Acquisition dates used: {vegetation.get('first_observation') or 'n/a'} to "
        f"{vegetation.get('last_observation') or 'n/a'}."
    )
    lines.append(
        f"- Scenes matched / used: {meta.get('scenes_matched', 'n/a')} / "
        f"{meta.get('scenes_used', vegetation.get('n_observations', 'n/a'))}."
    )
    if meta.get("rejection_reasons"):
        lines.append(
            "- Scenes rejected: "
            + "; ".join(
                f"{reason} ({count})" for reason, count in meta["rejection_reasons"].items()
            )
            + "."
        )
    if meta.get("scl_valid_classes"):
        lines.append(
            "- Sentinel-2 SCL classes accepted as vegetated/bare ground: "
            + ", ".join(str(v) for v in meta["scl_valid_classes"])
            + " (water, cloud, shadow, snow and saturated pixels excluded)."
        )
    lines.append(
        "- Each observation is the **median NDVI of valid field pixels** for one scene; "
        "the time-series CSV keeps measured rows and interpolated rows separate."
    )
    lines.append("")
    return "\n".join(lines)


def _data_quality(ctx: dict[str, Any]) -> str:
    vegetation = ctx["vegetation"]
    quality = vegetation.get("quality", {}) or {}
    lines = ["## 4. Data quality and gaps", ""]
    synthetic = ctx["run"].get("data_mode") == "synthetic"
    rows = [
        ["Usable observations", vegetation.get("n_observations", 0)],
        ["Observed span", f"{_fmt(quality.get('span_days'), 0, ' days')}"],
        ["Median gap", f"{_fmt(quality.get('median_gap_days'), 1, ' days')}"],
        ["Maximum gap", f"{_fmt(quality.get('max_gap_days'), 0, ' days')}"],
        ["Gaps over 30 days", quality.get("gaps_over_30_days", "n/a")],
        ["Largest gap", f"{quality.get('largest_gap_start') or 'n/a'} → {quality.get('largest_gap_end') or 'n/a'}"],
        ["Valid-pixel fraction (min / median / max)", (
            f"{_pct(quality.get('valid_fraction', {}).get('min'))} / "
            f"{_pct(quality.get('valid_fraction', {}).get('median'))} / "
            f"{_pct(quality.get('valid_fraction', {}).get('max'))}"
        )],
        ["Quality flags", ", ".join(
            f"{k}: {v}" for k, v in (quality.get("quality_flag_counts") or {}).items()
        ) or "n/a"],
    ]
    lines.append(_table(["Diagnostic", "Value"], rows))
    lines.append("")
    if vegetation.get("n_observations", 0) < 20:
        lines.append(
            "> Sparse coverage: fewer than 20 usable observations. Season boundaries and "
            "the crop-cycle model are sensitive to gaps; treat the detected cycles as "
            "provisional."
        )
        lines.append("")
    lines.append(
        "Synthetic gaps simulate cloud losses; they are not real acquisition gaps."
        if synthetic
        else (
            "Cloud masking removes contaminated pixels, so observation density depends on "
            "weather and overpass timing. Interpolated daily values are **not** measurements "
            "and are flagged as `daily_smoothed` in `vegetation_timeseries.csv`."
        )
    )
    lines.append("")
    return "\n".join(lines)


def _vegetation_section(ctx: dict[str, Any]) -> str:
    cycles = ctx["cycles"]
    vegetation = ctx["vegetation"]
    lines = ["## 5. Vegetation history and growing cycles", ""]
    lines.append(
        f"NDVI over the analysis window ranges from {_fmt(vegetation.get('ndvi_min'), 3)} "
        f"to {_fmt(vegetation.get('ndvi_max'), 3)} (median "
        f"{_fmt(vegetation.get('ndvi_median'), 3)}). The plot is saved as "
        f"`{ctx['run'].get('ndvi_plot', 'ndvi_timeseries.png')}`."
    )
    lines.append("")
    seasons = cycles.get("seasons", [])
    if seasons:
        lines.append("### Candidate seasons")
        lines.append("")
        rows = []
        for season in seasons:
            rows.append(
                [
                    season.get("season_id"),
                    season.get("start") or "before window",
                    season.get("end") or "after window",
                    _fmt(season.get("length_days"), 0),
                    season.get("peak_date"),
                    _fmt(season.get("peak_ndvi"), 3),
                    _fmt(season.get("mean_ndvi"), 3),
                    _fmt(season.get("integrated_ndvi"), 2),
                ]
            )
        lines.append(
            _table(
                ["Season", "Start", "End", "Length (d)", "Peak date", "Peak NDVI", "Mean NDVI", "Integrated NDVI"],
                rows,
            )
        )
        lines.append("")
        for season in seasons:
            if season.get("boundary_note"):
                lines.append(
                    f"> **{season.get('season_id')} boundary:** {season['boundary_note']} "
                    f"Statistics window: {season.get('stats_start') or 'n/a'} → "
                    f"{season.get('stats_end') or 'n/a'}."
                )
                lines.append("")
        lines.append("### Generic crop stages at detected milestones")
        lines.append("")
        stage_rows = []
        for season in seasons:
            for stage in season.get("stages", []):
                stage_rows.append(
                    [
                        season.get("season_id"),
                        stage.get("date"),
                        stage.get("stage"),
                        stage.get("stage_description"),
                        _fmt(stage.get("value"), 3),
                        _fmt(stage.get("velocity"), 4),
                        stage.get("days_since_peak"),
                    ]
                )
        if stage_rows:
            lines.append(
                _table(
                    ["Season", "Date", "Stage", "Description", "NDVI", "Velocity", "Days since peak"],
                    stage_rows,
                )
            )
            lines.append("")
            lines.append(
                "Stages are the upstream NASA Harvest **generic A–E NDVI-relative classes**; "
                "they are not BBCH/Zadoks phenology and are not crop-specific."
            )
            lines.append("")
    else:
        lines.append(
            f"No seasons were detected. Model status: `{cycles.get('status')}` — "
            f"{cycles.get('message', 'no message')}"
        )
        lines.append("")

    windows = cycles.get("bare_soil_windows", [])
    lines.append("### Low-NDVI windows (bare-soil / fallow candidates)")
    lines.append("")
    if windows:
        lines.append(
            _table(
                ["Start", "End", "Days", "Min NDVI", "Mean NDVI", "Relationship", "Confidence"],
                [
                    [
                        w.get("start"),
                        w.get("end"),
                        w.get("duration_days"),
                        _fmt(w.get("min_ndvi"), 3),
                        _fmt(w.get("mean_ndvi"), 3),
                        w.get("relationship"),
                        w.get("confidence"),
                    ]
                    for w in windows
                ],
            )
        )
        lines.append("")
        lines.append(
            "Low NDVI is consistent with bare soil, crop stubble, fallow or sparse "
            "vegetation. These windows are **candidates**, not proof of bare soil, and "
            "should be checked against management records."
        )
    else:
        lines.append("No low-NDVI windows met the configured duration threshold.")
    lines.append("")
    method = cycles.get("method", {})
    if method:
        lines.append(f"**Cycle method:** {method.get('name', 'n/a')}.")
        lines.append("")
        for caveat in method.get("caveats", []):
            lines.append(f"- {caveat}")
        lines.append("")
    return "\n".join(lines)


def _environment_section(ctx: dict[str, Any]) -> str:
    environment = ctx["environment"]
    weather = environment.get("weather", {}) or {}
    soil = environment.get("soil", {}) or {}
    terrain = environment.get("terrain", {}) or {}
    lines = ["## 6. Environmental context", ""]
    lines.append(
        f"Composite data completeness: **{_pct(environment.get('data_completeness'))}**."
    )
    lines.append("")

    lines.append("### Weather (regional reanalysis)")
    lines.append("")
    if weather.get("available"):
        rows = [
            ["Source", weather.get("source")],
            ["Days present / expected", f"{weather.get('days_present')} / {weather.get('days_expected')}"],
            ["Completeness", _pct(weather.get("completeness"))],
            ["Annual precipitation", f"{_fmt(weather.get('annual_precip_mm'), 0, ' mm')}"],
            ["Growing-season precipitation", f"{_fmt(weather.get('growing_season_precip_mm'), 0, ' mm')}"],
            ["Growing-season windows", "; ".join(
                f"{w.get('start')} → {w.get('end')}" for w in weather.get("growing_season_windows", [])
            ) or "n/a"],
            ["Mean / min / max temperature", (
                f"{_fmt(weather.get('mean_temp_c'), 1, ' °C')} / "
                f"{_fmt(weather.get('mean_tmin_c'), 1, ' °C')} / "
                f"{_fmt(weather.get('mean_tmax_c'), 1, ' °C')}"
            )],
            ["Growing degree days (base 5 °C)", f"{_fmt(weather.get('gdd_base5_c_days_per_year'), 0, ' °C·d/yr')}"],
            ["ET0 (Hargreaves)", f"{_fmt(weather.get('et0_hargreaves_mm_per_year'), 0, ' mm/yr')}"],
            ["Aridity index / class", f"{_fmt(weather.get('aridity_index'), 3)} / {weather.get('aridity_class') or 'n/a'}"],
            ["Mean solar radiation", f"{_fmt(weather.get('mean_solar_mj_m2_day'), 1, ' MJ/m²/day')}"],
            ["Mean relative humidity", f"{_fmt(weather.get('mean_rh_pct'), 0, '%')}"],
            ["Mean wind speed", f"{_fmt(weather.get('mean_wind_ms'), 1, ' m/s')}"],
            ["Representativeness", weather.get("spatial_representativeness")],
        ]
        lines.append(_table(["Variable", "Value"], rows))
        lines.append("")
        lines.append(f"_{weather.get('method_note', '')}_")
    else:
        lines.append("Weather data unavailable; water-balance scoring is disabled.")
    lines.append("")

    lines.append("### Soil (predicted, not a soil test)")
    lines.append("")
    if soil.get("available"):
        rows = [
            ["Source", soil.get("source")],
            ["pH", _fmt(soil.get("ph"), 2)],
            ["Organic carbon", f"{_fmt(soil.get('soc_g_kg'), 1, ' g/kg')}"],
            ["Total nitrogen", f"{_fmt(soil.get('nitrogen_g_kg'), 2, ' g/kg')}"],
            ["Clay / sand / silt", (
                f"{_fmt(soil.get('clay_pct'), 1, '%')} / "
                f"{_fmt(soil.get('sand_pct'), 1, '%')} / "
                f"{_fmt(soil.get('silt_pct'), 1, '%')}"
            )],
            ["Texture class (approximate)", soil.get("texture_class")],
            ["Depth", soil.get("depth")],
            ["Representativeness", soil.get("spatial_representativeness")],
        ]
        lines.append(_table(["Property", "Value"], rows))
        lines.append("")
        lines.append(f"_{soil.get('uncertainty_note', '')}_")
    else:
        lines.append("Soil data unavailable; agronomic-fit scoring is partial.")
    lines.append("")

    lines.append("### Terrain")
    lines.append("")
    if terrain.get("available"):
        lines.append(
            _table(
                ["Property", "Value"],
                [
                    ["Source", terrain.get("source")],
                    ["Elevation", f"{_fmt(terrain.get('elevation_m'), 0, ' m')}"],
                    ["Slope", f"{_fmt(terrain.get('slope_pct'), 1, '%')}"],
                    ["Representativeness", terrain.get("spatial_representativeness")],
                ],
            )
        )
    else:
        lines.append("Terrain data unavailable; erosion scoring uses a neutral slope.")
    lines.append("")
    if environment.get("missing"):
        lines.append("### Missing or degraded inputs")
        lines.append("")
        for item in environment["missing"]:
            lines.append(f"- {item}")
        lines.append("")
    if environment.get("caveats"):
        lines.append("### Environmental caveats")
        lines.append("")
        for item in environment["caveats"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)


def _rotation_section(ctx: dict[str, Any]) -> str:
    rotations = ctx["rotations"]
    evaluations = rotations.get("rotations", [])
    lines = ["## 7. Rotation comparison", ""]
    if not evaluations:
        lines.append("No rotation evaluation was produced.")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        "Scores are weighted averages of seven transparent criteria, each in [0, 1]. "
        "Weights are normalised across criteria that have data. **Higher is not a "
        "prediction of yield or profit.**"
    )
    lines.append("")
    rows = []
    for evaluation in evaluations:
        rows.append(
            [
                evaluation.get("rank"),
                evaluation.get("name"),
                " → ".join(evaluation.get("sequence", [])),
                _fmt(evaluation.get("total_score"), 3),
                _pct(evaluation.get("confidence")),
                evaluation.get("evidence_quality"),
            ]
        )
    lines.append(_table(["Rank", "Rotation", "Sequence", "Score", "Confidence", "Evidence"], rows))
    lines.append("")

    scores = sorted(
        (e.get("total_score") for e in evaluations if e.get("total_score") is not None),
        reverse=True,
    )
    if len(scores) >= 2 and abs(scores[0] - scores[1]) < 0.02:
        lines.append(
            f"> **Near-tie:** the top two rotations differ by only {abs(scores[0] - scores[1]):.3f} "
            "on a 0–1 scale. Given the heuristic inputs and unquantified uncertainty, treat them "
            "as effectively tied and use the criterion table plus local constraints to choose."
        )
        lines.append("")

    criteria = evaluations[0].get("criteria", [])
    if criteria:
        lines.append("### Criterion scores (weight in parentheses)")
        lines.append("")
        header = ["Criterion (weight)"] + [e.get("name") for e in evaluations]
        matrix = []
        for index, criterion in enumerate(criteria):
            row = [f"{criterion.get('label')} ({_fmt(criterion.get('weight'), 2)})"]
            for evaluation in evaluations:
                match = next(
                    (c for c in evaluation.get("criteria", []) if c.get("key") == criterion.get("key")),
                    None,
                )
                row.append(_fmt(match.get("score"), 3) if match else "n/a")
            matrix.append(row)
        lines.append(_table(header, matrix))
        lines.append("")

    for evaluation in evaluations:
        lines.append(f"### {evaluation.get('rank')}. {evaluation.get('name')}")
        lines.append("")
        lines.append(evaluation.get("description", ""))
        lines.append("")
        lines.append(
            f"- **Sequence:** {' → '.join(evaluation.get('sequence', []))}\n"
            f"- **Weighted score:** {_fmt(evaluation.get('total_score'), 3)} / 1.00\n"
            f"- **Score confidence:** {_pct(evaluation.get('confidence'))} "
            f"({evaluation.get('evidence_quality')} evidence)\n"
            f"- **Rationale:** {evaluation.get('rationale', 'n/a')}"
        )
        lines.append("")
        for title, key in (
            ("Benefits", "benefits"),
            ("Risks", "risks"),
            ("Trade-offs vs baseline", "tradeoffs"),
        ):
            items = evaluation.get(key, [])
            lines.append(f"**{title}**")
            lines.append("")
            if items:
                for item in items:
                    lines.append(f"- {item}")
            else:
                lines.append("- None identified by the current heuristics.")
            lines.append("")
        lines.append("**Criterion detail**")
        lines.append("")
        detail_rows = []
        for criterion in evaluation.get("criteria", []):
            detail_rows.append(
                [
                    criterion.get("label"),
                    _fmt(criterion.get("score"), 3),
                    _fmt(criterion.get("weight"), 2),
                    _pct(criterion.get("confidence")),
                    criterion.get("reason"),
                ]
            )
        lines.append(_table(["Criterion", "Score", "Weight", "Confidence", "Reason"], detail_rows))
        lines.append("")
        if evaluation.get("missing_information"):
            lines.append("**Missing information**")
            lines.append("")
            for item in evaluation["missing_information"]:
                lines.append(f"- {item}")
            lines.append("")

    lines.append("### Scoring assumptions")
    lines.append("")
    for assumption in evaluations[0].get("assumptions", []):
        lines.append(f"- {assumption}")
    lines.append("")
    return "\n".join(lines)


def _cropharvest_section(ctx: dict[str, Any]) -> str:
    payload = ctx.get("cropharvest")
    if not payload:
        return ""
    lines = ["### CropHarvest label context", ""]
    if payload.get("status") not in ("ok", "partial"):
        lines.append(
            f"CropHarvest labels were not used: {payload.get('message', 'unavailable')}."
        )
        lines.append("")
        return "\n".join(lines)
    lines.append(
        f"Within {_fmt(payload.get('radius_km'), 0, ' km')} of the field there are "
        f"{payload.get('n_labels', 0)} CropHarvest labels, of which "
        f"{payload.get('n_crop_labels', 0)} are crop labels "
        f"({_pct(payload.get('crop_fraction'))} crop fraction). Nearest label: "
        f"{_fmt(payload.get('nearest_km'), 1, ' km')}."
    )
    lines.append("")
    if payload.get("label_counts"):
        lines.append(
            "Most common labels: "
            + ", ".join(f"{k} ({v})" for k, v in list(payload["label_counts"].items())[:10])
            + "."
        )
        lines.append("")
    lines.append(
        "These labels are heterogeneous in space and time. They are **context for model "
        "evaluation**, not evidence of this field's current or historical crop."
    )
    lines.append("")
    return "\n".join(lines)


def _limitations(ctx: dict[str, Any]) -> str:
    lines = ["## 8. Limitations, assumptions and missing information", ""]
    lines.append("### What this analysis does not tell you")
    lines.append("")
    for item in [
        "It does not identify the crop species from NDVI alone.",
        "It does not measure yield, profit, soil carbon change, erosion, or emissions.",
        "It does not model pests, diseases, salinity, labour, machinery, irrigation efficiency, or water rights.",
        "It does not replace a local soil test, rain gauge, or management records.",
        "Low-NDVI windows are candidates for bare soil/fallow, not proof.",
        "Rotation scores are comparative heuristics and can change with weights and assumptions.",
    ]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("### Run assumptions")
    lines.append("")
    for item in ctx.get("assumptions", []):
        lines.append(f"- {item}")
    lines.append("")
    if ctx.get("missing_information"):
        lines.append("### Missing information that would materially change the result")
        lines.append("")
        for item in ctx["missing_information"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)


def _reproducibility(ctx: dict[str, Any]) -> str:
    run = ctx["run"]
    lines = ["## 9. Reproducibility", ""]
    lines.append("```bash")
    lines.append(run.get("command", "# command not recorded"))
    lines.append("```")
    lines.append("")
    lines.append("Software versions: " + ", ".join(
        f"{name} {version}" for name, version in ctx.get("software", {}).items()
    ) + ".")
    lines.append("")
    outputs = ctx.get("outputs", [])
    if outputs:
        lines.append("Files written by this run:")
        lines.append("")
        for output in outputs:
            name = output.get("path")
            if name == "report.md":
                lines.append(
                    f"- `report.md` — {output.get('description', '')} (this file; see `manifest.json` for its hash)."
                )
                continue
            if name == "report_context.json":
                lines.append(
                    f"- `report_context.json` — {output.get('description', '')} "
                    "(rewritten after rendering; see `manifest.json` for its hash)."
                )
                continue
            lines.append(
                f"- `{output.get('path')}` — {output.get('description', '')} "
                f"({output.get('bytes', 'n/a')} bytes, sha256 `{str(output.get('sha256', ''))[:12]}…`)"
            )
        lines.append("")
    lines.append(
        "The cache makes reruns reproducible only while upstream archives remain "
        "available; source products can be reprocessed by their providers."
    )
    lines.append("")
    return "\n".join(lines)


def _disclaimer(ctx: dict[str, Any]) -> str:
    return "\n".join(
        [
            "## 10. Disclaimer",
            "",
            ctx.get("disclaimer", ""),
            "",
            "Crop attribute ranges are indicative and compiled from public references; "
            "they are not local field trials. Rotation scores depend on the configured "
            "weights and on regional weather/soil products. Verify locally before any "
            "agronomic or financial decision.",
            "",
        ]
    )


def render_report(ctx: dict[str, Any]) -> str:
    run = ctx["run"]
    title = f"# Field vegetation history and rotation comparison — `{ctx['field'].get('field_id')}`"
    parts = [
        title,
        "",
        _banner(run.get("data_mode", "real")),
        "",
        _executive_summary(ctx),
        _field_and_inputs(ctx),
        _provenance(ctx),
        _data_quality(ctx),
        _vegetation_section(ctx),
        _environment_section(ctx),
        _rotation_section(ctx),
    ]
    cropharvest = _cropharvest_section(ctx)
    if cropharvest:
        parts.append(cropharvest)
    parts.extend([_limitations(ctx), _reproducibility(ctx), _disclaimer(ctx)])
    return "\n".join(parts).rstrip() + "\n"


def write_report(path: Path, ctx: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(ctx), encoding="utf-8")
    return path
