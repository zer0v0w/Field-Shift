"""Unit and end-to-end tests for the research prototype.

Run from the project root:

    .venv/bin/python -m unittest discover -s tests -v
"""

import json
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from config.settings import CROP_CATALOG_PATH, ROTATIONS_PATH
from src.crop_detection.cycles import detect_cycles
from src.crop_knowledge.catalog import CropCatalog, load_rotations
from src.environmental.context import build_context
from src.ingestion.base import SourceResult, SourceStatus
from src.ingestion.mock import MockAdapter
from src.pipeline import AnalysisPipeline, AnalysisRequest
from src.preprocessing.geometry import build_bbox_field, build_geojson_field, build_point_field
from src.reporting.json_writer import json_safe, write_json
from src.rotation_engine.evaluator import (
    evaluate_rotations,
    evaluation_summary,
    parse_priorities,
)
from src.vegetation.timeseries import merge_daily, observations_to_frame


class GeometryTests(unittest.TestCase):
    def test_point_buffer_area_is_about_pi_r_squared(self):
        field = build_point_field(32.24, 35.30, buffer_m=100)
        self.assertAlmostEqual(field.area_ha, math.pi * 100**2 / 10_000, delta=0.02)
        self.assertEqual(field.geometry.geom_type, "Polygon")

    def test_bbox_and_geojson(self):
        bbox = build_bbox_field(35.29, 32.23, 35.31, 32.25)
        self.assertGreater(bbox.area_ha, 0)
        polygon = {
            "type": "Polygon",
            "coordinates": [[
                [35.29, 32.23],
                [35.31, 32.23],
                [35.31, 32.25],
                [35.29, 32.25],
                [35.29, 32.23],
            ]],
        }
        geojson = build_geojson_field(polygon)
        self.assertAlmostEqual(geojson.area_ha, bbox.area_ha, places=3)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            build_point_field(91, 35)
        with self.assertRaises(ValueError):
            build_bbox_field(35.3, 32.2, 35.2, 32.1)


class JsonSafetyTests(unittest.TestCase):
    def test_nan_and_numpy_are_converted(self):
        payload = {
            "nan": float("nan"),
            "inf": float("inf"),
            "np_int": np.int64(3),
            "np_float": np.float32(1.5),
            "timestamp": pd.Timestamp("2023-01-01"),
            "nested": [float("nan"), {"x": np.float64(2.5)}],
        }
        safe = json_safe(payload)
        self.assertIsNone(safe["nan"])
        self.assertIsNone(safe["inf"])
        self.assertEqual(safe["np_int"], 3)
        self.assertAlmostEqual(safe["np_float"], 1.5)
        self.assertEqual(safe["timestamp"], "2023-01-01")
        self.assertIsNone(safe["nested"][0])
        self.assertEqual(safe["nested"][1]["x"], 2.5)

    def test_write_json_never_emits_nan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "x.json", {"a": float("nan")})
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("NaN", text)
            self.assertIn("null", text)


class CatalogTests(unittest.TestCase):
    def test_catalog_and_rotations_load(self):
        catalog = CropCatalog(CROP_CATALOG_PATH)
        self.assertGreaterEqual(len(catalog.crops), 9)
        rotations, baseline = load_rotations(ROTATIONS_PATH, catalog)
        self.assertEqual(len(rotations), 3)
        self.assertEqual(baseline, "existing")
        self.assertIn("durum_wheat", catalog.crops)
        self.assertTrue(catalog.get("chickpea").is_legume)

    def test_priority_parsing_and_errors(self):
        config = json.loads((PROJECT_ROOT / "config" / "scoring.json").read_text(encoding="utf-8"))
        parsed = parse_priorities("water=2,soil=1,resilience=1", config)
        self.assertAlmostEqual(sum(parsed.values()), 1.0)
        self.assertGreater(parsed["water_balance"], parsed["soil_health"])
        with self.assertRaises(ValueError):
            parse_priorities("not_a_criterion=1", config)


