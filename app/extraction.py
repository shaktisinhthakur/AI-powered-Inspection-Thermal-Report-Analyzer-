import json
import logging
from app.schemas import ParsedDocument, Observation, STAGE2_OBSERVATION_SCHEMA
from app.prompts import STAGE2_SYSTEM_PROMPT
from app.providers import LLMProvider

logger = logging.getLogger(__name__)


def run_stage2(
    provider: LLMProvider,
    doc: ParsedDocument,
    source_label: str = "",
) -> list[Observation]:
    """Stage 2: Use the LLM provider to extract structured observations.

    Sends the concatenated page text through provider.extract_structured().
    Retries once with a correction message if the JSON is malformed.
    """
    full_text = "\n\n--- PAGE BREAK ---\n\n".join(
        f"Page {i+1}:\n{text}"
        for i, text in enumerate(doc.pages_text)
    )

    if not full_text.strip():
        logger.warning("No text content in %s — returning empty observations.", source_label)
        return []

    try:
        result = provider.extract_structured(full_text, STAGE2_OBSERVATION_SCHEMA)
    except Exception as exc:
        logger.error("Stage 2 LLM call failed for %s: %s", source_label, exc)
        # One retry with correction hint
        try:
            correction_msg = (
                "Your last output was invalid or failed. Return ONLY valid JSON "
                "matching the schema: " + json.dumps(STAGE2_OBSERVATION_SCHEMA)
            )
            result = provider.extract_structured(
                full_text + "\n\n" + correction_msg,
                STAGE2_OBSERVATION_SCHEMA,
            )
        except Exception as exc2:
            logger.error("Stage 2 retry also failed for %s: %s", source_label, exc2)
            return []

    observations = result.get("observations", [])
    parsed: list[Observation] = []
    for obs in observations:
        try:
            parsed.append(Observation(
                area=obs.get("area", ""),
                observation=obs.get("observation", ""),
                temp_reading=obs.get("temp_reading"),
                severity_hint=obs.get("severity_hint"),
                recommended_action_hint=obs.get("recommended_action_hint"),
                page_ref=obs.get("page_ref"),
                source_document=source_label,
                nearby_image_refs=obs.get("nearby_image_refs", []),
            ))
        except Exception as e:
            logger.warning("Skipping malformed observation in %s: %s — %s", source_label, obs, e)

    logger.info("Stage 2 complete for %s: %d observations extracted.", source_label, len(parsed))
    return parsed
