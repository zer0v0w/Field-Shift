"""Groq AI integration for enhanced agricultural data analysis and crop suggestions.

Provides AI-powered analysis of pipeline data (NASA POWER, Open-Meteo, SoilGrids, SRTM)
and generates crop recommendations tailored to farmer priorities and environmental conditions.

API key: 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
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from groq import Groq


# ── API key ────────────────────────────────────────────────────────────── #

def _load_api_key() -> str | None:
    """Load Groq API key from environment."""
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    # Try reading from a config file as fallback
    config_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "groqkey.txt"
    )
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return f.read().strip()
    return None


API_KEY = _load_api_key()

if API_KEY:
    client = Groq(api_key=API_KEY)
else:
    client = None  # Will raise on use if no key


# ── Helper: build context from pipeline data ────────────────────────────── #


def _build_analysis_context(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
    analysis_type: str = "general",
    crop_list: Optional[list[str]] = None,
    focus_crop: Optional[str] = None,
    location_profile: Optional[dict[str, Any]] = None,
    crop_suitability: Optional[list[dict[str, Any]]] = None,
    storage_items: Optional[list[dict[str, Any]]] = None,
    farmer_crops: Optional[list[str]] = None,
    regional_crops: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Build a structured text context from pipeline data for Groq prompts.

    Args:
        pipeline_data: Dict with environmental and agricultural data from the
            pipeline (temperature, precipitation, soil moisture, etc.).
        farmer_priorities: Dict mapping priority names to weights (e.g.
            {"water_balance": 0.3, "soil_health": 0.25}).
        field_location: (lat, lon) tuple of the farmer's field.
        analysis_type: Type of analysis ("rotation", "crop_suggestion", "trends",
            "risk_assessment").
        crop_list: Optional list of crop names the farmer wants Groq to choose
            from (e.g. ["Wheat", "Maize", "Soybean", ...]). If provided, Groq
            will select only from this list rather than generating its own names.
        focus_crop: Optional crop name the farmer wants included in at least one
            rotation strategy / suggestion.
        location_profile: Site-specific climate/soil/terrain profile (see
            ``src.location_profile``).  Treated as authoritative.
        crop_suitability: Pre-computed per-crop fit for this location.
        storage_items: Seeds/plants the farmer already has on hand.

    Returns:
        A formatted string containing all relevant data for the Groq prompt.
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

    # Crops the farmer already grows / asked for
    if farmer_crops:
        lines.append(
            f"Farmer's own crops (they already grow or specifically requested these, "
            f"{len(farmer_crops)}):"
        )
        for crop in farmer_crops:
            lines.append(f"  - {crop}")
        lines.append("")

    # Regionally appropriate crops derived from live climate/soil scoring
    if regional_crops:
        lines.append(
            "Regionally appropriate crops for this exact location "
            "(pre-scored against live climate and soil data, best first):"
        )
        for entry in regional_crops:
            if not isinstance(entry, dict):
                continue
            fit = entry.get("fit_score")
            fit_text = f"{fit}/100" if fit is not None else "not scored"
            limits = ", ".join(entry.get("limits") or []) or "no hard limits"
            lines.append(
                f"  - {entry.get('crop')}: {entry.get('verdict')} "
                f"({fit_text}; {limits})"
            )
        lines.append(
            "These are the crops that actually fit this region. Use them to fill "
            "out rotations when the farmer's own crops are unsuitable or "
            "insufficient."
        )
        lines.append("")

    # Complete allow-list (union) that Groq may choose from
    if crop_list:
        lines.append(
            f"ALLOWED CROPS — you may choose ONLY from this list, never invent names "
            f"({len(crop_list)} crops):"
        )
        for crop in crop_list:
            lines.append(f"  - {crop}")
        lines.append("")

    # Farmer-requested focus crop
    if focus_crop:
        lines.append(
            f"The farmer specifically wants '{focus_crop}' included. At least one "
            f"rotation strategy / crop suggestion MUST be built around "
            f"'{focus_crop}' (use it as the first crop in that sequence)."
        )
        lines.append("")

    # What the farmer already has in storage
    if storage_items:
        lines.append("Farmer's on-hand storage (already owned):")
        for item in storage_items[:80]:
            if not isinstance(item, dict):
                continue
            crop = item.get("crop") or "unknown"
            variety = f" ({item['variety']})" if item.get("variety") else ""
            kind = item.get("type") or "item"
            qty = item.get("quantity")
            unit = item.get("unit") or ""
            lines.append(f"  - {crop}{variety} [{kind}]: {qty} {unit}".rstrip())
        lines.append(
            "Prefer crops the farmer already has seed/plants for where agronomically "
            "sensible, and say when a recommendation would require buying seed."
        )
        lines.append("")

    # Site-specific profile (authoritative for this location)
    if location_profile:
        climate = location_profile.get("climate") or {}
        soil_p = location_profile.get("soil") or {}
        terrain_p = location_profile.get("terrain") or {}
        irrigation = location_profile.get("irrigation") or {}
        lines.append("Location profile (site-specific, measured/modelled):")
        if climate.get("climate_zone"):
            lines.append(f"  - Climate zone: {climate['climate_zone']}")
        if climate.get("annual_precip_mm") is not None:
            lines.append(
                f"  - Annual precipitation: {climate['annual_precip_mm']} mm "
                f"(effective ~{climate.get('effective_rain_mm')} mm)"
            )
        if climate.get("mean_temp_c") is not None:
            lines.append(
                f"  - Temperature: mean {climate['mean_temp_c']}°C, "
                f"min {climate.get('mean_tmin_c')}°C, max {climate.get('mean_tmax_c')}°C"
            )
            if climate.get("warm_season_mean_c") is not None:
                lines.append(
                    f"  - Warm-season mean temperature: {climate['warm_season_mean_c']}°C"
                )
            if climate.get("cold_extreme_min_c") is not None:
                lines.append(
                    f"  - Winter extreme low (1st percentile): "
                    f"{climate['cold_extreme_min_c']}°C"
                )
        if climate.get("frost_days_per_year") is not None:
            lines.append(
                f"  - Frost days/year: {climate['frost_days_per_year']} "
                f"(frost-free: {climate.get('frost_free')})"
            )
        if climate.get("heat_stress_days_ge35c_per_year") is not None:
            lines.append(
                f"  - Heat-stress days (>=35°C)/year: {climate['heat_stress_days_ge35c_per_year']}"
            )
        if climate.get("aridity_class"):
            lines.append(
                f"  - Aridity: {climate['aridity_class']}"
                + (f" (index {climate['aridity_index']})" if climate.get("aridity_index") is not None else "")
            )
        if climate.get("growing_season_days") is not None:
            lines.append(f"  - Growing season: ~{climate['growing_season_days']} days")
        if soil_p.get("available"):
            bits = []
            if soil_p.get("ph") is not None:
                bits.append(f"pH {soil_p['ph']}")
            if soil_p.get("texture_class"):
                bits.append(f"texture {soil_p['texture_class']}")
            if soil_p.get("soc_g_kg") is not None:
                bits.append(f"SOC {soil_p['soc_g_kg']} g/kg")
            if soil_p.get("nitrogen_g_kg") is not None:
                bits.append(f"N {soil_p['nitrogen_g_kg']} g/kg")
            lines.append("  - Soil: " + ", ".join(bits))
        if terrain_p.get("elevation_m") is not None:
            lines.append(
                f"  - Terrain: elevation {terrain_p['elevation_m']} m"
                + (f", slope {terrain_p['slope_pct']}%" if terrain_p.get("slope_pct") is not None else "")
            )
        if irrigation.get("assumed_available"):
            lines.append(
                f"  - Irrigation assumed: +{irrigation.get('additional_mm_per_year')} mm/year"
            )
        lines.append("")

    # Pre-computed suitability for the farmer's crops
    if crop_suitability:
        lines.append("Location suitability for the farmer's crops (pre-computed):")
        for entry in crop_suitability:
            if not isinstance(entry, dict):
                continue
            fit = entry.get("fit_score")
            fit_text = f"{fit}/100" if fit is not None else "not scored"
            reasons = "; ".join(entry.get("reasons") or []) or "no specific limits found"
            lines.append(
                f"  - {entry.get('crop')}: {entry.get('verdict')} ({fit_text}) — {reasons}"
            )
        lines.append(
            "Do NOT put a crop marked 'not_suitable' into a rotation unless focus_crop "
            "forces it; if forced, include it but state the mitigation clearly. "
            "Prefer 'suitable' crops and explain why the top strategy fits this site."
        )
        lines.append("")

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
                lines.append(f"  - Heat stress days: {dr['heat_stress']}")
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

    if analysis_type == "rotation" and crop_list:
        lines.append(
            "TASK: Recommend exactly 3 crop rotation strategies using ONLY crops "
            "from the ALLOWED CROPS list above. Prefer the farmer's own crops where "
            "they are agronomically sensible; where they are not, substitute "
            "better-fit regionally appropriate crops and say why. Each strategy "
            "should be a sequence of 2-3 crops (e.g., Wheat → Maize → Soybean). "
            "Return valid JSON with summary, crop_suggestions (list of 3 items each "
            "with {crop, justify, priority_alignment, rotation_strategy} where "
            "rotation_strategy is a list of crop names in order), recommendations, "
            "and priority_adjustments fields."
        )
    elif crop_list:
        lines.append(
            "TASK: Recommend crop varieties from ONLY the ALLOWED CROPS list above "
            "that best suit these conditions. Prefer the farmer's own crops where "
            "they fit; otherwise recommend better-fit regionally appropriate crops "
            "and explain the substitution. Return valid JSON with summary, "
            "crop_suggestions (list of {crop, justify, priority_alignment}), "
            "recommendations, and priority_adjustments fields."
        )
    else:
        lines.append(
            "Provide: (1) a concise summary of the key findings, (2) crop "
            "varieties best suited to these conditions, and (3) specific "
            "recommendations aligned with the farmer priorities above. "
            "Format the crop suggestions as a prioritized list with brief "
            "justification for each. Return valid JSON with summary, crop_suggestions "
            "(list of {crop, justify, priority_alignment}), recommendations, and "
            "priority_adjustments fields."
        )

    return "\n".join(lines)


# ── Core: Groq analysis ──────────────────────────────────────────────── #


def analyze_with_groq(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
    analysis_type: str = "general",
    crop_list: Optional[list[str]] = None,
    focus_crop: Optional[str] = None,
    location_profile: Optional[dict[str, Any]] = None,
    crop_suitability: Optional[list[dict[str, Any]]] = None,
    storage_items: Optional[list[dict[str, Any]]] = None,
    farmer_crops: Optional[list[str]] = None,
    regional_crops: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Run Groq AI analysis on pipeline data for enhanced crop recommendations.

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
        crop_list: Optional list of crop names the farmer wants Groq to choose
            from (e.g. ["Wheat", "Maize", "Soybean", ...]).
        focus_crop: Optional crop the farmer wants included in at least one
            rotation strategy / suggestion.
        location_profile: Site-specific climate/soil/terrain profile; treated as
            authoritative location context.
        crop_suitability: Pre-computed location fit for each farmer crop.
        storage_items: Seeds/plants the farmer already has on hand.

    Returns:
        Dict with Groq's analysis output containing:
        - "summary": Concise summary of key findings
        - "crop_suggestions": List of dicts with "crop", "justify", and
          "priority_alignment" fields (and "rotation_strategy" for rotation type)
        - "recommendations": Specific actionable recommendations
        - "priority_adjustments": Suggested priority weight adjustments
        - "model_used": The Groq model name
        - "success": Whether the analysis succeeded

    Raises:
        RuntimeError: If no Groq API key is configured.
    """
    if client is None:
        raise RuntimeError(
            "Groq API key not configured. Set GROQ_API_KEY environment "
            "variable or provide a key via config."
        )

    # Build context string
    context = _build_analysis_context(
        pipeline_data,
        farmer_priorities=farmer_priorities,
        field_location=field_location,
        analysis_type=analysis_type,
        crop_list=crop_list,
        focus_crop=focus_crop,
        location_profile=location_profile,
        crop_suitability=crop_suitability,
        storage_items=storage_items,
        farmer_crops=farmer_crops,
        regional_crops=regional_crops,
    )

    # Construct the prompt
    # Model is configurable; gpt-oss-120b follows the JSON schema far more
    # reliably than the small allam-2-7b model.
    model_name = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

    if analysis_type == "rotation":
        crop_schema = """    {
      "crop": "Crop name from the ALLOWED CROPS list",
      "justify": "Brief justification (1-2 sentences) why this rotation suits the conditions",
      "priority_alignment": "Which farmer priority this best supports",
      "rotation_strategy": ["Crop A", "Crop B", "Crop C"]
    }"""
        rotation_rule = (
            "\nFor rotation analysis: return exactly 3 items in crop_suggestions, "
            "each with a `rotation_strategy` array of 2-3 crop names in planting "
            "order. Every crop name MUST come from the ALLOWED CROPS list. "
            "Prefer the farmer's own crops when they fit; otherwise use the "
            "best-fit regional crops and explain the substitution."
        )
        if focus_crop:
            rotation_rule += (
                f" At least one of the three rotation_strategy sequences MUST "
                f"start with '{focus_crop}'."
            )
    else:
        crop_schema = """    {
      "crop": "Crop name from the ALLOWED CROPS list",
      "justify": "Brief justification (1-2 sentences) why this crop suits the conditions",
      "priority_alignment": "Which farmer priority this best supports"
    }"""
        rotation_rule = (
            "\nEvery crop name MUST come from the ALLOWED CROPS list. "
            "Do not invent crop names. Prefer the farmer's own crops when they "
            "fit; otherwise use the best-fit regional crops."
        )

    prompt = f"""You are an agricultural AI assistant with access to environmental 
data from NASA POWER, Open-Meteo, SoilGrids, and SRTM. You are helping a farmer 
make informed crop-rotation and planting decisions.

{context}

Respond with valid JSON only, using this exact structure:
{{
  "summary": "Concise 2-3 sentence summary of the key agricultural findings",
  "crop_suggestions": [
{crop_schema}
  ],
  "recommendations": "Specific actionable recommendations (2-3 bullet points)",
  "priority_adjustments": {{
    "suggested_changes": "Description of suggested priority weight changes",
    "reasoning": "Why these changes would improve outcomes"
  }}
}}
{rotation_rule}
Rank the suggestions best-fit first. Use the environmental data provided, be
practical and evidence-based, and stay directly responsive to the farmer
priorities above.
"""

    # Call Groq - using 'content' not 'contents' (Groq API format)
    try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
        except Exception:
            # Some models do not support JSON mode; retry without it.
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2048,
            )

        response_text = response.choices[0].message.content

        # Try to parse JSON from the response
        result = _parse_groq_response(response_text)

        # Add metadata
        result["model_used"] = model_name
        result["success"] = True

        return result

    except Exception as e:
        # Return structured error information
        return {
            "summary": f"Groq analysis failed: {str(e)}",
            "crop_suggestions": [],
            "recommendations": [],
            "priority_adjustments": {"suggested_changes": "", "reasoning": ""},
            "model_used": model_name,
            "success": False,
        }


