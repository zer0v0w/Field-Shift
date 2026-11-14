"""Gemini AI integration for enhanced agricultural data analysis and crop suggestions.

Provides AI-powered analysis of pipeline data (NASA POWER, Open-Meteo, SoilGrids, SRTM)
and generates crop recommendations tailored to farmer priorities and environmental conditions.

API key: AQ.Ab8RN6J2ScK4VRHXYr9l_psSfCoYTQukc5-z6dfTkyOTXO2QyQ
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import numpy as np

# Gemini SDK
from google import genai
from google.genai import types


# ── API key ────────────────────────────────────────────────────────────── #

def _load_api_key() -> str | None:
    """Load Gemini API key from environment or config."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    # Try reading from a config file as fallback
    config_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "geminikey.txt"
    )
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return f.read().strip()
    return None


API_KEY = _load_api_key()

if API_KEY:
    client = genai.Client(api_key=API_KEY)
else:
    client = None  # Will raise on use if no key


# ── Helper: build context from pipeline data ────────────────────────────── #


def _build_analysis_context(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
    analysis_type: str = "general",
) -> str:
    """Build a structured text context from pipeline data for Gemini prompts.

    Args:
        pipeline_data: Dict with environmental and agricultural data from the
            pipeline (temperature, precipitation, soil moisture, etc.).
        farmer_priorities: Dict mapping priority names to weights (e.g.
            {"water_balance": 0.3, "soil_health": 0.25}).
        field_location: (lat, lon) tuple of the farmer's field.
        analysis_type: Type of analysis ("rotation", "crop_suggestion", "trends",
            "risk_assessment").

    Returns:
        A formatted string containing all relevant data for the Gemini prompt.
    """
    lines: list[str] = []

    # Field location
    if field_location:
        lat, lon = field_location
        lines.append(f"Farmer's field location: {lat:.4f}, {lon:.4f}")

    # Priorities
    if farmer_priorities:
        lines.append("Farmer priorities (weights sum to 1.0):")
        for priority, weight in farmer_priorities.items():
            lines.append(f"  - {priority}: {weight:.2f}")

    # Pipeline data sections
    if pipeline_data:
        lines.append("Environmental data:")

        # Temperature
        if "temperature" in pipeline_data:
            temp = pipeline_data["temperature"]
            lines.append(f"  - Mean temperature: {temp.get('mean', 'N/A')}°C")
            lines.append(
                f"  - Min temperature: {temp.get('min', 'N/A')}°C"
            )
            lines.append(
                f"  - Max temperature: {temp.get('max', 'N/A')}°C"
            )

        # Precipitation
        if "precipitation" in pipeline_data:
            prec = pipeline_data["precipitation"]
            lines.append(
                f"  - Total precipitation: {prec.get('total', 'N/A')} mm"
            )
            lines.append(
                f"  - Rainy days: {prec.get('rainy_days', 'N/A')}"
            )

        # Soil moisture
        if "soil_moisture" in pipeline_data:
            sm = pipeline_data["soil_moisture"]
            lines.append(
                f"  - Current soil moisture: {sm.get('current', 'N/A')}%"
            )
            lines.append(
                f"  - Field capacity: {sm.get('field_capacity', 'N/A')}%"
            )
            lines.append(
                f"  - Wilting point: {sm.get('wilting_point', 'N/A')}%"
            )

        # Derived metrics
        if "derived" in pipeline_data:
            dr = pipeline_data["derived"]
            if "et0" in dr:
                lines.append(f"  - ET0 (reference evapotranspiration): {dr['et0']} mm/day")
            if "heat_stress" in dr:
                lines.append(
                    f"  - Heat stress days: {dr['heat_stress']}"
                )
            if "frost_risk" in dr:
                lines.append(f"  - Frost risk: {dr['frost_risk']}")
            if "extreme_precip" in dr:
                lines.append(
                    f"  - Extreme precipitation events: {dr['extreme_precip']}"
                )
            if "water_trend" in dr:
                lines.append(
                    f"  - Long-term water trend: {dr['water_trend']}"
                )

        # NDVI / vegetation
        if "ndvi" in pipeline_data:
            ndvi = pipeline_data["ndvi"]
            lines.append(
                f"  - Current NDVI: {ndvi.get('current', 'N/A')}"
            )
            lines.append(
                f"  - NDVI trend: {ndvi.get('trend', 'N/A')}"
            )

        # NASA POWER data
        if "nasa_power" in pipeline_data:
            np_data = pipeline_data["nasa_power"]
            if "temp" in np_data:
                lines.append(
                    f"  - NASA POWER mean temp: {np_data['temp']}°C"
                )
            if "precip" in np_data:
                lines.append(
                    f"  - NASA POWER precipitation: {np_data['precip']} mm"
                )
            if "soil_moisture" in np_data:
                lines.append(
                    f"  - NASA POWER soil moisture: {np_data['soil_moisture']}"
                )

    # Add analysis type directive
    lines.append("")
    lines.append(f"Analysis type: {analysis_type}")
    lines.append(
        "Provide: (1) a concise summary of the key findings, (2) crop "
        "varieties best suited to these conditions, and (3) specific "
        "recommendations aligned with the farmer priorities above. "
        "Format the crop suggestions as a prioritized list with brief "
        "justification for each."
    )

    return "\n".join(lines)


