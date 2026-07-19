import io
import logging
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from app.schemas import ExtractedImage

logger = logging.getLogger(__name__)


def run_stage5(ddr_json: dict, images: list[ExtractedImage]) -> bytes:
    """Stage 5: Render the DDR JSON into a .docx file and return bytes.

    Images are embedded per area using matched_image_ids from the DDR.
    Unmatched images show "Image Not Available".
    """
    doc = Document()
    _set_styles(doc)

    # Build lookup: image_id string -> ExtractedImage
    image_by_id: dict[str, ExtractedImage] = {
        str(idx): img for idx, img in enumerate(images)
    }

    # ── Title Page ────────────────────────────────────────────────
    summary = ddr_json.get("property_issue_summary", "Detailed Diagnostic Report")
    short_title = summary[:80] + "..." if len(summary) > 80 else summary

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("Detailed Diagnostic Report")
    run.bold = True
    run.font.size = Pt(24)
    run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)

    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run2 = p2.add_run(short_title)
    run2.font.size = Pt(14)
    run2.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    from datetime import date
    p3 = doc.add_paragraph()
    p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run3 = p3.add_run(f"Report Date: {date.today().isoformat()}")
    run3.font.size = Pt(11)
    run3.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    doc.add_page_break()

    # ── Section 1: Property Issue Summary ─────────────────────────
    _add_section_heading(doc, "1. Property Issue Summary")
    doc.add_paragraph(summary)

    # ── Section 2: Area-wise Observations ─────────────────────────
    _add_section_heading(doc, "2. Area-wise Observations")

    for area_entry in ddr_json.get("area_wise_observations", []):
        area_name = area_entry.get("area", "Unnamed Area")
        doc.add_heading(area_name, level=2)

        obs_text = area_entry.get("observation_text", "Not Available")
        doc.add_paragraph(obs_text)

        # Conflict note
        conflict_note = area_entry.get("conflict_note")
        if conflict_note:
            note_p = doc.add_paragraph(conflict_note)
            for run in note_p.runs:
                run.font.italic = True
                run.font.color.rgb = RGBColor(0xCC, 0x55, 0x00)

        # Embedded images for this area
        matched_ids = area_entry.get("matched_image_ids", [])
        if matched_ids:
            for img_id in matched_ids:
                img = image_by_id.get(img_id)
                if img and img.caption:
                    _embed_image(doc, img)
                    if img.area_tag:
                        cap_p = doc.add_paragraph(f"Image: {img.area_tag}")
                        cap_p.runs[0].font.size = Pt(9)
                        cap_p.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)
                else:
                    doc.add_paragraph("Image Not Available").italic = True
        else:
            doc.add_paragraph("Image Not Available").italic = True

        doc.add_paragraph()  # spacer

    # ── Section 3: Probable Root Cause ────────────────────────────
    _add_section_heading(doc, "3. Probable Root Cause")
    root_causes = ddr_json.get("probable_root_cause", [])
    if root_causes:
        for rc in root_causes:
            area_text = rc.get("area", "Not Available")
            cause_text = rc.get("cause", "Not Available")
            confidence = rc.get("confidence", "")
            label = f"[{area_text}] (Confidence: {confidence})" if confidence else f"[{area_text}]"
            cause_p = doc.add_paragraph()
            run_b = cause_p.add_run(label + " ")
            run_b.bold = True
            cause_p.add_run(cause_text)
    else:
        doc.add_paragraph("Not Available")

    # ── Section 4: Severity Assessment ────────────────────────────
    _add_section_heading(doc, "4. Severity Assessment")
    sev_list = ddr_json.get("severity_assessment", [])
    if sev_list:
        for sev in sev_list:
            area_text = sev.get("area", "Not Available")
            level = sev.get("severity", "Not Available")
            reasoning = sev.get("reasoning", "")
            sev_p = doc.add_paragraph()
            run_b = sev_p.add_run(f"[{area_text}] Severity: {level}")
            run_b.bold = True
            if reasoning:
                doc.add_paragraph(reasoning)
    else:
        doc.add_paragraph("Not Available")

    # ── Section 5: Recommended Actions ────────────────────────────
    _add_section_heading(doc, "5. Recommended Actions")
    actions = ddr_json.get("recommended_actions", [])
    if actions:
        for act in actions:
            area_text = act.get("area", "Not Available")
            action_text = act.get("action", "Not Available")
            priority = act.get("priority", "")
            label = f"[{area_text}] ({priority})" if priority else f"[{area_text}]"
            act_p = doc.add_paragraph()
            run_b = act_p.add_run(label + " ")
            run_b.bold = True
            act_p.add_run(action_text)
    else:
        doc.add_paragraph("Not Available")

    # ── Section 6: Additional Notes ───────────────────────────────
    _add_section_heading(doc, "6. Additional Notes")
    doc.add_paragraph(ddr_json.get("additional_notes", "Not Available"))

    # ── Section 7: Missing or Unclear Information ─────────────────
    _add_section_heading(doc, "7. Missing or Unclear Information")
    missing = ddr_json.get("missing_or_unclear_information", [])
    if missing:
        for item in missing:
            doc.add_paragraph(item, style="List Bullet")
    else:
        doc.add_paragraph("Not Available")

    # ── Output ────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    logger.info("Stage 5 complete: DOCX rendered (%d bytes).", len(buf.getvalue()))
    return buf.getvalue()


# ──────────────────────── Helpers ────────────────────────

def _set_styles(doc: Document):
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.paragraph_format.space_after = Pt(6)


def _add_section_heading(doc: Document, text: str):
    heading = doc.add_heading(text, level=1)
    for run in heading.runs:
        run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)


def _embed_image(doc: Document, img: ExtractedImage):
    """Insert an image into the document at a reasonable size."""
    try:
        stream = io.BytesIO(img.image_bytes)
        doc.add_picture(stream, width=Inches(4.5))
    except Exception as exc:
        logger.warning("Failed to embed image (page %d): %s", img.page_number, exc)
        doc.add_paragraph("[Image could not be embedded]")
