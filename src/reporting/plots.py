"""Plots for the vegetation history and rotation comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def plot_ndvi(
    frame: pd.DataFrame,
    cycles: dict[str, Any],
    *,
    field_id: str,
    data_mode: str,
    path: Path,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(13, 6.5), dpi=150)

    observations = frame[frame["record_type"] == "observation"].copy()
    daily = frame[frame["record_type"] == "daily_smoothed"].copy()
    if not daily.empty:
        ax.plot(
            pd.to_datetime(daily["date"]),
            daily["ndvi_smoothed"],
            color="#2f6f4e",
            linewidth=1.4,
            label="Daily smoothed / interpolated NDVI",
            zorder=2,
        )
    if not observations.empty:
        ax.scatter(
            pd.to_datetime(observations["date"]),
            observations["ndvi"],
            s=18,
            color="#1f3b73",
            alpha=0.75,
            label="Observed NDVI (cloud-masked)",
            zorder=3,
        )

    for season in cycles.get("seasons", []):
        start, end = season.get("start") or season.get("stats_start"), season.get("end") or season.get("stats_end")
        if start and end:
            ax.axvspan(pd.Timestamp(start), pd.Timestamp(end), color="#8fbf9f", alpha=0.18, zorder=0)
        if season.get("peak_date"):
            ax.scatter(
                [pd.Timestamp(season["peak_date"])],
                [season.get("peak_ndvi")],
                marker="^",
                s=55,
                color="#b23a2e",
                zorder=4,
            )
            ax.annotate(
                f"peak {season['peak_ndvi']:.2f}",
                (pd.Timestamp(season["peak_date"]), season.get("peak_ndvi") or 0),
                textcoords="offset points",
                xytext=(4, 6),
                fontsize=8,
                color="#7a2018",
            )

    for window in cycles.get("bare_soil_windows", []):
        if window.get("duration_days", 0) >= 20:
            ax.axvspan(
                pd.Timestamp(window["start"]),
                pd.Timestamp(window["end"]),
                color="#d9a441",
                alpha=0.10,
                hatch="//",
                zorder=0,
            )

    ax.set_ylim(0, 1)
    ax.set_ylabel("NDVI")
    ax.set_xlabel("Date")
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    title = f"Vegetation history — {field_id}"
    if data_mode == "synthetic":
        title += "  [SYNTHETIC DEMO — NOT A REAL FIELD ANALYSIS]"
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, frameon=True)
    if data_mode == "synthetic":
        ax.text(
            0.5,
            0.02,
            "Synthetic values generated for pipeline testing; not satellite observations.",
            transform=ax.transAxes,
            ha="center",
            fontsize=8,
            color="#a11",
            bbox={"facecolor": "#fff3cd", "edgecolor": "#a11", "alpha": 0.9},
        )
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_rotations(evaluations: list[Any], *, path: Path, baseline_id: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    criteria = list(evaluations[0].criteria) if evaluations else []
    labels = [f"{e.rotation.name}\n(confidence {e.confidence:.2f})" for e in evaluations]
    y = np.arange(len(evaluations))

    fig, ax = plt.subplots(figsize=(11, max(3.5, 1.4 * len(evaluations) + 2)), dpi=150)
    colors = plt.cm.tab20(np.linspace(0, 1, max(len(criteria), 1)))
    left = np.zeros(len(evaluations))
    for index, criterion in enumerate(criteria):
        contributions = []
        for evaluation in evaluations:
            match = evaluation.criterion(criterion.key)
            score = match.score if match is not None else None
            weight = match.weight if match is not None else 0.0
            contributions.append((score or 0.0) * weight)
        ax.barh(
            y,
            contributions,
            left=left,
            color=colors[index],
            edgecolor="white",
            linewidth=0.6,
            label=criterion.label,
        )
        left += np.array(contributions)

    for index, evaluation in enumerate(evaluations):
        ax.text(
            evaluation.total_score + 0.01,
            index,
            f"{evaluation.total_score:.2f}",
            va="center",
            fontsize=9,
            fontweight="bold",
        )
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()  # rank 1 at the top
    ax.set_xlim(0, 1.05)
    ax.set_xlabel("Weighted score (stack segments are criterion contributions)")
    ax.set_title("Rotation comparison — transparent weighted criteria", fontsize=12, fontweight="bold")
    ax.grid(True, axis="x", alpha=0.25)
    ax.legend(loc="lower right", fontsize=7, ncol=2, frameon=True)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path
