"""Turn a downloaded file into text with locators (page, slide, sheet/row, ...).

Adapted from local-rag-mcp (Apache-2.0): the PDF/OCR, DOCX, XLSX and text extractors are copied;
PPTX now includes tables, grouped shapes and speaker notes; legacy XLS/XLSB are read with
python-calamine; DOC/PPT/RTF are converted with headless LibreOffice first. A missing optional
tool is always a per-file error or flag, never a service error.
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import formats
from .builder import Builder
from .extraction_errors import ExtractionError
from .models import ExtractedDocument
from .ocr_runtime import OCRRuntimeManager
from .xlsx_compat import (
    STYLES_PART,
    color_failure,
    copy_archive,
    inspect_archive,
    repair_styles,
    stylesheet_failure,
)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
LEGACY_CONVERSIONS = {".doc": "docx", ".rtf": "docx", ".ppt": "pptx"}
SUPPORTED_SUFFIXES = frozenset(
    {
        ".pdf", ".docx", ".xlsx", ".pptx", ".xls", ".xlsb", ".doc", ".ppt", ".rtf",
        ".md", ".markdown", ".txt", ".json", ".csv", ".html", ".htm", ".epub",
        ".odt", ".ods", ".odp", *IMAGE_SUFFIXES,
    }
)  # fmt: skip
IMAGE_MAX_SIDE = 4096
IMAGE_PDF_DPI = 200
CONVERT_TIMEOUT_SECONDS = 300

# (source file, target extension, output directory) -> converted file
Converter = Callable[[Path, str, Path], Path]


def libreoffice_convert(source: Path, target: str, outdir: Path) -> Path:
    """Convert a legacy Office file with headless LibreOffice; raise ExtractionError on failure."""
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if binary is None:
        raise ExtractionError("converter_missing")
    profile = outdir / "lo-profile"
    command = [
        binary,
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--headless",
        "--norestore",
        "--convert-to",
        target,
        "--outdir",
        str(outdir),
        str(source),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CONVERT_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ExtractionError("converter_failed") from exc
    converted = outdir / f"{source.stem}.{target}"
    if not converted.is_file():
        raise ExtractionError("converter_failed")
    return converted


class Extractor:
    def __init__(
        self,
        ocr_runtime: OCRRuntimeManager | None,
        *,
        ocr_enabled: bool = True,
        converter: Converter = libreoffice_convert,
        work_dir: Path | None = None,
    ):
        self.ocr_runtime = ocr_runtime
        self.ocr_enabled = ocr_enabled
        self.converter = converter
        self.work_dir = work_dir

    def extract(self, path: Path) -> ExtractedDocument:
        suffix = path.suffix.lower()
        if suffix in {".htm", ".html"}:
            return formats.extract_html(path)
        if suffix == ".csv":
            return formats.extract_csv(path)
        if suffix == ".json":
            return formats.extract_json(path)
        if suffix == ".epub":
            return formats.extract_epub(path)
        if suffix in {".odt", ".ods", ".odp"}:
            return formats.extract_odf(path)
        if suffix in {".txt", ".md", ".markdown"}:
            return _text(path, markdown=suffix != ".txt")
        if suffix == ".docx":
            return _docx(path)
        if suffix == ".xlsx":
            return _xlsx(path, self.work_dir)
        if suffix in {".xls", ".xlsb"}:
            return _calamine(path)
        if suffix == ".pptx":
            return _pptx(path)
        if suffix in LEGACY_CONVERSIONS:
            return self._legacy(path, LEGACY_CONVERSIONS[suffix])
        if suffix == ".pdf":
            return self._pdf(path)
        if suffix in IMAGE_SUFFIXES:
            return self._image(path)
        raise ExtractionError("unsupported_type")

    def _legacy(self, path: Path, target: str) -> ExtractedDocument:
        if self.work_dir is not None:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="convert-", dir=self.work_dir) as temporary:
            outdir = Path(temporary)
            converted = self.converter(path, target, outdir)
            document = _docx(converted) if target == "docx" else _pptx(converted)
        metadata = {**document.metadata, "converted_with": "libreoffice"}
        media = mimetypes.guess_type(path.name)[0] or document.media_type
        return ExtractedDocument(document.text, document.spans, media, metadata)

    def _ocr_ready(self) -> bool:
        """Native libraries and the pinned model files are present (OCR runs offline)."""
        runtime = self.ocr_runtime
        return bool(
            self.ocr_enabled
            and runtime
            and runtime.configure()
            and runtime.resolved_model_dir() is not None
        )

    def _ocr(
        self, run: Callable[[str | None], Any], routed: list[int]
    ) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
        """Run local OCR for ``routed`` 1-based pages; return page texts and problems."""
        pages: dict[int, dict[str, Any]] = {}
        problems: list[dict[str, Any]] = []
        runtime = self.ocr_runtime
        if runtime is None or not self._ocr_ready():
            problems.extend({"page": page, "reason": "ocr_unavailable"} for page in routed)
            return pages, problems
        try:
            result = run(str(runtime.resolved_model_dir()))
            for ocr_page in result.pages:
                number = int(ocr_page.page_number)
                markdown = (ocr_page.markdown or "").strip()
                if markdown:
                    pages[number] = {"text": markdown, "source": "ocr"}
                else:
                    problems.append({"page": number, "reason": "ocr_blank"})
        except Exception as exc:
            problems.extend(
                {"page": page, "reason": "ocr_failed", "error": type(exc).__name__}
                for page in routed
            )
        return pages, problems

    def _image(self, path: Path) -> ExtractedDocument:
        """OCR a photo or scan as one page: wrap it in a PDF and reuse the PDF OCR path."""
        import io

        import pdf_inspector
        from PIL import Image, ImageOps

        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
        except Exception as exc:
            raise ExtractionError("image_unreadable") from exc
        if max(image.size) > IMAGE_MAX_SIDE:
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buffer = io.BytesIO()
        image.save(buffer, "PDF", resolution=IMAGE_PDF_DPI)
        pdf = buffer.getvalue()
        pages, problems = self._ocr(
            lambda model: pdf_inspector.process_pdf_with_ocr_bytes(
                pdf, page_numbers=[1], model_directory=model, offline=True
            ),
            [1],
        )
        builder = Builder()
        for number, page in sorted(pages.items()):
            builder.add(page["text"], "pdf_page", f"page:{number}", {"source": page["source"]})
        return ExtractedDocument(
            builder.text, builder.spans, _media(path), _ocr_metadata([1], problems)
        )

    def _pdf(self, path: Path) -> ExtractedDocument:
        import pdf_inspector

        try:
            classification = pdf_inspector.process_pdf(str(path))
            native = pdf_inspector.extract_pages_markdown(str(path))
        except Exception as exc:
            raise ExtractionError("format_parse_failed") from exc
        routed = [int(page) for page in classification.pages_needing_ocr]
        pages: dict[int, dict[str, Any]] = {}
        for native_page in native.pages:
            number = int(native_page.page) + 1
            markdown = (native_page.markdown or "").strip()
            if markdown:
                pages[number] = {"text": markdown, "source": "native"}
        problems: list[dict[str, Any]] = []
        if routed:
            ocr_pages, problems = self._ocr(
                lambda model: pdf_inspector.process_pdf_with_ocr(
                    str(path), page_numbers=routed, model_directory=model, offline=True
                ),
                routed,
            )
            pages.update(ocr_pages)
        builder = Builder()
        for number, page in sorted(pages.items()):
            builder.add(page["text"], "pdf_page", f"page:{number}", {"source": page["source"]})
        metadata = {"page_count": int(classification.page_count), **_ocr_metadata(routed, problems)}
        return ExtractedDocument(builder.text, builder.spans, "application/pdf", metadata)


def _ocr_metadata(routed: list[int], problems: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"pages_needing_ocr": routed}
    if problems:
        metadata["ocr_problems"] = problems
    if any(problem["reason"] == "ocr_unavailable" for problem in problems):
        metadata["ocr_unavailable"] = True
    return metadata


def _text(path: Path, markdown: bool) -> ExtractedDocument:
    text, _ = formats._decode(formats.read_limited(path))
    builder = Builder()
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            builder.add(line, "markdown_line" if markdown else "text_line", f"line:{number}")
    return ExtractedDocument(
        builder.text, builder.spans, "text/markdown" if markdown else "text/plain"
    )


def _docx(path: Path) -> ExtractedDocument:
    from docx import Document
    from docx.text.paragraph import Paragraph

    try:
        document = Document(str(path))
    except Exception as exc:
        raise ExtractionError("format_parse_failed") from exc
    builder = Builder()
    headings: dict[int, str] = {}
    paragraph_index = table_index = 0
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            paragraph_index += 1
            style = block.style.name if block.style is not None else ""
            match = re.fullmatch(r"Heading (\d+)", style or "", re.IGNORECASE)
            text = block.text
            if match:
                level = int(match[1])
                headings = {key: value for key, value in headings.items() if key < level}
                headings[level] = text.strip()
                text = "#" * min(level, 6) + " " + text.strip() if text.strip() else text
            builder.add(
                text,
                "docx_paragraph",
                f"paragraph:{paragraph_index}",
                {"heading_path": list(headings.values())},
            )
        else:
            table_index += 1
            for row_index, row in enumerate(block.rows, 1):
                cells = _dedupe_merged(cell.text.strip() for cell in row.cells)
                builder.add(
                    " | ".join(cells),
                    "docx_table_row",
                    f"table:{table_index}/row:{row_index}",
                    {"heading_path": list(headings.values())},
                )
    return ExtractedDocument(builder.text, builder.spans, _media(path))


def _dedupe_merged(values: Iterator[str]) -> list[str]:
    """Merged cells repeat their text in python-docx; keep one copy of consecutive repeats."""
    output: list[str] = []
    for value in values:
        if value and (not output or output[-1] != value):
            output.append(value)
    return output


def _xlsx(path: Path, cache_dir: Path | None = None) -> ExtractedDocument:
    from openpyxl import load_workbook

    builder = Builder()
    repairs: list[dict[str, Any]] = []
    try:
        styles = inspect_archive(path)
        with ExitStack() as stack:
            try:
                workbook = load_workbook(path, read_only=True, data_only=True)
            except Exception as original:
                if not color_failure(original) or styles is None:
                    raise
                try:
                    repaired, count = repair_styles(styles)
                    if not count:
                        raise original
                    if cache_dir is not None:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                    temporary = stack.enter_context(
                        tempfile.TemporaryDirectory(prefix="xlsx-", dir=cache_dir)
                    )
                    copy = Path(temporary) / "workbook.xlsx"
                    copy_archive(path, copy, repaired)
                    workbook = load_workbook(copy, read_only=True, data_only=True)
                except ExtractionError as exc:
                    raise exc from original
                except Exception as exc:
                    raise ExtractionError("xlsx_stylesheet_unreadable") from exc
                repairs = [{"kind": "xlsx_rgb_hash_prefix", "part": STYLES_PART, "count": count}]
            stack.callback(workbook.close)
            for sheet in workbook.worksheets:
                for row_number, row in enumerate(sheet.iter_rows(), 1):
                    cells = [
                        _cell_text(cell.value, getattr(cell, "number_format", None))
                        for cell in row
                        if cell.value is not None and str(cell.value).strip()
                    ]
                    if cells:
                        number = getattr(row[0], "row", None) or row_number
                        builder.add(
                            " | ".join(cells), "sheet_row", f"sheet:{sheet.title}/row:{number}"
                        )
            metadata: dict[str, Any] = {"sheets": workbook.sheetnames}
            if repairs:
                metadata["compatibility_repairs"] = repairs
            return ExtractedDocument(builder.text, builder.spans, _media(path), metadata)
    except ExtractionError:
        raise
    except Exception as exc:
        code = (
            "xlsx_stylesheet_unreadable"
            if repairs or stylesheet_failure(exc)
            else "xlsx_parse_failed"
        )
        raise ExtractionError(code) from exc


def _calamine(path: Path) -> ExtractedDocument:
    """Legacy .xls and binary .xlsb workbooks through python-calamine (MIT)."""
    from python_calamine import CalamineWorkbook

    builder = Builder()
    try:
        workbook = CalamineWorkbook.from_path(str(path))
        try:
            names = list(workbook.sheet_names)
            for name in names:
                rows = workbook.get_sheet_by_name(name).to_python(skip_empty_area=False)
                for row_number, row in enumerate(rows, 1):
                    cells = [_cell_text(value, None) for value in row if str(value).strip()]
                    if cells:
                        builder.add(
                            " | ".join(cells), "sheet_row", f"sheet:{name}/row:{row_number}"
                        )
        finally:
            workbook.close()
    except Exception as exc:
        raise ExtractionError("xlsx_parse_failed") from exc
    return ExtractedDocument(builder.text, builder.spans, _media(path), {"sheets": names})


_QUOTED = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')


def _cell_text(value: Any, number_format: str | None) -> str:
    """Render a cell roughly as a spreadsheet displays it, so amounts and dates are searchable."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    pattern = _QUOTED.sub("", (number_format or "General").split(";")[0])
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return _date_text(value.date(), pattern)
        return f"{_date_text(value.date(), pattern)} {_time_text(value.time())}"
    if isinstance(value, dt.date):
        return _date_text(value, pattern)
    if isinstance(value, dt.time):
        return _time_text(value)
    if isinstance(value, int | float):
        return _number_text(value, pattern)
    return str(value)