# ── Core: Gemini analysis ──────────────────────────────────────────────── #


def analyze_with_gemini(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
    analysis_type: str = "general",
) -> dict[str, Any]:
    """Run Gemini AI analysis on pipeline data for enhanced crop recommendations.

    Args:
        pipeline_data: Dict with environmental and agricultural data from the
            pipeline (temperature, precipitation, soil moisture, derived metrics,
            NASA POWER, etc.).
        farmer_priorities: Dict mapping priority names to weights (e.g.
            {"water_balance": 0.3, "soil_health": 0.25}). If None, default
            priorities are used.
        field_location: (lat, lon) tuple of the farmer's field.
        analysis_type: Type of analysis ("rotation", "crop_suggestion", "trends",
            "risk_assessment").

    Returns:
        Dict with Gemini's analysis output containing:
        - "summary": Concise summary of key findings
        - "crop_suggestions": List of dicts with "crop", "justify", and
          "priority_alignment" fields
        - "recommendations": Specific actionable recommendations
        - "priority_adjustments": Suggested priority weight adjustments
        - "model_used": The Gemini model name
        - "success": Whether the analysis succeeded

    Raises:
        RuntimeError: If no Gemini API key is configured.
    """
    if client is None:
        raise RuntimeError(
            "Gemini API key not configured. Set GEMINI_API_KEY environment "
            "variable or provide a key via config."
        )

    # Build context string
    context = _build_analysis_context(
        pipeline_data,
        farmer_priorities=farmer_priorities,
        field_location=field_location,
        analysis_type=analysis_type,
    )

    # Construct the prompt
    model_name = "gemini-3.5-flash"

    prompt = f"""You are an agricultural AI assistant with access to environmental 
data from NASA POWER, Open-Meteo, SoilGrids, and SRTM. You are helping a farmer 
make informed crop-rotation and planting decisions.

{context}

Please provide your analysis in the following JSON format:

{{
  "summary": "Concise 2-3 sentence summary of the key agricultural findings",
  "crop_suggestions": [
    {{
      "crop": "Crop name/variety",
      "justify": "Brief justification (1-2 sentences) why this crop suits the conditions",
      "priority_alignment": "Which farmer priority this best supports (e.g., water_balance, soil_health, market_fit)"
    }}
  ],
  "recommendations": "Specific actionable recommendations (2-3 bullet points)",
  "priority_adjustments": {{
    "suggested_changes": "Description of suggested priority weight changes",
    "reasoning": "Why these changes would improve outcomes"
  }}
}}

Be practical, evidence-based, and directly responsive to the farmer priorities provided.
"""

    # Call Gemini
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2048,
            ),
        )

        response_text = response.text

        # Try to parse JSON from the response
        result = _parse_gemini_response(response_text)

        # Add metadata
        result["model_used"] = model_name
        result["success"] = True

        return result

    except Exception as e:
        # Return structured error information
        return {
            "summary": f"Gemini analysis failed: {str(e)}",
            "crop_suggestions": [],
            "recommendations": [],
            "priority_adjustments": {"suggested_changes": "", "reasoning": ""},
            "model_used": model_name,
            "success": False,
        }


