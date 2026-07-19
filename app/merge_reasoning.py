import json
import logging
from typing import Optional
from app.schemas import Observation, ExtractedImage, STAGE4_DDR_SCHEMA
from app.prompts import STAGE4_SYSTEM_PROMPT
from app.providers import LLMProvider

logger = logging.getLogger(__name__)


def run_stage4(
    provider: LLMProvider,
    inspection_observations: list[Observation],
    thermal_observations: list[Observation],
    images: list[ExtractedImage],
) -> dict:
    """Stage 4: Merge observations from both reports + image mappings,
    then call the LLM to produce a structured DDR JSON.

    Builds a payload describing all observations and image-observation
    mappings for the LLM to reason over.
    """
    payload = _build_merge_payload(
        inspection_observations,
        thermal_observations,
        images,
    )

    logger.info(
        "Stage 4 input: %d inspection obs, %d thermal obs, %d images (%d matched).",
        len(inspection_observations),
        len(thermal_observations),
        len(images),
        sum(1 for img in images if img.matched_observation_idx is not None),
    )

    try:
        result = provider.merge_and_reason(payload, STAGE4_DDR_SCHEMA)
    except Exception as exc:
        logger.error("Stage 4 LLM call failed: %s", exc)
        # Retry with correction hint
        try:
            correction_msg = (
                "Your last output was invalid or failed. Return ONLY valid JSON "
                "matching the schema provided."
            )
            result = provider.merge_and_reason(
                {"correction": correction_msg, "original_payload": payload},
                STAGE4_DDR_SCHEMA,
            )
        except Exception as exc2:
            logger.error("Stage 4 retry also failed: %s", exc2)
            return _build_fallback_ddr(inspection_observations, thermal_observations)

    _validate_ddr_sections(result)
    return result


def _build_merge_payload(
    inspection_obs: list[Observation],
    thermal_obs: list[Observation],
    images: list[ExtractedImage],
) -> dict:
    """Construct the structured payload for the Stage 4 LLM call."""

    def obs_to_dict(obs: Observation) -> dict:
        return {
            "area": obs.area,
            "observation": obs.observation,
            "temp_reading": obs.temp_reading,
            "severity_hint": obs.severity_hint,
            "recommended_action_hint": obs.recommended_action_hint,
            "page_ref": obs.page_ref,
            "source_document": obs.source_document,
            "nearby_image_refs": obs.nearby_image_refs,
        }

    image_mappings = []
    for img_idx, img in enumerate(images):
        entry = {
            "image_id": str(img_idx),
            "page_number": img.page_number,
            "caption": img.caption,
            "area_tag": img.area_tag,
        }
        if img.matched_observation_idx is not None:
            entry["matched_observation"] = obs_to_dict(
                (inspection_obs + thermal_obs)[img.matched_observation_idx]
                if img.matched_observation_idx < len(inspection_obs) + len(thermal_obs)
                else Observation(area="", observation="")
            )
        image_mappings.append(entry)

    return {
        "inspection_report_observations": [obs_to_dict(o) for o in inspection_obs],
        "thermal_report_observations": [obs_to_dict(o) for o in thermal_obs],
        "image_mappings": image_mappings,
        "instructions": (
            "Merge the observations from both reports. "
            "Handle duplicates, conflicts, and missing data according to the rules. "
            "Produce the 7-section DDR JSON."
        ),
    }


def _validate_ddr_sections(ddr: dict):
    """Log a warning if any required section is missing."""
    required = [
        "property_issue_summary",
        "area_wise_observations",
        "probable_root_cause",
        "severity_assessment",
        "recommended_actions",
        "additional_notes",
        "missing_or_unclear_information",
    ]
    for section in required:
        if section not in ddr:
            logger.warning("Stage 4 output missing required section: '%s'", section)
    if "area_wise_observations" in ddr:
        logger.info(
            "Stage 4 complete: %d areas in report.",
            len(ddr["area_wise_observations"]),
        )


def _build_fallback_ddr(
    inspection_obs: list[Observation],
    thermal_obs: list[Observation],
) -> dict:
    """Minimal DDR when Stage 4 LLM call fails completely."""
    area_map: dict[str, list[str]] = {}
    for obs in inspection_obs + thermal_obs:
        if obs.area not in area_map:
            area_map[obs.area] = []
        area_map[obs.area].append(obs.observation)

    area_wise = [
        {
            "area": area,
            "observation_text": "; ".join(descs),
            "conflict_note": None,
            "matched_image_ids": [],
        }
        for area, descs in area_map.items()
    ]

    return {
        "property_issue_summary": "Not Available — LLM stage failed",
        "area_wise_observations": area_wise or [
            {"area": "Not Available", "observation_text": "No observations could be extracted.",
             "conflict_note": None, "matched_image_ids": []}
        ],
        "probable_root_cause": [
            {"area": "Not Available", "cause": "Not Available", "confidence": "Low"}
        ],
        "severity_assessment": [
            {"area": "Not Available", "severity": "Not Available", "reasoning": "Not Available"}
        ],
        "recommended_actions": [
            {"area": "Not Available", "action": "Not Available", "priority": "Routine"}
        ],
        "additional_notes": "This report was generated in fallback mode because the LLM processing stage encountered an error.",
        "missing_or_unclear_information": ["All sections — LLM stage failed"],
    }
