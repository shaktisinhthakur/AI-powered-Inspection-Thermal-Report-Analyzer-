from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
from dataclasses import dataclass, field


class ProviderEnum(str, Enum):
    anthropic = "anthropic"
    openai = "openai"
    groq = "groq"
    gemini = "gemini"


# ───────────────────────────── Internal data types ─────────────────────────────

@dataclass
class ExtractedImage:
    image_bytes: bytes
    page_number: int = 0
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    caption: Optional[str] = None
    matched_observation_idx: Optional[int] = None
    area_tag: Optional[str] = None
    file_name: Optional[str] = None


@dataclass
class Observation:
    area: str
    observation: str
    temp_reading: Optional[str] = None
    severity_hint: Optional[str] = None
    recommended_action_hint: Optional[str] = None
    page_ref: Optional[int] = None
    source_document: str = ""
    nearby_image_refs: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    pages_text: list[str]
    images: list[ExtractedImage]


# ───────────────────────────── Stage 2 output schema ─────────────────────────

STAGE2_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "observation": {"type": "string"},
                    "temp_reading": {"type": "string"},
                    "severity_hint": {"type": "string"},
                    "recommended_action_hint": {"type": "string"},
                    "page_ref": {"type": "integer"},
                    "source_document": {"type": "string"},
                    "nearby_image_refs": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["area", "observation"]
            }
        }
    },
    "required": ["observations"]
}


# ───────────────────────────── Stage 3 output schema ─────────────────────────

STAGE3_CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "area_tag": {"type": "string"},
        "is_thermal_image": {"type": "boolean"},
        "visible_temp_reading": {"type": "string"}
    },
    "required": ["caption", "area_tag", "is_thermal_image"]
}


# ───────────────────────────── Stage 4 output schema ─────────────────────────

STAGE4_DDR_SCHEMA = {
    "type": "object",
    "properties": {
        "property_issue_summary": {"type": "string"},
        "area_wise_observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "observation_text": {"type": "string"},
                    "conflict_note": {"type": "string"},
                    "matched_image_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["area", "observation_text"]
            }
        },
        "probable_root_cause": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "cause": {"type": "string"},
                    "confidence": {"type": "string"}
                },
                "required": ["area", "cause", "confidence"]
            }
        },
        "severity_assessment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "severity": {"type": "string"},
                    "reasoning": {"type": "string"}
                },
                "required": ["area", "severity", "reasoning"]
            }
        },
        "recommended_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "action": {"type": "string"},
                    "priority": {"type": "string"}
                },
                "required": ["area", "action", "priority"]
            }
        },
        "additional_notes": {"type": "string"},
        "missing_or_unclear_information": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": [
        "property_issue_summary",
        "area_wise_observations",
        "probable_root_cause",
        "severity_assessment",
        "recommended_actions",
        "additional_notes",
        "missing_or_unclear_information"
    ]
}


# ───────────────────────────── API / Pydantic models ─────────────────────────

class ValidateKeyRequest(BaseModel):
    provider: str
    api_key: str


class ValidateKeyResponse(BaseModel):
    provider: str
    valid: bool


class ProvidersResponse(BaseModel):
    supported: list[str]


class DDRSummaryResponse(BaseModel):
    job_id: str
    provider: str
    docx_download_url: str
    summary_json: dict
