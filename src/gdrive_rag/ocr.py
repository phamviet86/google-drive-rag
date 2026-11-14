"""Local OCR with the Tesseract 5 command-line program (Apache-2.0), run as a subprocess.

Scanned PDF pages are rendered with pypdfium2 (Apache-2.0 / BSD-3-Clause) and images are passed
in directly, as PNG on stdin. Languages default to ``vie+eng`` (``GDRAG_OCR_LANGS``). A missing
binary or missing language data makes OCR unavailable; it never raises a service error.

``python -m gdrive_rag.ocr`` runs a real OCR pass on a generated Vietnamese line and exits non-zero
when key words are missing; the Docker build runs it.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from .config import DEFAULT_OCR_LANGS

ENGINE = "tesseract-5"  # stored with each OCR'd document; a change re-extracts those documents
PAGE_TIMEOUT_SECONDS = 120
RENDER_DPI = 300
MAX_SIDE = 6000  # pixels; larger renders are scaled down to bound memory and time
SELF_TEST_TEXT = ("HỢP ĐỒNG THUÊ MẶT BẰNG", "Số hợp đồng 2026/HD-07, giá thuê 15 triệu đồng")
SELF_TEST_WORDS = ("HỢP ĐỒNG", "triệu đồng")
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


class OcrFailed(RuntimeError):
    """OCR of one page failed (non-zero exit, timeout, unreadable output)."""


class OcrEngine(Protocol):
    name: str

    def available(self) -> bool: ...

    def recognize(self, png: bytes) -> str: ...


class TesseractOCR:
    """``tesseract stdin stdout -l <langs>``; availability is checked once per instance."""

    name = ENGINE

    def __init__(
        self,
        langs: str = DEFAULT_OCR_LANGS,
        binary: str | None = None,
        timeout: float = PAGE_TIMEOUT_SECONDS,
    ):
        self.langs = langs
        self.binary = binary or shutil.which("tesseract")
        self.timeout = timeout
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is None:
            self._available = self._check()
        return self._available

    def _check(self) -> bool:
        if not self.binary:
            return False
        try:
            listed = subprocess.run(
                [self.binary, "--list-langs"],
                capture_output=True,
                timeout=30,
                check=True,
                env=_environment(),
            )
        except (subprocess.SubprocessError, OSError):
            return False
        installed = {line.strip() for line in listed.stdout.decode("utf-8", "replace").split()}
        return all(lang in installed for lang in self.langs.split("+"))

    def recognize(self, png: bytes) -> str:
        assert self.binary is not None
        try:
            result = subprocess.run(
                [self.binary, "stdin", "stdout", "-l", self.langs, "--psm", "3"],
                input=png,
                capture_output=True,
                timeout=self.timeout,
                env=_environment(),
            )
        except subprocess.TimeoutExpired:
            raise OcrFailed("timeout") from None
        except OSError as exc:
            raise OcrFailed(type(exc).__name__) from None
        if result.returncode != 0:
            raise OcrFailed(f"exit {result.returncode}")
        return clean_text(result.stdout.decode("utf-8", "replace"))


def _environment() -> dict[str, str]:
    # One thread per page: OpenMP inside Tesseract slows down small pages on shared CPUs.
    return {**os.environ, "OMP_THREAD_LIMIT": "1"}


def clean_text(text: str) -> str:
    """NFC, trimmed lines, at most one blank line in a row, no form feeds."""
    lines = [
        line.rstrip() for line in unicodedata.normalize("NFC", text).replace("\f", "\n").split("\n")
    ]
    output: list[str] = []
    for line in lines:
        if line or (output and output[-1]):
            output.append(line)
    return "\n".join(output).strip()


def to_png(image: Any) -> bytes:
    """Grayscale PNG of a PIL image, scaled so its longest side is at most ``MAX_SIDE``."""
    image = image.convert("L")
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def render_pdf_pages(
    path: Path, pages: list[int], dpi: int = RENDER_DPI
) -> Iterator[tuple[int, bytes | None]]:
    """Yield ``(page, grayscale PNG)`` for 1-based ``pages``, one page at a time so only one
    bitmap is in memory; ``None`` when that page could not be rendered. The scale is capped
    before rendering, so a poster-sized page never allocates more than ``MAX_SIDE`` pixels."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        for number in pages:
            if not 1 <= number <= len(document):
                yield number, None
                continue
            page = document[number - 1]
            try:
                width, height = page.get_size()
                scale = min(dpi / 72, MAX_SIDE / max(width, height, 1))
                png = to_png(page.render(scale=scale, grayscale=True).to_pil())
            except Exception:
                png = None
            finally:
                page.close()
            yield number, png
    finally:
        document.close()


def self_test(engine: OcrEngine | None = None, font: str | None = None) -> str:
    """OCR a generated Vietnamese page; raise RuntimeError unless the key words come back."""
    from PIL import Image, ImageDraw, ImageFont

    engine = engine or TesseractOCR()
    if not engine.available():
        raise RuntimeError("tesseract or its language data is missing")
    font_path = font or next((f for f in FONT_CANDIDATES if Path(f).is_file()), None)
    if font_path is None:
        raise RuntimeError("no font with Vietnamese letters found for the OCR self-test")
    face = ImageFont.truetype(font_path, 44)
    image = Image.new("L", (1800, 320), 255)
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(SELF_TEST_TEXT):
        draw.text((40, 50 + row * 120), line, font=face, fill=0)
    text = engine.recognize(to_png(image))
    missing = [word for word in SELF_TEST_WORDS if word not in text]
    if missing:
        raise RuntimeError(f"OCR self-test lost {missing}; got: {text!r}")
    return text


def main() -> int:
    try:
        text = self_test()
    except RuntimeError as exc:
        print(f"OCR self-test failed: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