def _parse_groq_response(response_text: str) -> dict[str, Any]:
    """Parse Groq response, extracting JSON if wrapped in text.

    Groq's allam-2-7b model returns valid JSON natively, so we try to parse
    the entire response as JSON first, then fall back to text extraction.
    """
    result: dict[str, Any] = {
        "summary": "",
        "crop_suggestions": [],
        "recommendations": "",
        "priority_adjustments": {"suggested_changes": "", "reasoning": ""},
    }

    # Try to parse the entire response as JSON first (Groq models return JSON natively)
    try:
        parsed = json.loads(response_text.strip())
        if isinstance(parsed, dict):
            result["summary"] = parsed.get("summary", "")

            # Crop suggestions
            raw_suggestions = parsed.get("crop_suggestions", [])
            suggestions = []
            for suggestion in raw_suggestions:
                if isinstance(suggestion, dict):
                    suggestions.append({
                        "crop": suggestion.get("crop", ""),
                        "justify": suggestion.get("justify", ""),
                        "priority_alignment": suggestion.get(
                            "priority_alignment", ""
                        ),
                        "rotation_strategy": suggestion.get(
                            "rotation_strategy", []
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
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Fallback: look for JSON block with triple-backticks
    if "```" in response_text:
        parts = response_text.split("```")
        if len(parts) >= 3:
            json_text = parts[1].strip()
            try:
                parsed = json.loads(json_text)
                if isinstance(parsed, dict):
                    result["summary"] = parsed.get("summary", "")

                    raw_suggestions = parsed.get("crop_suggestions", [])
                    suggestions = []
                    for suggestion in raw_suggestions:
                        if isinstance(suggestion, dict):
                            suggestions.append({
                                "crop": suggestion.get("crop", ""),
                                "justify": suggestion.get("justify", ""),
                                "priority_alignment": suggestion.get(
                                    "priority_alignment", ""
                                ),
                                "rotation_strategy": suggestion.get(
                                    "rotation_strategy", []
                                ),
                            })
                    result["crop_suggestions"] = suggestions

                    result["recommendations"] = parsed.get("recommendations", "")

                    pa = parsed.get("priority_adjustments", {})
                    result["priority_adjustments"] = {
                        "suggested_changes": pa.get("suggested_changes", ""),
                        "reasoning": pa.get("reasoning", ""),
                    }
                    return result
            except json.JSONDecodeError:
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
    crop_list: Optional[list[str]] = None,
    focus_crop: Optional[str] = None,
    *,
    location_profile: Optional[dict[str, Any]] = None,
    crop_suitability: Optional[list[dict[str, Any]]] = None,
    storage_items: Optional[list[dict[str, Any]]] = None,
    farmer_crops: Optional[list[str]] = None,
    regional_crops: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Get Groq-powered crop suggestions for given conditions.

    This is a convenience wrapper around analyze_with_groq optimized
    for the common use case of crop suggestions.

    Args:
        pipeline_data: Environmental data dict.
        farmer_priorities: Priority weights dict.
        field_location: (lat, lon) tuple.
        crop_list: Optional list of crop names to choose from.
        focus_crop: Optional crop to prioritize.

    Returns:
        Dict with crop_suggestions list and summary.
    """
    result = analyze_with_groq(
        pipeline_data=pipeline_data,
        farmer_priorities=farmer_priorities,
        field_location=field_location,
        analysis_type="crop_suggestion",
        crop_list=crop_list,
        focus_crop=focus_crop,
        location_profile=location_profile,
        crop_suitability=crop_suitability,
        storage_items=storage_items,
        farmer_crops=farmer_crops,
        regional_crops=regional_crops,
    )

    return {
        "summary": result.get("summary", ""),
        "crop_suggestions": result.get("crop_suggestions", []),
        "recommendations": result.get("recommendations", ""),
        "model_used": result.get("model_used"),
        "success": result.get("success", False),
    }


def get_rotation_analysis(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
    crop_list: Optional[list[str]] = None,
    focus_crop: Optional[str] = None,
    *,
    location_profile: Optional[dict[str, Any]] = None,
    crop_suitability: Optional[list[dict[str, Any]]] = None,
    storage_items: Optional[list[dict[str, Any]]] = None,
    farmer_crops: Optional[list[str]] = None,
    regional_crops: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Get Groq-powered rotation analysis and suggestions.

    Args:
        pipeline_data: Environmental data dict.
        farmer_priorities: Priority weights dict.
        field_location: (lat, lon) tuple.
        crop_list: Optional list of crop names to choose from for rotation.
        focus_crop: Optional crop that must appear in at least one rotation.

    Returns:
        Dict with analysis summary and recommendations.
    """
    result = analyze_with_groq(
        pipeline_data=pipeline_data,
        farmer_priorities=farmer_priorities,
        field_location=field_location,
        analysis_type="rotation",
        crop_list=crop_list,
        focus_crop=focus_crop,
        location_profile=location_profile,
        crop_suitability=crop_suitability,
        storage_items=storage_items,
        farmer_crops=farmer_crops,
        regional_crops=regional_crops,
    )

    return {
        "summary": result.get("summary", ""),
        "crop_suggestions": result.get("crop_suggestions", []),
        "recommendations": result.get("recommendations", ""),
        "priority_adjustments": result.get("priority_adjustments", {}),
        "model_used": result.get("model_used"),
        "success": result.get("success", False),
    }


def get_trend_analysis(
    pipeline_data: dict[str, Any],
    farmer_priorities: Optional[dict[str, float]] = None,
    field_location: Optional[tuple[float, float]] = None,
) -> dict[str, Any]:
    """Get Groq-powered long-term trend and water trend analysis.

    Args:
        pipeline_data: Environmental data dict (should include long-term trends).
        farmer_priorities: Priority weights dict.
        field_location: (lat, lon) tuple.

    Returns:
        Dict with trend analysis and implications.
    """
    result = analyze_with_groq(
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
        "model_used": result.get("model_used"),
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