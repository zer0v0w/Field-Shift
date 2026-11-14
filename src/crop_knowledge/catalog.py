"""Versioned crop knowledge catalog with explicit provenance and confidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REQUIRED_ATTRIBUTES = [
    "water_demand_mm",
    "growing_duration_days",
    "planting_window",
    "harvest_window",
    "nitrogen_fixation",
    "rooting_depth",
    "forage_value",
    "residue_om_contribution",
    "erosion_reduction",
    "soil_requirements",
    "drought_tolerance",
    "market_suitability",
]


@dataclass
class Crop:
    crop_id: str
    common_name: str
    scientific_name: str | None
    family: str
    crop_type: str
    is_legume: bool
    attributes: dict[str, Any] = field(default_factory=dict)

    def attribute(self, name: str) -> dict[str, Any]:
        return self.attributes.get(name, {})

    def value(self, name: str) -> Any:
        return self.attributes.get(name, {}).get("value")

    def score(self, name: str, default: float = 0.0) -> float:
        raw = self.attributes.get(name, {}).get("score")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def confidence(self, name: str) -> str:
        return str(self.attributes.get(name, {}).get("confidence", "unknown"))

    def sources(self, name: str) -> list[str]:
        return list(self.attributes.get(name, {}).get("source_refs", []))


@dataclass
class Rotation:
    rotation_id: str
    name: str
    description: str
    sequence: list[str]
    rationale: str = ""
    notes: str = ""

    def crops(self) -> list[str]:
        return list(self.sequence)


class CropCatalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.schema_version = self.raw.get("schema_version", "unknown")
        self.catalog_version = self.raw.get("catalog_version", "unknown")
        self.references: dict[str, Any] = self.raw.get("references", {})
        self.confidence_scale: dict[str, str] = self.raw.get("confidence_scale", {})
        self.crops: dict[str, Crop] = {}
        for crop_id, payload in self.raw.get("crops", {}).items():
            self.crops[crop_id] = Crop(
                crop_id=crop_id,
                common_name=payload.get("common_name", crop_id),
                scientific_name=payload.get("scientific_name"),
                family=payload.get("family", "unknown"),
                crop_type=payload.get("crop_type", "unknown"),
                is_legume=bool(payload.get("is_legume", False)),
                attributes=payload.get("attributes", {}),
            )
        self.errors = self.validate()
        if self.errors:
            raise ValueError("Invalid crop catalog:\n- " + "\n- ".join(self.errors))

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.crops:
            errors.append("Catalog has no crops")
        for crop_id, crop in self.crops.items():
            for attribute in REQUIRED_ATTRIBUTES:
                if attribute not in crop.attributes:
                    errors.append(f"{crop_id}: missing attribute {attribute}")
                    continue
                payload = crop.attributes[attribute]
                confidence = payload.get("confidence", "unknown")
                if confidence not in self.confidence_scale and confidence != "unknown":
                    errors.append(f"{crop_id}.{attribute}: unknown confidence {confidence}")
                for ref in payload.get("source_refs", []):
                    if ref not in self.references:
                        errors.append(f"{crop_id}.{attribute}: unknown source ref {ref}")
            water = crop.value("water_demand_mm")
            if isinstance(water, list) and len(water) == 2 and water[0] > water[1]:
                errors.append(f"{crop_id}: water demand min > max")
            duration = crop.value("growing_duration_days")
            if isinstance(duration, list) and len(duration) == 2 and duration[0] > duration[1]:
                errors.append(f"{crop_id}: duration min > max")
            for window_name in ("planting_window", "harvest_window"):
                window = crop.value(window_name)
                if isinstance(window, dict):
                    start = window.get("start_month")
                    end = window.get("end_month")
                    if start is not None and not (1 <= int(start) <= 12):
                        errors.append(f"{crop_id}.{window_name}: start month out of range")
                    if end is not None and not (1 <= int(end) <= 12):
                        errors.append(f"{crop_id}.{window_name}: end month out of range")
        return errors

    def get(self, crop_id: str) -> Crop:
        if crop_id not in self.crops:
            raise KeyError(
                f"Unknown crop '{crop_id}'. Available: {', '.join(sorted(self.crops))}"
            )
        return self.crops[crop_id]

    def all(self) -> Iterable[Crop]:
        return self.crops.values()

    def reference(self, ref_id: str) -> dict[str, Any]:
        return self.references.get(ref_id, {})

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "n_crops": len(self.crops),
            "crops": sorted(self.crops),
            "references": sorted(self.references),
            "disclaimer": self.raw.get("disclaimer", ""),
        }


def load_rotations(path: Path, catalog: CropCatalog) -> tuple[list[Rotation], str]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rotations: list[Rotation] = []
    errors: list[str] = []
    for payload in raw.get("rotations", []):
        sequence = list(payload.get("sequence", []))
        if not sequence:
            errors.append(f"{payload.get('id')}: empty sequence")
        for crop_id in sequence:
            if crop_id not in catalog.crops:
                errors.append(f"{payload.get('id')}: unknown crop '{crop_id}'")
        rotations.append(
            Rotation(
                rotation_id=payload.get("id", f"rotation_{len(rotations) + 1}"),
                name=payload.get("name", payload.get("id", "unnamed")),
                description=payload.get("description", ""),
                sequence=sequence,
                rationale=payload.get("rationale", ""),
                notes=payload.get("notes", ""),
            )
        )
    if len(rotations) < 3:
        errors.append("At least three rotations are required for comparison")
    baseline = raw.get("baseline_id")
    if baseline and baseline not in {r.rotation_id for r in rotations}:
        errors.append(f"baseline_id '{baseline}' is not one of the rotations")
    if errors:
        raise ValueError("Invalid rotation file:\n- " + "\n- ".join(errors))
    return rotations, baseline or rotations[0].rotation_id
