"""Getting readable text out of whatever was uploaded.

Handles the four things people actually have to hand: a screenshot, a PDF
attachment, a saved email, and a block of pasted text. PDFs are tried as text
first and only rasterised for OCR when they turn out to be scans, because an
embedded text layer is always better than reading pixels.
"""

from __future__ import annotations

import email
import email.policy
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_config
from .ocr import OCRUnavailable, run_ocr

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
TEXT_SUFFIXES = {".txt", ".md", ".text", ".json", ".ics"}
EMAIL_SUFFIXES = {".eml", ".mbox"}

#: Below this, an extracted PDF text layer is treated as absent (a scan).
MIN_PDF_TEXT_CHARS = 120


@dataclass
class Ingested:
    """Everything one uploaded file yielded."""

    text: str
    source_file: str
    #: Mean OCR confidence 0..1, or None when the text came from a real text layer.
    ocr_confidence: float | None = None
    #: Paths to page images, kept for barcode scanning.
    image_paths: list[Path] = field(default_factory=list)
    #: Human-readable account of how the text was obtained.
    method: str = "text"
    doubtful_words: list[str] = field(default_factory=list)
    #: Temporary directory to clean up, when one was created.
    tempdir: tempfile.TemporaryDirectory | None = None

    def cleanup(self) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()
            self.tempdir = None


def ingest(path: Path, original_name: str | None = None) -> Ingested:
    """Extract text (and page images) from an uploaded file."""
    name = original_name or path.name
    suffix = Path(name).suffix.lower()

    if suffix == ".pdf":
        return _ingest_pdf(path, name)
    if suffix in IMAGE_SUFFIXES:
        return _ingest_image(path, name)
    if suffix in EMAIL_SUFFIXES:
        return _ingest_email(path, name)
    if suffix in TEXT_SUFFIXES or suffix == "":
        return _ingest_text(path, name)

    # Unknown extension: try text, fall back to treating it as an image.
    try:
        return _ingest_text(path, name)
    except UnicodeDecodeError:
        return _ingest_image(path, name)


def ingest_text(text: str, source_file: str = "-") -> Ingested:
    """Wrap a pasted snippet in the same envelope as an uploaded file."""
    return Ingested(text=text, source_file=source_file, method="pasted text")


def _ingest_text(path: Path, name: str) -> Ingested:
    return Ingested(
        text=path.read_text(encoding="utf-8", errors="replace"),
        source_file=name,
        method="text file",
    )


def _ingest_email(path: Path, name: str) -> Ingested:
    message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    parts: list[str] = []
    subject = message.get("Subject")
    if subject:
        parts.append(f"Subject: {subject}")

    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = _strip_html(content)
        parts.append(content)

    return Ingested(text="\n\n".join(parts), source_file=name, method="email")


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _ingest_pdf(path: Path, name: str) -> Ingested:
    cfg = get_config()
    text = ""

    if shutil.which(cfg.pdftotext_bin):
        proc = subprocess.run(
            [cfg.pdftotext_bin, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            text = proc.stdout

    images = _rasterise_pdf(path)

    if len(text.strip()) >= MIN_PDF_TEXT_CHARS:
        return Ingested(
            text=text,
            source_file=name,
            method="PDF text layer",
            image_paths=[p for p, _ in images],
            tempdir=images[0][1] if images else None,
        )

    # A scan: fall back to OCR over the rendered pages.
    if not images:
        return Ingested(text=text, source_file=name, method="PDF (no text, no rasteriser)")

    pages, tempdir = [p for p, _ in images], images[0][1]
    ocr_text, confidence, doubtful = _ocr_pages(pages)
    return Ingested(
        text=ocr_text or text,
        source_file=name,
        ocr_confidence=confidence,
        image_paths=pages,
        method="PDF scan (OCR)",
        doubtful_words=doubtful,
        tempdir=tempdir,
    )


def _rasterise_pdf(path: Path) -> list[tuple[Path, tempfile.TemporaryDirectory]]:
    """Render PDF pages to PNGs at a resolution that OCRs and scans well."""
    cfg = get_config()
    if not shutil.which(cfg.pdftoppm_bin):
        return []
    tempdir = tempfile.TemporaryDirectory(prefix="wayfare-pdf-")
    prefix = Path(tempdir.name) / "page"
    proc = subprocess.run(
        [cfg.pdftoppm_bin, "-r", "300", "-png", str(path), str(prefix)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        tempdir.cleanup()
        return []
    return [(p, tempdir) for p in sorted(Path(tempdir.name).glob("page*.png"))]


def _ingest_image(path: Path, name: str) -> Ingested:
    text, confidence, doubtful = _ocr_pages([path])
    return Ingested(
        text=text,
        source_file=name,
        ocr_confidence=confidence,
        image_paths=[path],
        method="image (OCR)",
        doubtful_words=doubtful,
    )


def _ocr_pages(paths: list[Path]) -> tuple[str, float | None, list[str]]:
    chunks: list[str] = []
    confidences: list[float] = []
    doubtful: list[str] = []
    for page in paths:
        try:
            result = run_ocr(page)
        except OCRUnavailable:
            raise
        chunks.append(result.text)
        if result.confidence is not None:
            confidences.append(result.confidence)
        doubtful.extend(result.doubtful_words)
    mean = sum(confidences) / len(confidences) if confidences else None
    return "\n\n".join(chunks), mean, doubtful[:40]