def _mock_context():
    adapter = MockAdapter(seed=7)
    field = build_point_field(32.24, 35.30)
    weather = adapter.weather(field, "2022-01-01", "2023-12-31")
    soil = adapter.soil(field, "2022-01-01", "2023-12-31")
    terrain = adapter.terrain(field, "2022-01-01", "2023-12-31")
    context, _ = build_context(
        weather, soil, terrain, lat=32.24, start="2022-01-01", end="2023-12-31"
    )
    return context


class RotationTests(unittest.TestCase):
    def test_scores_are_bounded_and_ranked(self):
        catalog = CropCatalog(CROP_CATALOG_PATH)
        rotations, baseline = load_rotations(ROTATIONS_PATH, catalog)
        config = json.loads((PROJECT_ROOT / "config" / "scoring.json").read_text(encoding="utf-8"))
        priorities = parse_priorities(None, config)
        evaluations = evaluate_rotations(
            rotations, catalog, _mock_context(), priorities, config, baseline_id=baseline
        )
        self.assertEqual([e.rank for e in evaluations], [1, 2, 3])
        for evaluation in evaluations:
            self.assertGreaterEqual(evaluation.total_score, 0.0)
            self.assertLessEqual(evaluation.total_score, 1.0)
            self.assertTrue(any(c.score is not None for c in evaluation.criteria))
        summary = evaluation_summary(evaluations, baseline_id=baseline)
        self.assertEqual(summary["baseline_id"], "existing")
        self.assertEqual(len(summary["ranking"]), 3)

    def test_missing_information_uses_own_rotation_crops(self):
        catalog = CropCatalog(CROP_CATALOG_PATH)
        rotations, baseline = load_rotations(ROTATIONS_PATH, catalog)
        config = json.loads((PROJECT_ROOT / "config" / "scoring.json").read_text(encoding="utf-8"))
        evaluations = evaluate_rotations(
            rotations,
            catalog,
            _mock_context(),
            parse_priorities(None, config),
            config,
            baseline_id=baseline,
        )
        alternative_a = next(e for e in evaluations if e.rotation.rotation_id == "alternative_a")
        text = " ".join(alternative_a.missing_information)
        self.assertNotIn("Faba bean", text)


class CycleTests(unittest.TestCase):
    def test_mock_series_produces_cycles_and_daily_values(self):
        adapter = MockAdapter(seed=3)
        field = build_point_field(32.24, 35.30)
        result = adapter.fetch(field, "2022-01-01", "2024-12-31")
        observations = observations_to_frame(result)
        cycles, daily = detect_cycles(
            observations,
            field_id=field.field_id,
            params={
                "ndvi_threshold_for_approx_season_start": 0.35,
                "ndvi_threshold_for_approx_season_end": 0.40,
                "min_season_length": 60,
                "ndvi_peak_percentile": 90.0,
                "ndvi_peak_threshold_ceiling": 0.70,
                "season_start_pct_of_peak": 0.35,
                "season_end_pct_of_peak": 0.35,
                "season_window_days": 90,
                "min_ndvi_for_peak": None,
            },
        )
        self.assertEqual(cycles["status"], "ok")
        self.assertGreaterEqual(cycles["n_seasons"], 2)
        self.assertFalse(daily.empty)
        self.assertIn("bare_soil_windows", cycles)
        frame = merge_daily(observations, daily, source_id="mock")
        self.assertEqual(
            set(frame["record_type"].unique()), {"observation", "daily_smoothed"}
        )
        daily_rows = frame[frame["record_type"] == "daily_smoothed"]
        self.assertTrue(daily_rows["is_observed"].eq(False).all())
        self.assertTrue(daily_rows["ndvi"].isna().all())