def _date_text(value: dt.date, pattern: str) -> str:
    if pattern.lower().lstrip().startswith("d"):
        return value.strftime("%d/%m/%Y")
    return value.isoformat()


def _time_text(value: dt.time) -> str:
    return value.strftime("%H:%M:%S" if value.second else "%H:%M")


def _number_text(value: int | float, pattern: str) -> str:
    percent = "%" in pattern
    number = value * 100 if percent else value
    match = re.search(r"\.([0#?]+)", pattern)
    decimals = len(match.group(1)) if match else None
    grouped = bool(re.search(r"[#0],[#0]", pattern))
    if decimals is None:
        if isinstance(number, int) or (
            isinstance(number, float) and number.is_integer() and abs(number) < 1e15
        ):
            text = f"{int(number):,}" if grouped else str(int(number))
        else:
            text = f"{number:,.10g}" if grouped else f"{number:.10g}"
    else:
        text = f"{number:,.{decimals}f}" if grouped else f"{number:.{decimals}f}"
    return f"{text}%" if percent else text


def _pptx(path: Path) -> ExtractedDocument:
    from pptx import Presentation

    try:
        presentation = Presentation(str(path))
    except Exception as exc:
        raise ExtractionError("format_parse_failed") from exc
    builder = Builder()
    count = 0
    for slide_index, slide in enumerate(presentation.slides, 1):
        count += 1
        parts = [text for text in _shape_texts(slide.shapes) if text.strip()]
        builder.add("\n".join(parts), "pptx_slide", f"slide:{slide_index}")
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None and notes.text.strip():
                builder.add(
                    "Ghi chú: " + notes.text.strip(), "pptx_notes", f"slide:{slide_index}/notes"
                )
    return ExtractedDocument(builder.text, builder.spans, _media(path), {"slides": count})


def _shape_texts(shapes: Any) -> Iterator[str]:
    """Text of shapes in reading order, descending into groups and tables."""
    for shape in shapes:
        if getattr(shape, "shape_type", None) is not None and hasattr(shape, "shapes"):
            yield from _shape_texts(shape.shapes)  # group shape
            continue
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                cells = _dedupe_merged(cell.text.strip() for cell in row.cells)
                if cells:
                    yield " | ".join(cells)
            continue
        if getattr(shape, "has_text_frame", False):
            yield shape.text_frame.text.strip()


def _media(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
