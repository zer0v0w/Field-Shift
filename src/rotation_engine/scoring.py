"""Criterion-level scoring for crop rotations.

Every function returns a score in [0, 1] plus a plain-language reason and a
confidence value.  Scores are heuristics derived from the catalog's indicative
ranges; they are not yield predictions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..crop_knowledge.catalog import Crop, CropCatalog, Rotation
from ..environmental.context import EnvironmentalContext


@dataclass
class CriterionScore:
    key: str
    label: str
    score: float | None
    weight: float
    confidence: float
    reason: str
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "score": round(self.score, 3) if self.score is not None else None,
            "weight": round(self.weight, 3),
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "missing": self.missing,
        }


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return float(max(low, min(high, value)))


def range_mid(value: Any) -> float | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]) + float(value[1])) / 2.0
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def confidence_value(level: str, config: dict[str, Any]) -> float:
    return float(config.get("confidence_levels", {}).get(level, 0.2))


def _mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return float(np.mean(clean)) if clean else None


def _crop_confidences(crops: list[Crop], attribute: str, config: dict[str, Any]) -> float:
    values = [confidence_value(crop.confidence(attribute), config) for crop in crops]
    return float(np.mean(values)) if values else 0.2


def _fallow_fraction(crops: list[Crop]) -> float:
    return sum(1 for crop in crops if crop.crop_type == "fallow") / max(len(crops), 1)


def score_water_balance(
    crops: list[Crop],
    context: EnvironmentalContext,
    config: dict[str, Any],
) -> CriterionScore:
    label = "Water balance"
    weather = context.weather
    if not weather.get("available") or weather.get("growing_season_precip_mm") is None:
        return CriterionScore(
            key="water_balance",
            label=label,
            score=None,
            weight=0.0,
            confidence=0.2,
            reason="No weather data; water balance cannot be scored.",
            missing=["growing-season precipitation"],
        )
    effective_rain = float(weather["growing_season_precip_mm"]) * float(
        config.get("effective_rain_fraction", 0.70)
    )
    scores: list[float] = []
    reasons: list[str] = []
    for crop in crops:
        if crop.crop_type == "fallow":
            scores.append(1.0)
            reasons.append(f"{crop.common_name}: no crop water demand")
            continue
        demand = range_mid(crop.value("water_demand_mm"))
        if demand is None:
            continue
        score = clamp(1.0 - max(0.0, demand - effective_rain) / max(demand, 1.0))
        scores.append(score)
        reasons.append(
            f"{crop.common_name}: {demand:.0f} mm demand vs {effective_rain:.0f} mm effective rain"
        )
    return CriterionScore(
        key="water_balance",
        label=label,
        score=_mean(scores),
        weight=0.0,
        confidence=_crop_confidences(crops, "water_demand_mm", config)
        * float(weather.get("completeness", 0.5)),
        reason="; ".join(reasons),
        missing=[],
    )


def score_soil_health(
    crops: list[Crop],
    context: EnvironmentalContext,
    config: dict[str, Any],
) -> CriterionScore:
    scores: list[float] = []
    reasons: list[str] = []
    legume_fraction = sum(1 for crop in crops if crop.is_legume) / max(len(crops), 1)
    for crop in crops:
        if crop.crop_type == "fallow":
            scores.append(0.10)
            reasons.append(f"{crop.common_name}: no residue or roots")
            continue
        component = _mean(
            [
                crop.score("nitrogen_fixation"),
                crop.score("residue_om_contribution"),
                crop.score("rooting_depth"),
            ]
        )
        if component is not None:
            scores.append(component)
            reasons.append(
                f"{crop.common_name}: N-fix {crop.score('nitrogen_fixation'):.2f}, "
                f"residue {crop.score('residue_om_contribution'):.2f}, "
                f"rooting {crop.score('rooting_depth'):.2f}"
            )
    base = _mean(scores)
    bonus = float(config.get("adjustments", {}).get("legume_frequency_bonus", 0.12)) * min(
        1.0, legume_fraction * 3.0
    )
    total = clamp((base or 0.0) + bonus)
    return CriterionScore(
        key="soil_health",
        label="Soil health",
        score=total,
        weight=0.0,
        confidence=_crop_confidences(crops, "residue_om_contribution", config),
        reason=(
            f"Legumes in {legume_fraction:.0%} of seasons; "
            + ("; ".join(reasons) if reasons else "no crop attributes available")
        ),
    )


def score_erosion_control(
    crops: list[Crop],
    context: EnvironmentalContext,
    config: dict[str, Any],
) -> CriterionScore:
    adjustments = config.get("adjustments", {})
    slope = context.terrain.get("slope_pct") if context.terrain.get("available") else None
    slope_value = float(slope) if slope is not None else 5.0
    threshold = float(adjustments.get("slope_threshold_pct", 8.0))
    penalty = 0.0
    if slope_value > threshold:
        penalty = min(
            float(adjustments.get("max_slope_penalty", 0.25)),
            (slope_value - threshold) * float(adjustments.get("slope_penalty_per_pct", 0.012)),
        )
    scores = [
        0.10 if crop.crop_type == "fallow" else crop.score("erosion_reduction")
        for crop in crops
    ]
    fallow_penalty = (
        float(adjustments.get("fallow_erosion_penalty", 0.15)) * _fallow_fraction(crops)
    )
    total = clamp((_mean(scores) or 0.0) - penalty - fallow_penalty)
    return CriterionScore(
        key="erosion_control",
        label="Erosion control",
        score=total,
        weight=0.0,
        confidence=_crop_confidences(crops, "erosion_reduction", config)
        * (1.0 if context.terrain.get("available") else 0.7),
        reason=(
            f"Mean residue/cover class {(_mean(scores) or 0):.2f}; "
            f"slope {slope_value:.1f}% (penalty {penalty:.2f}); "
            f"fallow penalty {fallow_penalty:.2f}"
        ),
        missing=[] if context.terrain.get("available") else ["terrain slope"],
    )


def score_forage(
    crops: list[Crop],
    context: EnvironmentalContext,
    config: dict[str, Any],
) -> CriterionScore:
    scores = [crop.score("forage_value") for crop in crops]
    return CriterionScore(
        key="forage_value",
        label="Forage / livestock",
        score=_mean(scores),
        weight=0.0,
        confidence=_crop_confidences(crops, "forage_value", config),
        reason="; ".join(
            f"{crop.common_name}: {crop.score('forage_value'):.2f}" for crop in crops
        ),
    )


def score_market_fit(
    crops: list[Crop],
    context: EnvironmentalContext,
    config: dict[str, Any],
) -> CriterionScore:
    scores = [crop.score("market_suitability") for crop in crops]
    types = {crop.crop_type for crop in crops if crop.crop_type != "fallow"}
    diversity = len(types) / max(len([c for c in crops if c.crop_type != "fallow"]), 1)
    bonus = float(config.get("adjustments", {}).get("diversity_bonus", 0.10)) * diversity
    total = clamp((_mean(scores) or 0.0) + bonus)
    return CriterionScore(
        key="market_fit",
        label="Market fit",
        score=total,
        weight=0.0,
        confidence=min(0.55, _crop_confidences(crops, "market_suitability", config)),
        reason=(
            f"Mean qualitative market suitability {(_mean(scores) or 0):.2f}; "
            f"{len(types)} crop types. Local prices, contracts and processing access are not modelled."
        ),
        missing=["local market prices", "contract availability", "transport and processing access"],
    )


def score_resilience(
    crops: list[Crop],
    context: EnvironmentalContext,
    config: dict[str, Any],
) -> CriterionScore:
    adjustments = config.get("adjustments", {})
    families = [crop.family for crop in crops if crop.crop_type != "fallow"]
    diversity = len(set(families)) / max(len(crops), 1)
    drought = _mean([crop.score("drought_tolerance") for crop in crops]) or 0.5
    monoculture_penalty = 0.0
    same_family_penalty = 0.0
    for previous, current in zip(crops[:-1], crops[1:]):
        if previous.crop_id == current.crop_id:
            monoculture_penalty += float(adjustments.get("monoculture_penalty", 0.18))
        elif (
            previous.family == current.family
            and previous.crop_type != "fallow"
            and current.crop_type != "fallow"
        ):
            same_family_penalty += float(adjustments.get("same_family_penalty", 0.10))
    bonus = float(adjustments.get("diversity_bonus", 0.10)) * diversity
    total = clamp(0.60 * drought + 0.40 * diversity + bonus - monoculture_penalty - same_family_penalty)
    return CriterionScore(
        key="resilience",
        label="Resilience",
        score=total,
        weight=0.0,
        confidence=_crop_confidences(crops, "drought_tolerance", config),
        reason=(
            f"{len(set(families))} crop families, mean drought class {drought:.2f}, "
            f"monoculture penalty {monoculture_penalty:.2f}, "
            f"same-family penalty {same_family_penalty:.2f}"
        ),
    )


def _texture_match(soil_texture: str | None, crop_textures: list[str]) -> float:
    if not soil_texture or not crop_textures:
        return 0.6
    if soil_texture in crop_textures:
        return 1.0
    if "loam" in soil_texture and any("loam" in texture for texture in crop_textures):
        return 0.8
    return 0.5


def score_agronomic_fit(
    crops: list[Crop],
    context: EnvironmentalContext,
    config: dict[str, Any],
) -> CriterionScore:
    soil = context.soil
    ph = soil.get("ph") if soil.get("available") else None
    texture = soil.get("texture_class") if soil.get("available") else None
    available_days = context.weather.get("available_season_days") if context.weather.get("available") else None
    available_days = float(available_days) if available_days else 180.0
    scores: list[float] = []
    reasons: list[str] = []
    for crop in crops:
        if crop.crop_type == "fallow":
            scores.append(0.5)
            reasons.append(f"{crop.common_name}: not applicable")
            continue
        requirements = crop.value("soil_requirements") or {}
        ph_range = requirements.get("ph_range") if isinstance(requirements, dict) else None
        ph_score = 0.6
        if ph is not None and isinstance(ph_range, list) and len(ph_range) == 2:
            low, high = float(ph_range[0]), float(ph_range[1])
            if low <= float(ph) <= high:
                ph_score = 1.0
            else:
                distance = min(abs(float(ph) - low), abs(float(ph) - high))
                ph_score = clamp(1.0 - distance / 2.0)
        texture_score = _texture_match(
            texture,
            requirements.get("textures", []) if isinstance(requirements, dict) else [],
        )
        duration = range_mid(crop.value("growing_duration_days"))
        duration_score = 1.0
        if duration is not None:
            duration_score = clamp(1.0 - max(0.0, duration - available_days) / max(available_days, 1.0))
        component = _mean([ph_score, texture_score, duration_score]) or 0.5
        scores.append(component)
        reasons.append(
            f"{crop.common_name}: pH {ph_score:.2f}, texture {texture_score:.2f}, "
            f"duration {duration_score:.2f}"
        )
    return CriterionScore(
        key="agronomic_fit",
        label="Agronomic fit",
        score=_mean(scores),
        weight=0.0,
        confidence=_crop_confidences(crops, "soil_requirements", config)
        * (1.0 if soil.get("available") else 0.6),
        reason="; ".join(reasons),
        missing=[] if soil.get("available") else ["field soil pH and texture"],
    )


CRITERION_FUNCTIONS = {
    "water_balance": score_water_balance,
    "soil_health": score_soil_health,
    "erosion_control": score_erosion_control,
    "forage_value": score_forage,
    "market_fit": score_market_fit,
    "resilience": score_resilience,
    "agronomic_fit": score_agronomic_fit,
}
