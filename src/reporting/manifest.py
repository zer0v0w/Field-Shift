"""Run manifest: file inventory, hashes, and provenance for one analysis."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .json_writer import write_json


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_outputs(output_dir: Path, descriptions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    descriptions = descriptions or {}
    entries: list[dict[str, Any]] = []
    for path in sorted(Path(output_dir).iterdir()):
        if not path.is_file() or path.name == "manifest.json":
            continue
        entries.append(
            {
                "path": path.name,
                "description": descriptions.get(path.name, ""),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return entries


def build_manifest(
    output_dir: Path,
    *,
    run_id: str,
    data_mode: str,
    command: str,
    field_id: str,
    source_id: str,
    descriptions: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outputs = describe_outputs(output_dir, descriptions)
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "data_mode": data_mode,
        "field_id": field_id,
        "vegetation_source": source_id,
        "command": command,
        "outputs": outputs,
        "output_count": len(outputs),
        **(extra or {}),
    }


def write_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    return write_json(Path(output_dir) / "manifest.json", manifest)
