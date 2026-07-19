import io
import logging
import zipfile
from app.schemas import ParsedDocument, ExtractedImage

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}


def parse_input(data: bytes, source_label: str = "") -> ParsedDocument:
    """Auto-detect PDF or ZIP and parse accordingly.

    - PDF bytes → delegate to parse_pdf()
    - ZIP bytes → extract all PDFs, images, and text files inside

    This lets the user pass either a .pdf file or a .zip folder for
    each report input.
    """
    if data[:4] == b"%PDF":
        return parse_pdf(data, source_label)

    if data[:2] == b"PK":
        return _parse_zip(data, source_label)

    logger.warning("Unknown format for %s — treating as PDF (may fail).", source_label)
    return parse_pdf(data, source_label)


def _parse_zip(zip_bytes: bytes, source_label: str = "") -> ParsedDocument:
    """Extract text + images from a zip archive.

    - Any .pdf files inside are parsed with parse_pdf()
    - Any image files are collected as ExtractedImage
    - Any .txt files are read as page text
    """
    pages_text: list[str] = []
    images: list[ExtractedImage] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
            ext = f".{ext}"

            try:
                entry_bytes = zf.read(name)
            except Exception:
                continue

            if ext == ".pdf":
                sub = parse_pdf(entry_bytes, source_label=f"{source_label}/{name}")
                pages_text.extend(sub.pages_text)
                images.extend(sub.images)

            elif ext in _IMAGE_EXTENSIONS and len(entry_bytes) > 100:
                images.append(ExtractedImage(
                    image_bytes=entry_bytes,
                    page_number=0,
                    file_name=name,
                ))

            elif ext == ".txt":
                try:
                    text = entry_bytes.decode("utf-8", errors="replace").strip()
                    if text:
                        pages_text.append(f"--- {name} ---\n{text}")
                except Exception:
                    pass

    logger.info(
        "Parsed ZIP %s: %d text blocks, %d images.",
        source_label, len(pages_text), len(images),
    )
    return ParsedDocument(pages_text=pages_text, images=images)


def parse_pdf(pdf_bytes: bytes, source_label: str = "") -> ParsedDocument:
    """Stage 1: Parse a PDF and return full text per page + embedded images.

    Uses PyMuPDF as primary. Falls back to pdfplumber for table extraction
    if the text extracted by PyMuPDF is very sparse (e.g. scanned document).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_text: list[str] = []
    images: list[ExtractedImage] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().strip()
        pages_text.append(text)

        # Extract embedded images with bbox
        image_list = page.get_images(full=True)
        for img_idx, img_info in enumerate(image_list):
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            bbox = _get_image_bbox(page, img_idx)
            images.append(ExtractedImage(
                image_bytes=img_bytes,
                page_number=page_num + 1,
                bbox=bbox,
            ))

    doc.close()

    total_text = sum(len(t) for t in pages_text)
    if total_text < 100:
        logger.warning(
            "Very little text extracted from %s (%d chars). "
            "Trying pdfplumber fallback for table data.",
            source_label, total_text,
        )
        try:
            tables_text = _extract_tables_fallback(pdf_bytes)
            if tables_text.strip():
                pages_text.append(tables_text)
                logger.info("pdfplumber fallback recovered %d chars of table data.", len(tables_text))
        except Exception as exc:
            logger.warning("pdfplumber fallback failed: %s", exc)

    logger.info(
        "Parsed %s: %d pages, %d images, %d total text chars.",
        source_label, len(pages_text), len(images), total_text,
    )
    return ParsedDocument(pages_text=pages_text, images=images)


def _get_image_bbox(page, img_idx: int) -> tuple[float, float, float, float]:
    """Extract the bbox of the img_idx-th image on the page."""
    try:
        blocks = page.get_text("dict")["blocks"]
        image_blocks = [b for b in blocks if b.get("type") == 1]  # type 1 = image
        if img_idx < len(image_blocks):
            bbox = image_blocks[img_idx]["bbox"]
            return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except Exception:
        pass
    return (0.0, 0.0, 0.0, 0.0)


def _extract_tables_fallback(pdf_bytes: bytes) -> str:
    """Use pdfplumber to extract tabular data as text."""
    import pdfplumber
    lines: list[str] = []
    with pdfplumber.open(pdf_bytes) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    row_text = " | ".join(
                        cell.strip() if cell else ""
                        for cell in row
                    )
                    if row_text.strip():
                        lines.append(row_text)
            text = page.extract_text()
            if text:
                lines.append(text)
    return "\n".join(lines)


def extract_images_from_zip(zip_bytes: bytes) -> list[ExtractedImage]:
    """Extract images from a zip file. Each image becomes an ExtractedImage
    with page_number=0 and its filename as file_name."""
    images: list[ExtractedImage] = []
    _image_extensions = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
            if f".{ext}" not in _image_extensions:
                continue
            try:
                img_bytes = zf.read(name)
                if len(img_bytes) < 100:
                    continue
                images.append(ExtractedImage(
                    image_bytes=img_bytes,
                    page_number=0,
                    file_name=name,
                ))
            except Exception as exc:
                logger.warning("Failed to read image '%s' from zip: %s", name, exc)

    logger.info("Extracted %d images from zip archive.", len(images))
    return images
