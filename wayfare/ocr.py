"""Local, deterministic text extraction.

Free models are asked to *interpret* text, never to *read* it. Reading happens
here, with tesseract, which gives the same answer every time and reports a
per-word confidence we can act on. That confidence becomes a ceiling on the
whole record: nothing downstream is allowed to be more certain than the pixels
justify.
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import get_config


@dataclass
class OCRResult:
    text: str
    #: Mean word confidence, 0..1, or None if tesseract reported nothing usable.
    confidence: float | None
    #: Words tesseract read with low confidence, for the review screen.
    doubtful_words: list[str]


class OCRUnavailable(RuntimeError):
    """Raised when tesseract is not installed."""


def available() -> bool:
    return shutil.which(get_config().tesseract_bin) is not None


def run_ocr(image_path: Path, languages: str = "eng") -> OCRResult:
    """OCR an image, returning text plus a calibrated confidence."""
    cfg = get_config()
    if not available():
        raise OCRUnavailable(
            f"'{cfg.tesseract_bin}' not found. Install tesseract-ocr, or set WAYFARE_TESSERACT."
        )

    proc = subprocess.run(
        [cfg.tesseract_bin, str(image_path), "stdout", "-l", languages, "tsv"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OCRUnavailable(f"tesseract failed: {proc.stderr.strip()[:300]}")

    return _parse_tsv(proc.stdout)


def _parse_tsv(tsv_text: str) -> OCRResult:
    """Rebuild the page text from tesseract's TSV, keeping line structure.

    Layout matters for booking documents — a departure time sits next to its
    airport code on the same line — so the naive `tesseract ... stdout` text
    output is not enough on its own.
    """
    lines: dict[tuple[int, int, int, int], list[str]] = {}
    confidences: list[float] = []
    doubtful: list[str] = []

    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf", "-1"))
        except ValueError:
            conf = -1.0
        if conf < 0:
            continue

        key = (
            int(row.get("page_num", 1)),
            int(row.get("block_num", 0)),
            int(row.get("par_num", 0)),
            int(row.get("line_num", 0)),
        )
        lines.setdefault(key, []).append(text)
        confidences.append(conf / 100.0)
        if conf < 60:
            doubtful.append(text)

    page = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    mean = sum(confidences) / len(confidences) if confidences else None
    return OCRResult(text=page, confidence=mean, doubtful_words=doubtful[:40])
