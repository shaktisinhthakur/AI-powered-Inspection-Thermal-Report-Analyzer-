import json
import logging
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import (
    DDRSummaryResponse,
    ProviderEnum,
    ProvidersResponse,
    ValidateKeyResponse,
)
from app.providers import get_provider, LLMProvider
from app.parsing import parse_input
from app.extraction import run_stage2
from app.image_matching import run_stage3
from app.merge_reasoning import run_stage4
from app.render import run_stage5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="DDR Report Generator",
    description="Generate Detailed Diagnostic Reports from Inspection and Thermal PDFs",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ──────────────────────── Provider + Key validation ────────────────────────

SUPPORTED_PROVIDERS = [e.value for e in ProviderEnum]


def resolve_provider(x_provider: str, x_api_key: str) -> LLMProvider:
    """Validate headers and return an LLMProvider instance.

    Raises HTTPException with 400/401 on failure.
    Never leaks the API key in logs or error messages.
    """
    if not x_provider:
        raise HTTPException(status_code=400, detail="Missing X-Provider header")
    if not x_api_key:
        raise HTTPException(status_code=400, detail="Missing X-API-Key header")

    provider_name = x_provider.lower().strip()
    if provider_name not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider: '{x_provider}'. Supported: {SUPPORTED_PROVIDERS}",
        )

    try:
        provider = get_provider(provider_name, x_api_key)
    except Exception as exc:
        logger.error("Failed to instantiate provider %s: %s", provider_name, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to initialize provider: {provider_name}",
        )

    # Validate the key with a cheap call
    try:
        valid = provider.validate_key()
    except Exception:
        valid = False

    if not valid:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid or unauthorized API key for provider: {provider_name}",
        )

    return provider


# ──────────────────────────────── Routes ────────────────────────────────


@app.get("/providers", response_model=ProvidersResponse)
async def list_providers():
    return ProvidersResponse(supported=SUPPORTED_PROVIDERS)


@app.post("/validate-key", response_model=ValidateKeyResponse)
async def validate_key(
    x_provider: str = Header(...),
    x_api_key: str = Header(...),
):
    provider_name = x_provider.lower().strip()
    if provider_name not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider: '{x_provider}'",
        )

    try:
        provider = get_provider(provider_name, x_api_key)
        valid = provider.validate_key()
    except Exception:
        valid = False

    return ValidateKeyResponse(provider=provider_name, valid=valid)


@app.post("/generate-ddr", response_model=DDRSummaryResponse)
async def generate_ddr(
    inspection_report: UploadFile = File(...),
    thermal_report: UploadFile = File(...),
    additional_images: UploadFile = File(None),
    x_provider: str = Header(...),
    x_api_key: str = Header(...),
):
    # ── 0. Resolve provider (fails fast with 400/401) ────────────
    provider = resolve_provider(x_provider, x_api_key)
    provider_name = x_provider.lower().strip()

    job_id = uuid.uuid4().hex[:12]
    logger.info("Job %s started — provider: %s", job_id, provider_name)

    # ── Read uploaded files ──────────────────────────────────────
    insp_bytes = await inspection_report.read()
    therm_bytes = await thermal_report.read()

    if not insp_bytes or not therm_bytes:
        raise HTTPException(status_code=400, detail="Both PDF files are required")

    # ── Stage 1: Parse (supports PDF or ZIP for each input) ──────
    try:
        insp_doc = parse_input(insp_bytes, source_label="inspection_report")
        therm_doc = parse_input(therm_bytes, source_label="thermal_report")
    except Exception as exc:
        logger.exception("Stage 1 (parsing) failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"Report parsing failed: {exc}")

    # ── Additional images from zip ────────────────────────────────
    extra_images: list = []
    if additional_images and additional_images.filename:
        try:
            zip_bytes = await additional_images.read()
            from app.parsing import extract_images_from_zip
            extra_images = extract_images_from_zip(zip_bytes)
            logger.info("Loaded %d additional images from zip.", len(extra_images))
        except Exception as exc:
            logger.warning("Failed to process additional_images zip: %s", exc)

    # ── Stage 2: Structured extraction (one LLM call per doc) ────
    try:
        insp_obs = run_stage2(provider, insp_doc, source_label="inspection_report")
        therm_obs = run_stage2(provider, therm_doc, source_label="thermal_report")
    except Exception as exc:
        logger.exception("Stage 2 (extraction) failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"Structured extraction failed: {exc}")

    # ── Stage 3: Image matching ──────────────────────────────────
    all_images = insp_doc.images + therm_doc.images + extra_images
    try:
        matched_images = run_stage3(provider, all_images, insp_obs + therm_obs)
    except Exception as exc:
        logger.exception("Stage 3 (image matching) failed for job %s", job_id)
        matched_images = all_images  # carry on without captions

    # ── Stage 4: Merge & reason ──────────────────────────────────
    try:
        ddr_json = run_stage4(provider, insp_obs, therm_obs, matched_images)
    except Exception as exc:
        logger.exception("Stage 4 (merge & reason) failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"Merge & reasoning failed: {exc}")

    # ── Stage 5: Render ──────────────────────────────────────────
    try:
        docx_bytes = run_stage5(ddr_json, matched_images)
    except Exception as exc:
        logger.exception("Stage 5 (render) failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"DOCX rendering failed: {exc}")

    # ── Save to output ───────────────────────────────────────────
    output_path = OUTPUT_DIR / f"{job_id}_DDR.docx"
    output_path.write_bytes(docx_bytes)
    logger.info("Job %s finished — output: %s (%d bytes)", job_id, output_path, len(docx_bytes))

    return DDRSummaryResponse(
        job_id=job_id,
        provider=provider_name,
        docx_download_url=f"/download/{job_id}",
        summary_json=ddr_json,
    )


@app.get("/download/{job_id}")
async def download_ddr(job_id: str):
    output_path = OUTPUT_DIR / f"{job_id}_DDR.docx"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail=f"No DDR found for job ID: {job_id}")
    return FileResponse(
        path=str(output_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"DDR_{job_id}.docx",
    )


# ── Dev-only root redirect to static form ──────────────────────────
@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/index.html")
