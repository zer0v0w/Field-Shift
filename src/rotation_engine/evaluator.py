"""Rotation evaluation, ranking, and transparent explanations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..crop_knowledge.catalog import CropCatalog, Rotation
from ..environmental.context import EnvironmentalContext
from .scoring import CRITERION_FUNCTIONS, CriterionScore


def _load_stock_crops() -> set[str]:
    """Best-effort: crop names/ids the user has in stock (seeds/plants)."""
    try:
        from ..storage import get_store  # type: ignore
    except Exception:
        return set()
    try:
        items = get_store().list(include_empty=False)
    except Exception:
        return set()
    crops: set[str] = set()
    for it in items or []:
        crop = str(it.get("crop") or "").strip().lower().replace(" ", "_")
        if crop:
            crops.add(crop)
        # also keep common-name-like token
        crop_raw = str(it.get("crop") or "").strip().lower()
        if crop_raw:
            crops.add(crop_raw)
    return crops

CRITERION_ALIASES = {
    "water": "water_balance",
    "water_balance": "water_balance",
    "soil": "soil_health",
    "soil_health": "soil_health",
    "erosion": "erosion_control",
    "erosion_control": "erosion_control",
    "forage": "forage_value",
    "forage_value": "forage_value",
    "market": "market_fit",
    "market_fit": "market_fit",
    "resilience": "resilience",
    "agronomic": "agronomic_fit",
    "agronomic_fit": "agronomic_fit",
}


@dataclass
class RotationEvaluation:
    rotation: Rotation
    total_score: float
    rank: int
    confidence: float
    evidence_quality: str
    criteria: list[CriterionScore]
    benefits: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    tradeoffs: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def criterion(self, key: str) -> CriterionScore | None:
        return next((c for c in self.criteria if c.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rotation_id": self.rotation.rotation_id,
            "name": self.rotation.name,
            "description": self.rotation.description,
            "sequence": self.rotation.sequence,
            "rationale": self.rotation.rationale,
            "notes": self.rotation.notes,
            "rank": self.rank,
            "total_score": round(self.total_score, 3),
            "confidence": round(self.confidence, 3),
            "evidence_quality": self.evidence_quality,
            "criteria": [c.to_dict() for c in self.criteria],
            "benefits": self.benefits,
            "risks": self.risks,
            "tradeoffs": self.tradeoffs,
            "missing_information": self.missing_information,
            "assumptions": self.assumptions,
        }


def parse_priorities(
    raw: str | None,
    scoring_config: dict[str, Any],
) -> dict[str, float]:
    defaults = {
        key: float(payload.get("weight", 0.0))
        for key, payload in scoring_config.get("criteria", {}).items()
    }
    if raw is None or not raw.strip():
        total = sum(defaults.values()) or 1.0
        return {key: value / total for key, value in defaults.items()}

    parsed: dict[str, float | None] = {}
    for token in [part.strip() for part in raw.split(",") if part.strip()]:
        if "=" in token:
            name, value = token.split("=", 1)
        elif ":" in token:
            name, value = token.split(":", 1)
        else:
            name, value = token, None
        key = CRITERION_ALIASES.get(name.strip().lower())
        if key is None:
            raise ValueError(
                f"Unknown priority '{name}'. Choose from: {', '.join(sorted(CRITERION_ALIASES))}"
            )
        parsed[key] = float(value) if value is not None else None

    if not parsed:
        raise ValueError("No valid priorities provided")
    if all(value is None for value in parsed.values()):
        weight = 1.0 / len(parsed)
        return {key: weight for key in parsed}
    total = sum(value for value in parsed.values() if value is not None) or 1.0
    return {
        key: (value / total if value is not None else 0.0)
        for key, value in parsed.items()
    }


def _evidence_quality(confidence: float, config: dict[str, Any]) -> str:
    thresholds = config.get("evidence_quality_thresholds", {})
    if confidence >= float(thresholds.get("high", 0.75)):
        return "high"
    if confidence >= float(thresholds.get("medium", 0.55)):
        return "medium"
    return "low"


def _fmt(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.2f}"


def _default_assumptions(config: dict[str, Any]) -> list[str]:
    return [
        f"Effective growing-season rain is {config.get('effective_rain_fraction', 0.70):.0%} of precipitation.",
        "GDD uses a 5 C base temperature; ET0 uses Hargreaves, not Penman-Monteith.",
        "Scores are relative heuristics in [0, 1]; they are not yields, profits, or emissions.",
        "No pest, disease, salinity, labour, machinery, or irrigation-efficiency constraints are modelled.",
        "Market suitability is qualitative and does not use local prices or contracts.",
    ]


def evaluate_rotations(
    rotations: list[Rotation],
    catalog: CropCatalog,
    context: EnvironmentalContext,
    priorities: dict[str, float],
    scoring_config: dict[str, Any],
    *,
    baseline_id: str,
    stock_crops: set[str] | None = None,
) -> list[RotationEvaluation]:
    evaluations: list[RotationEvaluation] = []
    for rotation in rotations:
        crops = [catalog.get(crop_id) for crop_id in rotation.sequence]
        criteria: list[CriterionScore] = []
        for key, function in CRITERION_FUNCTIONS.items():
            result = function(crops, context, scoring_config)
            result.weight = float(priorities.get(key, 0.0))
            criteria.append(result)

        active = [c for c in criteria if c.score is not None and c.weight > 0]
        weight_sum = sum(c.weight for c in active)
        total = (
            sum((c.score or 0.0) * c.weight for c in active) / weight_sum
            if weight_sum > 0
            else 0.0
        )
        confidence = (
            sum(c.confidence * c.weight for c in active) / weight_sum if weight_sum > 0 else 0.2
        )
        evaluations.append(
            RotationEvaluation(
                rotation=rotation,
                total_score=float(total),
                rank=0,
                confidence=float(confidence),
                evidence_quality=_evidence_quality(confidence, scoring_config),
                criteria=criteria,
                missing_information=[],
                assumptions=_default_assumptions(scoring_config),
            )
        )

    evaluations.sort(key=lambda e: e.total_score, reverse=True)
    for rank, evaluation in enumerate(evaluations, start=1):
        evaluation.rank = rank

    if stock_crops is None:
        stock_crops = _load_stock_crops()
    stock_crops_norm = {str(c).strip().lower().replace(" ", "_") for c in (stock_crops or set()) if str(c).strip()}

    baseline = next((e for e in evaluations if e.rotation.rotation_id == baseline_id), None)
    for evaluation in evaluations:
        active = [c for c in evaluation.criteria if c.score is not None and c.weight > 0]
        evaluation.benefits = [
            f"{c.label} ({_fmt(c.score)}): {c.reason}"
            for c in sorted(active, key=lambda c: (c.score or 0) * c.weight, reverse=True)
            if (c.score or 0) >= 0.65
        ][:3]
        evaluation.risks = [
            f"{c.label} ({_fmt(c.score)}): {c.reason}"
            for c in sorted(active, key=lambda c: c.score or 0)
            if (c.score or 0) <= 0.45
        ][:3]

        if baseline is not None and evaluation is not baseline:
            deltas: list[tuple[str, float]] = []
            for criterion in evaluation.criteria:
                base_criterion = baseline.criterion(criterion.key)
                if (
                    criterion.score is not None
                    and base_criterion is not None
                    and base_criterion.score is not None
                ):
                    delta = criterion.score - base_criterion.score
                    if abs(delta) >= 0.08:
                        deltas.append((criterion.label, delta))
            deltas.sort(key=lambda item: abs(item[1]), reverse=True)
            evaluation.tradeoffs = [
                f"{'Higher' if delta > 0 else 'Lower'} {label} ({delta:+.2f} vs baseline)"
                for label, delta in deltas[:4]
            ]
            total_delta = evaluation.total_score - baseline.total_score
            evaluation.tradeoffs.append(
                f"Overall weighted score {total_delta:+.2f} vs baseline"
            )
        else:
            evaluation.tradeoffs = ["Baseline reference; trade-offs are shown for alternatives."]

        # Stock/inventory context (user's seeds/plants) used in suggestions.
        seq_norm = [str(c).strip().lower().replace(" ", "_") for c in (evaluation.rotation.sequence or [])]
        in_stock = [c for c in seq_norm if c in stock_crops_norm] if stock_crops_norm else []
        not_in_stock = [c for c in seq_norm if c not in stock_crops_norm] if stock_crops_norm else []
        if stock_crops_norm and in_stock:
            pretty = ", ".join(sorted({s.replace('_', ' ') for s in in_stock}))
            evaluation.benefits.append(f"Uses crops you already have in stock: {pretty}.")
        if stock_crops_norm and not_in_stock:
            pretty = ", ".join(sorted({s.replace('_', ' ') for s in not_in_stock}))
            evaluation.risks.append(f"May require getting seed/planting material not currently in stock: {pretty}.")

        missing = list(context.missing)
        for criterion in evaluation.criteria:
            missing.extend(criterion.missing)
        rotation_crops = [catalog.get(crop_id) for crop_id in evaluation.rotation.sequence]
        low_confidence = [
            f"{crop.common_name}: low-confidence {attribute}"
            for crop in rotation_crops
            for attribute in (
                "water_demand_mm",
                "planting_window",
                "harvest_window",
                "market_suitability",
            )
            if crop.confidence(attribute) == "low"
        ]
        missing.extend(low_confidence[:6])
        missing.extend(
            [
                "local market prices and contract availability",
                "irrigation water availability and application efficiency",
                "field soil test (pH, organic matter, salinity)",
                "actual crop history and management records",
            ]
        )
        if stock_crops_norm:
            missing.append("on-farm stock/inventory was considered from saved storage (quantities/quality may need local check)")
        evaluation.missing_information = list(dict.fromkeys(missing))

    return evaluations


def comparison_rows(evaluations: list[RotationEvaluation]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    criteria_keys = list(CRITERION_FUNCTIONS)
    for evaluation in evaluations:
        row: dict[str, Any] = {
            "rotation_id": evaluation.rotation.rotation_id,
            "name": evaluation.rotation.name,
            "sequence": " > ".join(evaluation.rotation.sequence),
            "rank": evaluation.rank,
            "total_score": round(evaluation.total_score, 3),
            "confidence": round(evaluation.confidence, 3),
            "evidence_quality": evaluation.evidence_quality,
        }
        for key in criteria_keys:
            criterion = evaluation.criterion(key)
            row[f"{key}_score"] = (
                round(criterion.score, 3)
                if criterion is not None and criterion.score is not None
                else None
            )
            row[f"{key}_confidence"] = (
                round(criterion.confidence, 3) if criterion is not None else None
            )
        row["benefits"] = " | ".join(evaluation.benefits)
        row["risks"] = " | ".join(evaluation.risks)
        row["tradeoffs"] = " | ".join(evaluation.tradeoffs)
        row["missing_information"] = " | ".join(evaluation.missing_information)
        rows.append(row)
    return rows


def evaluation_summary(
    evaluations: list[RotationEvaluation],
    baseline_id: str | None = None,
) -> dict[str, Any]:
    if baseline_id is None:
        baseline_id = next(
            (
                e.rotation.rotation_id
                for e in evaluations
                if e.rotation.rotation_id == "existing"
            ),
            evaluations[0].rotation.rotation_id if evaluations else None,
        )
    return {
        "disclaimer": (
            "These are transparent comparative heuristics, not farming advice and not "
            "predictions of yield, profit, or environmental outcome. Crop attributes are "
            "indicative ranges; local verification is required."
        ),
        "baseline_id": baseline_id,
        "rotations": [evaluation.to_dict() for evaluation in evaluations],
        "ranking": [
            {
                "rank": e.rank,
                "rotation_id": e.rotation.rotation_id,
                "name": e.rotation.name,
                "total_score": round(e.total_score, 3),
                "confidence": round(e.confidence, 3),
            }
            for e in evaluations
        ],
    }