def _parse_gemini_response(response_text: str) -> dict[str, Any]:
    """Parse Gemini response, extracting JSON if wrapped in text.

    Gemini may return text before/after the JSON block. This function tries
    to extract the JSON portion.
    """
    result: dict[str, Any] = {
        "summary": "",
        "crop_suggestions": [],
        "recommendations": "",
        "priority_adjustments": {"suggested_changes": "", "reasoning": ""},
    }

    # Try to find JSON block
    # Look for triple-backticks
    if "```" in response_text:
        # Extract content between first pair of triple-backticks
        parts = response_text.split("```")
        if len(parts) >= 3:
            json_text = parts[1].strip()
        else:
            json_text = response_text
    else:
        json_text = response_text

    # Try to find JSON start/end
    start_idx = json_text.find("{")
    end_idx = json_text.rfind("}") + 1

    if start_idx >= 0 and end_idx > start_idx:
        json_str = json_text[start_idx:end_idx]
        try:
            parsed = json.loads(json_str)
            # Map parsed fields to result structure
            result["summary"] = parsed.get("summary", "")
            
            # Crop suggestions
            raw_suggestions = parsed.get("crop_suggestions", [])
            suggestions = []
            for suggestion in raw_suggestions:
                suggestions.append({
                    "crop": suggestion.get("crop", ""),
                    "justify": suggestion.get("justify", ""),
                    "priority_alignment": suggestion.get(
                        "priority_alignment", ""
                    ),
                })
            result["crop_suggestions"] = suggestions
            
            result["recommendations"] = parsed.get("recommendations", "")
            
            # Priority adjustments
            pa = parsed.get("priority_adjustments", {})
            result["priority_adjustments"] = {
                "suggested_changes": pa.get("suggested_changes", ""),
                "reasoning": pa.get("reasoning", ""),
            }
            return result
        except json.JSONDecodeError:
            # Fall through to text summary
            pass

    # Fallback: use first substantial paragraph as summary
    paragraphs = [p.strip() for p in response_text.split("\n\n") if p.strip()]
    if paragraphs:
        result["summary"] = paragraphs[0][:500]

    return result


# ── Convenience functions ──────────────────────────────────────────────── #

def get_crop_suggestions(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
) -> dict[str, Any]:
    """Get Gemini-powered crop suggestions for given conditions.

    This is a convenience wrapper around analyze_with_gemini optimized
    for the common use case of crop suggestions.

    Args:
        pipeline_data: Environmental data dict.
        farmer_priorities: Priority weights dict.
        field_location: (lat, lon) tuple.

    Returns:
        Dict with crop_suggestions list and summary.
    """
    result = analyze_with_gemini(
        pipeline_data=pipeline_data,
        farmer_priorities=farmer_priorities,
        field_location=field_location,
        analysis_type="crop_suggestion",
    )

    return {
        "summary": result.get("summary", ""),
        "crop_suggestions": result.get("crop_suggestions", []),
        "recommendations": result.get("recommendations", ""),
        "success": result.get("success", False),
    }


def get_rotation_analysis(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
) -> dict[str, Any]:
    """Get Gemini-powered rotation analysis and suggestions.

    Args:
        pipeline_data: Environmental data dict.
        farmer_priorities: Priority weights dict.
        field_location: (lat, lon) tuple.

    Returns:
        Dict with analysis summary and recommendations.
    """
    result = analyze_with_gemini(
        pipeline_data=pipeline_data,
        farmer_priorities=farmer_priorities,
        field_location=field_location,
        analysis_type="rotation",
    )

    return {
        "summary": result.get("summary", ""),
        "crop_suggestions": result.get("crop_suggestions", []),
        "recommendations": result.get("recommendations", ""),
        "priority_adjustments": result.get("priority_adjustments", {}),
        "success": result.get("success", False),
    }


def get_trend_analysis(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
) -> dict[str, Any]:
    """Get Gemini-powered long-term trend and water trend analysis.

    Args:
        pipeline_data: Environmental data dict (should include long-term trends).
        farmer_priorities: Priority weights dict.
        field_location: (lat, lon) tuple.

    Returns:
        Dict with trend analysis and implications.
    """
    result = analyze_with_gemini(
        pipeline_data=pipeline_data,
        farmer_priorities=farmer_priorities,
        field_location=field_location,
        analysis_type="trends",
    )

    return {
        "summary": result.get("summary", ""),
        "crop_suggestions": result.get("crop_suggestions", []),
        "recommendations": result.get("recommendations", ""),
        "priority_adjustments": result.get("priority_adjustments", {}),
        "success": result.get("success", False),
    }


# ── Default priorities ─────────────────────────────────────────────────── #

DEFAULT_FARMER_PRIORITIES: dict[str, float] = {
    "water_balance": 0.25,
    "soil_health": 0.20,
    "erosion_control": 0.15,
    "forage_value": 0.10,
    "market_fit": 0.15,
    "resilience": 0.10,
    "agronomic_fit": 0.05,
}


def get_default_priorities() -> dict[str, float]:
    """Return the default farmer priority weights."""
    return dict(DEFAULT_FARMER_PRIORITIES)