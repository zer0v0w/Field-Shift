#!/usr/bin/env python3
"""Command-line interface for the agri-rotation research prototype.

Examples
--------
    python main.py sources
    python main.py rotations
    python main.py analyze --point 32.24,35.30 --start 2023-01-01 --end 2024-12-31
    python main.py analyze --point 32.24,35.30 --start 2023-01-01 --end 2023-12-31 \\
        --source modis-13Q1-061 --output-dir results/modis_demo
    python main.py analyze --point 32.24,35.30 --start 2023-01-01 --end 2023-12-31 \\
        --mock --output-dir results/mock_demo
    python main.py report --output-dir results/mock_demo
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    CROP_CATALOG_PATH,
    DEFAULT_BUFFER_M,
    DEFAULT_CLOUD_COVER_MAX,
    DEFAULT_MIN_VALID_FRACTION,
    DISCLAIMER,
    ROTATIONS_PATH,
    SOURCES_CATALOG_PATH,
    software_versions,
)
from src.crop_knowledge.catalog import CropCatalog, load_rotations  # noqa: E402
from src.pipeline import NDVI_ADAPTERS, AnalysisPipeline, AnalysisRequest, run_from_config  # noqa: E402
from src.preprocessing.geometry import (  # noqa: E402
    build_bbox_field,
    build_geojson_field,
    build_point_field,
)
from src.reporting.json_writer import read_json, write_json  # noqa: E402
from src.reporting.markdown import write_report  # noqa: E402
from src.rotation_engine.evaluator import parse_priorities  # noqa: E402


def _command_line() -> str:
    return "python " + " ".join(shlex.quote(argument) for argument in sys.argv)


def _parse_point(raw: str) -> tuple[float, float]:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Point must be 'lat,lon' (decimal degrees)")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid point: {raw}") from exc
    return lat, lon


def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "Bounding box must be 'min_lon,min_lat,max_lon,max_lat'"
        )
    try:
        values = tuple(float(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid bounding box: {raw}") from exc
    return values  # type: ignore[return-value]


def _build_field(args: argparse.Namespace):
    if args.point:
        lat, lon = args.point
        return build_point_field(lat, lon, buffer_m=args.buffer_m, field_id=args.field_id)
    if args.bbox:
        return build_bbox_field(*args.bbox, field_id=args.field_id)
    if args.geojson:
        return build_geojson_field(args.geojson, field_id=args.field_id)
    raise SystemExit("Provide one of --point, --bbox, or --geojson")


def cmd_analyze(args: argparse.Namespace) -> int:
    field = _build_field(args)
    scoring_config = json.loads((PROJECT_ROOT / "config" / "scoring.json").read_text(encoding="utf-8"))
    priorities = parse_priorities(args.priorities, scoring_config)

    source = "mock" if args.mock else args.source
    request = AnalysisRequest(
        field=field,
        start=args.start,
        end=args.end,
        source=source,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        cloud_cover_max=args.cloud_cover,
        min_valid_fraction=args.min_valid_fraction,
        min_valid_pixels=args.min_valid_pixels,
        max_scenes=args.max_scenes,
        priorities=priorities,
        priorities_text=args.priorities or "configured defaults",
        baseline_id=args.baseline,
        include_environment=not args.no_environment,
        include_cropharvest=args.cropharvest,
        cropharvest_radius_km=args.cropharvest_radius_km,
        cropharvest_download=args.download_cropharvest,
        seed=args.seed,
        command=_command_line(),
    )
    result = AnalysisPipeline(request).run()
    summary = result["field_summary"]
    vegetation = summary["vegetation"]
    rotation = summary["rotation"]

    print(f"Run:        {result['run_id']}")
    print(f"Data mode:  {summary['run']['data_mode'].upper()}")
    if summary["run"]["data_mode"] == "synthetic":
        print("WARNING:    synthetic values — not a real field analysis.")
    print(f"Field:      {field.field_id} ({field.area_ha:.2f} ha)")
    print(f"Period:     {request.start} → {request.end}")
    print(f"Source:     {vegetation['source_id']} — {vegetation['status']}")
    print(f"Observations: {vegetation['n_observations']} ({vegetation['first_observation']} → {vegetation['last_observation']})")
    print(f"Cycles:     {summary['cycles']['n_seasons']} season(s), {summary['cycles']['n_bare_soil_windows']} low-NDVI window(s)")
    print(f"Environment completeness: {summary['environment']['data_completeness']:.0%}")
    ranking = rotation.get("ranking", [])
    if ranking:
        print("Rotation ranking:")
        for item in ranking:
            print(
                f"  {item['rank']}. {item['name']} — score {item['total_score']:.3f}, "
                f"confidence {item['confidence']:.0%}"
            )
    print(f"Status:     {summary['run']['overall_status']}")
    print(f"Output:     {result['output_dir']}")
    print()
    print("Key files: field_summary.json, report.md, vegetation_timeseries.csv, crop_cycles.json,")
    print("           environmental_summary.json, rotation_comparison.csv, ndvi_timeseries.png")
    print()
    print("Scores are comparative heuristics, not farming advice or yield predictions.")
    return 0 if summary["run"]["overall_status"] in ("ok", "partial") else 1


def cmd_sources(args: argparse.Namespace) -> int:
    catalog = json.loads(SOURCES_CATALOG_PATH.read_text(encoding="utf-8"))
    sources = catalog.get("sources", [])
    if args.json:
        print(json.dumps(catalog, indent=2, ensure_ascii=False))
        return 0
    print(f"{'ID':26} {'STATUS':20} {'RESOLUTION':24} AUTH")
    print("-" * 90)
    for source in sources:
        print(
            f"{source.get('id', ''):26} {source.get('status', ''):20} "
            f"{source.get('spatial_resolution', '')[:24]:24} {source.get('authentication', '')}"
        )
    print()
    print("NDVI sources available to `analyze --source`:")
    for source_id, description in NDVI_ADAPTERS.items():
        print(f"  {source_id:20} {description}")
    return 0


def cmd_rotations(args: argparse.Namespace) -> int:
    catalog = CropCatalog(CROP_CATALOG_PATH)
    rotations, baseline = load_rotations(ROTATIONS_PATH, catalog)
    print(f"Baseline: {baseline}")
    print()
    for rotation in rotations:
        marker = "*" if rotation.rotation_id == baseline else " "
        print(f"{marker} {rotation.rotation_id}: {rotation.name}")
        print(f"    {' → '.join(rotation.sequence)}")
        print(f"    {rotation.description}")
        print()
    if args.crops:
        print("Crop catalog (indicative ranges, not local recommendations):")
        print()
        for crop in sorted(catalog.all(), key=lambda item: item.crop_id):
            water = crop.value("water_demand_mm")
            duration = crop.value("growing_duration_days")
            print(
                f"  {crop.crop_id:16} {crop.common_name:22} {crop.crop_type:14} "
                f"water={water} duration={duration}"
            )
    print(DISCLAIMER)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if args.from_config:
        result = run_from_config(Path(args.from_config), output_dir=Path(args.output_dir) if args.output_dir else None, command=_command_line())
        print(f"Re-ran analysis: {result['output_dir']}")
        print(f"Report: {Path(result['output_dir']) / 'report.md'}")
        return 0

    output_dir = Path(args.output_dir)
    context_path = output_dir / "report_context.json"
    if not context_path.exists():
        raise SystemExit(
            f"{context_path} not found. Run `analyze` first, or use `report --from-config PATH`."
        )
    context = read_json(context_path)
    path = write_report(output_dir / "report.md", context)

    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        from src.reporting.manifest import sha256_file

        manifest = read_json(manifest_path)
        for entry in manifest.get("outputs", []):
            if entry.get("path") == "report.md":
                entry["bytes"] = path.stat().st_size
                entry["sha256"] = sha256_file(path)
        write_json(manifest_path, manifest)

    print(f"Report rewritten: {path}")
    return 0


def cmd_cropharvest(args: argparse.Namespace) -> int:
    from config.settings import DATA_DIR
    from src.ingestion.cache import Cache
    from src.ingestion.cropharvest import CropHarvestAdapter
    from src.ingestion.http import build_session
    from src.preprocessing.geometry import build_point_field

    settings_cache = Cache(DATA_DIR / "downloads" / "cache")
    session = build_session("agri-rotation-intelligence/0.1")
    adapter = CropHarvestAdapter(DATA_DIR / "cropharvest", settings_cache, session)
    if not args.point:
        path = adapter.ensure_labels(download=args.download)
        print(f"CropHarvest labels: {path} ({path.stat().st_size / 1e6:.1f} MB)")
        print("Provide --point lat,lon to summarise labels within a radius.")
        return 0

    lat, lon = args.point
    field = build_point_field(lat, lon, buffer_m=100)
    result = adapter.nearby(field, radius_km=args.radius_km, download=args.download)
    print(f"Status: {result.status.status} — {result.status.message}")
    metadata = result.metadata
    for key in ("n_labels", "n_crop_labels", "crop_fraction", "nearest_km", "label_counts", "dataset_counts"):
        if key in metadata:
            print(f"{key}: {metadata[key]}")
    print(metadata.get("note", ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct a field's vegetation history from public Earth observations and "
            "compare crop rotations with transparent, non-predictive heuristics."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=DISCLAIMER,
    )
    parser.add_argument("--version", action="version", version="agri-rotation-intelligence 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Run a full field analysis.")
    location = analyze.add_mutually_exclusive_group(required=True)
    location.add_argument("--point", type=_parse_point, metavar="LAT,LON", help="Field centroid, decimal degrees.")
    location.add_argument(
        "--bbox",
        type=_parse_bbox,
        metavar="MINLON,MINLAT,MAXLON,MAXLAT",
        help="Bounding box in WGS84 decimal degrees.",
    )
    location.add_argument("--geojson", help="Path to a GeoJSON polygon/Feature/FeatureCollection.")
    analyze.add_argument("--buffer-m", type=float, default=DEFAULT_BUFFER_M, help="Buffer radius for --point (metres).")
    analyze.add_argument("--field-id", help="Optional field identifier used in outputs.")
    analyze.add_argument("--start", required=True, help="Start date (YYYY-MM-DD).")
    analyze.add_argument("--end", required=True, help="End date (YYYY-MM-DD).")
    analyze.add_argument(
        "--source",
        default="sentinel-2-l2a",
        choices=sorted(NDVI_ADAPTERS),
        help="Vegetation source (default: sentinel-2-l2a).",
    )
    analyze.add_argument("--mock", action="store_true", help="Shortcut for --source mock (SYNTHETIC data).")
    analyze.add_argument("--output-dir", help="Output directory (default: results/<run id>).")
    analyze.add_argument("--cloud-cover", type=float, default=DEFAULT_CLOUD_COVER_MAX, help="Maximum scene cloud cover (%%).")
    analyze.add_argument("--min-valid-fraction", type=float, default=DEFAULT_MIN_VALID_FRACTION, help="Minimum valid field-pixel fraction per scene.")
    analyze.add_argument("--min-valid-pixels", type=int, default=None, help="Minimum valid pixels per scene (default: source-specific).")
    analyze.add_argument("--max-scenes", type=int, default=None, help="Maximum scenes to sample (default: source-specific).")
    analyze.add_argument(
        "--priorities",
        help="Criterion weights, e.g. 'water=2,soil=1,resilience=1'. Unknown names are rejected.",
    )
    analyze.add_argument("--baseline", help="Rotation id used as the comparison baseline.")
    analyze.add_argument("--no-environment", action="store_true", help="Skip weather/soil/terrain (scores degrade).")
    analyze.add_argument("--cropharvest", action="store_true", help="Attach nearby CropHarvest label context.")
    analyze.add_argument("--cropharvest-radius-km", type=float, default=25.0, help="CropHarvest search radius.")
    analyze.add_argument("--download-cropharvest", action="store_true", help="Download CropHarvest labels if missing.")
    analyze.add_argument("--seed", type=int, default=42, help="Seed for the synthetic generator only.")
    analyze.set_defaults(func=cmd_analyze)

    sources = subparsers.add_parser("sources", help="List configured Earth-observation sources.")
    sources.add_argument("--json", action="store_true", help="Print the full source catalog as JSON.")
    sources.set_defaults(func=cmd_sources)

    rotations = subparsers.add_parser("rotations", help="Show configured rotations and crop catalog.")
    rotations.add_argument("--crops", action="store_true", help="Also print crop attribute ranges.")
    rotations.set_defaults(func=cmd_rotations)

    report = subparsers.add_parser("report", help="Rebuild report.md from a previous run.")
    report.add_argument("--output-dir", help="Run output directory containing report_context.json.")
    report.add_argument("--from-config", help="Re-run an analysis from a saved run_config.json.")
    report.set_defaults(func=cmd_report)

    cropharvest = subparsers.add_parser("cropharvest-labels", help="Inspect or download CropHarvest labels.")
    cropharvest.add_argument("--point", type=_parse_point, metavar="LAT,LON", help="Summarise labels near this point.")
    cropharvest.add_argument("--radius-km", type=float, default=25.0, help="Search radius.")
    cropharvest.add_argument("--download", action="store_true", help="Download labels from Zenodo if missing (about 82 MB).")
    cropharvest.set_defaults(func=cmd_cropharvest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "report" and not args.output_dir and not args.from_config:
        parser.error("report requires --output-dir or --from-config")
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