class PipelineTests(unittest.TestCase):
    def test_mock_pipeline_writes_all_required_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = AnalysisRequest(
                field=build_point_field(32.24, 35.30, buffer_m=100),
                start="2022-01-01",
                end="2023-12-31",
                source="mock",
                output_dir=Path(tmp),
                command="unit-test",
            )
            result = AnalysisPipeline(request).run()
            required = [
                "field_summary.json",
                "vegetation_timeseries.csv",
                "crop_cycles.json",
                "environmental_summary.json",
                "rotation_comparison.csv",
                "report.md",
                "ndvi_timeseries.png",
                "rotation_comparison.png",
                "manifest.json",
            ]
            for name in required:
                path = Path(tmp) / name
                self.assertTrue(path.exists(), f"missing {name}")
                self.assertGreater(path.stat().st_size, 0, f"empty {name}")

            summary = json.loads((Path(tmp) / "field_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["run"]["data_mode"], "synthetic")
            self.assertIn(summary["run"]["overall_status"], ("ok", "partial"))
            self.assertEqual(summary["rotation"]["baseline_id"], "existing")

            report = (Path(tmp) / "report.md").read_text(encoding="utf-8")
            self.assertIn("SYNTHETIC DEMO RUN", report)
            self.assertIn("comparative heuristic", report)
            self.assertIn("Near-tie", report)
            self.assertNotIn("NaN", report)

            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            listed = {entry["path"] for entry in manifest["outputs"]}
            self.assertIn("report.md", listed)
            self.assertIn("field_summary.json", listed)
            self.assertNotIn("manifest.json", listed)

            frame = pd.read_csv(Path(tmp) / "vegetation_timeseries.csv")
            self.assertIn("record_type", frame.columns)
            self.assertTrue((frame["record_type"] == "observation").any())

    def test_report_inventory_hashes_stay_current_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = AnalysisRequest(
                field=build_point_field(32.24, 35.30, buffer_m=100),
                start="2022-01-01",
                end="2023-12-31",
                source="mock",
                output_dir=Path(tmp),
                command="unit-test",
            )
            AnalysisPipeline(request).run()
            AnalysisPipeline(request).run()

            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            hashes = {entry["path"]: entry["sha256"] for entry in manifest["outputs"]}
            report = (Path(tmp) / "report.md").read_text(encoding="utf-8")

            for line in report.splitlines():
                match = re.match(r"- `([^`]+)` — .*sha256 `([0-9a-f]{12})…`", line)
                if match:
                    name, short_hash = match.groups()
                    self.assertIn(name, hashes)
                    self.assertEqual(hashes[name][:12], short_hash, name)

            self.assertIn("`report_context.json`", report)
            self.assertIn("`run_config.json`", report)
            self.assertIn("`manifest.json`", report)


class ContextTests(unittest.TestCase):
    def test_missing_soil_lowers_completeness(self):
        adapter = MockAdapter(seed=1)
        field = build_point_field(32.24, 35.30)
        weather = adapter.weather(field, "2022-01-01", "2022-12-31")
        terrain = adapter.terrain(field, "2022-01-01", "2022-12-31")
        soil = SourceResult(
            source_id="soilgrids-2",
            status=SourceStatus(source_id="soilgrids-2", status="error", message="timeout"),
            metadata={"properties": {}},
        )
        context, _ = build_context(
            weather, soil, terrain, lat=32.24, start="2022-01-01", end="2022-12-31"
        )
        self.assertAlmostEqual(context.data_completeness, 0.70, places=3)
        self.assertTrue(any("soil" in item.lower() for item in context.missing))

    def test_hargreaves_et0_is_in_a_plausible_range(self):
        adapter = MockAdapter(seed=1)
        field = build_point_field(32.24, 35.30)
        weather = adapter.weather(field, "2022-01-01", "2022-12-31")
        soil = adapter.soil(field, "2022-01-01", "2022-12-31")
        terrain = adapter.terrain(field, "2022-01-01", "2022-12-31")
        context, _ = build_context(
            weather, soil, terrain, lat=32.24, start="2022-01-01", end="2022-12-31"
        )
        et0 = context.weather["et0_hargreaves_mm_per_year"]
        self.assertGreater(et0, 800)
        self.assertLess(et0, 2500)


if __name__ == "__main__":
    unittest.main()
