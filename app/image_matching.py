import logging
import re
from typing import Optional
from app.schemas import ExtractedImage, Observation
from app.prompts import STAGE3_VISION_PROMPT
from app.providers import LLMProvider

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "this", "that", "these",
    "those", "it", "its", "shows", "showing", "seen", "visible", "image",
})


def _extract_keywords(text: str) -> list[str]:
    """Derive meaningful keywords from a caption string."""
    text = text.lower()
    # Split on non-alpha characters
    words = re.findall(r"[a-z]+", text)
    return [w for w in words if w not in _STOP_WORDS and len(w) > 2]


def run_stage3(
    provider: LLMProvider,
    images: list[ExtractedImage],
    observations: list[Observation],
) -> list[ExtractedImage]:
    """Stage 3: Caption images via provider and match to observations.

    For each image:
      1. Send to provider.caption_image() for a caption + area tag.
         (If the provider doesn't support vision, skip gracefully.)
      2. Match to an observation using page number + keyword overlap.
      3. If no confident match, leave unmatched (renderer will show
         'Image Not Available').

    Returns the list of ExtractedImage objects with caption, area_tag,
    and matched_observation_idx filled in where possible.
    """
    if not provider.supports_vision:
        logger.warning(
            "Provider %s does not support vision. Skipping image captioning. "
            "All images will be marked as unmatched.",
            type(provider).__name__,
        )
        return _mark_all_unmatched(images)

    for idx, img in enumerate(images):
        try:
            caption_result = provider.caption_image(
                img.image_bytes,
                STAGE3_VISION_PROMPT,
            )
        except Exception as exc:
            logger.warning(
                "Failed to caption image %d (page %d): %s",
                idx, img.page_number, exc,
            )
            continue

        img.caption = caption_result.get("caption", "")
        img.area_tag = caption_result.get("area_tag", "")

        # Derive keywords from caption text
        caption_keywords = _extract_keywords(img.caption)
        is_thermal = caption_result.get("is_thermal_image", False)

        # ── Match to best observation ──────────────────────────
        match_idx = _match_observation(
            image=img,
            caption_keywords=caption_keywords,
            observations=observations,
            is_thermal=is_thermal,
        )
        img.matched_observation_idx = match_idx
        if match_idx is not None:
            logger.info(
                "Image %d (page %d) matched to observation %d: %s",
                idx, img.page_number, match_idx, observations[match_idx].area,
            )
        else:
            logger.info("Image %d (page %d) — no confident match.", idx, img.page_number)

    matched_count = sum(1 for img in images if img.matched_observation_idx is not None)
    logger.info(
        "Stage 3 complete: %d/%d images matched to observations.",
        matched_count, len(images),
    )
    return images


def _match_observation(
    image: ExtractedImage,
    caption_keywords: list[str],
    observations: list[Observation],
    is_thermal: bool = False,
) -> Optional[int]:
    """Score each observation and return the index of the best match.

    Scoring criteria (descending weight):
      - Same page reference   → +3
      - Keyword overlap with observation text  → +2 per match
      - Thermal image matched to thermal source observation → +2
      - Area tag in area name  → +1
      - Area tag in observation text → +1
    Threshold: minimum score of 3 for a match.
    """
    best_idx: Optional[int] = None
    best_score = 0

    caption_kw_lower = [kw.lower().strip() for kw in caption_keywords]
    area_tag_lower = (image.area_tag or "").lower().strip()

    for obs_idx, obs in enumerate(observations):
        score = 0

        # Page number match
        if obs.page_ref and obs.page_ref == image.page_number:
            score += 3

        # Keyword overlap with observation text + area
        obs_text_lower = (obs.observation + " " + obs.area).lower()
        for ckw in caption_kw_lower:
            if ckw and ckw in obs_text_lower:
                score += 2
            elif ckw and len(ckw) > 3:
                for word in obs_text_lower.split():
                    if ckw in word or word in ckw:
                        score += 1
                        break

        # Thermal image matched to thermal source observation
        if is_thermal and obs.source_document == "thermal_report":
            score += 2

        # Area tag in observation area name
        if area_tag_lower:
            if area_tag_lower in obs.area.lower() or obs.area.lower() in area_tag_lower:
                score += 1
            if area_tag_lower in obs.observation.lower():
                score += 1

        if score > best_score:
            best_score = score
            best_idx = obs_idx

    if best_score >= 3:
        return best_idx
    return None


def _mark_all_unmatched(images: list[ExtractedImage]) -> list[ExtractedImage]:
    for img in images:
        img.caption = None
        img.area_tag = None
        img.matched_observation_idx = None
    return images
