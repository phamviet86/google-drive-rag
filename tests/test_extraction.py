import json
from pathlib import Path

import pytest
from helpers import (
    FakeOCR,
    fake_converter,
    make_docx,
    make_pdf,
    make_pptx,
    make_scanned_pdf,
    make_xlsx,
)

from gdrive_rag.chunking import make_chunks
from gdrive_rag.extract import Extractor, _calamine, libreoffice_convert
from gdrive_rag.extraction_errors import ExtractionError


def extract(tmp_path: Path, name: str, data: bytes, **kwargs):
    path = tmp_path / name
    path.write_bytes(data)
    extractor = Extractor(None, ocr_enabled=False, work_dir=tmp_path / "work", **kwargs)
    return extractor.extract(path)


def locators(document):
    return [span.locator for span in document.spans]


def test_docx_paragraphs_headings_and_tables(tmp_path: Path) -> None:
    document = extract(
        tmp_path, "a.docx", make_docx(["Đoạn văn một."], [["Tên", "Tuổi"], ["An", "30"]])
    )
    assert "# Tiêu đề chính" in document.text
    assert "Tên | Tuổi" in document.text and "An | 30" in document.text
    assert "table:1/row:2" in locators(document)


def test_xlsx_rows_keep_sheet_locator(tmp_path: Path) -> None:
    document = extract(tmp_path, "b.xlsx", make_xlsx([["Mục", "Số tiền"], ["Điện", 1200000]]))
    assert "Điện | 1200000" in document.text
    assert locators(document) == ["sheet:Ngân sách/row:1", "sheet:Ngân sách/row:2"]


def test_calamine_reader(tmp_path: Path) -> None:
    # No .xls/.xlsb writer exists in Python; calamine reads xlsx through the same code path.
    path = tmp_path / "c.xlsx"
    path.write_bytes(make_xlsx([["Tên", "Điểm"], ["Bình", 9.5]], sheet="Lớp"))
    document = _calamine(path)
    assert "Bình | 9.5" in document.text
    assert "sheet:Lớp/row:2" in locators(document)


def test_pptx_tables_groups_and_notes(tmp_path: Path) -> None:
    document = extract(tmp_path, "d.pptx", make_pptx())
    assert "Kế hoạch quý" in document.text
    assert "Văn bản trong nhóm" in document.text
    assert "Máy chủ | 500" in document.text
    assert "Nhắc khách hàng về hạn chót" in document.text
    assert locators(document) == ["slide:1", "slide:1/notes"]


def test_pdf_pages(tmp_path: Path) -> None:
    document = extract(tmp_path, "e.pdf", make_pdf(["First page budget", "Second page invoice"]))
    assert "budget" in document.text and "invoice" in document.text
    assert locators(document) == ["page:1", "page:2"]
    assert document.metadata["page_count"] == 2
    chunks = make_chunks(document, 1500, 200)
    assert chunks[0].locator == "page:1-2"


def test_text_formats(tmp_path: Path) -> None:
    assert "# Tiêu đề" in extract(tmp_path, "f.md", "# Tiêu đề\n\nnội dung".encode()).text
    assert extract(tmp_path, "g.txt", "dòng một\ndòng hai".encode()).spans[1].locator == "line:2"
    js = extract(tmp_path, "h.json", json.dumps({"khach": {"ten": "Lan"}}).encode())
    assert "khach.ten: Lan" in js.text
    csv = extract(tmp_path, "i.csv", b"ten,tuoi\nLan,20\n")
    assert "ten: Lan; tuoi: 20" in csv.text
    html = extract(tmp_path, "j.html", b"<h2>Muc</h2><p>Noi dung <b>web</b></p><script>x</script>")
    assert "## Muc" in html.text and "Noi dung web" in html.text and "x" not in html.text.split()


@pytest.mark.parametrize("name", ["k.doc", "l.rtf", "m.ppt"])
def test_legacy_formats_convert_with_libreoffice(tmp_path: Path, name: str) -> None:
    document = extract(tmp_path, name, "Hợp đồng cũ".encode(), converter=fake_converter)
    assert "Hợp đồng cũ" in document.text
    assert document.metadata["converted_with"] == "libreoffice"


def test_missing_libreoffice_is_a_per_file_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ExtractionError) as error:
        extract(tmp_path, "n.doc", b"x", converter=libreoffice_convert)
    assert error.value.code == "converter_missing"


def test_scanned_pdf_without_ocr_is_flagged(tmp_path: Path) -> None:
    from PIL import Image

    image = tmp_path / "scan.png"
    Image.new("RGB", (200, 100), "white").save(image)
    document = Extractor(None, ocr_enabled=True).extract(image)
    assert document.metadata["ocr_unavailable"] is True
    assert document.text == ""


def test_unsupported_type(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError):
        extract(tmp_path, "o.bin", b"x")


def test_mixed_pdf_keeps_text_pages_and_ocrs_scanned_pages(tmp_path: Path) -> None:
    from PIL import Image

    path = tmp_path / "mixed.pdf"
    path.write_bytes(make_scanned_pdf())
    ocr = FakeOCR("Hợp đồng thuê mặt bằng")
    document = Extractor(ocr, ocr_enabled=True).extract(path)
    assert document.metadata["pages_needing_ocr"] == [2]
    assert document.metadata["ocr_engine"] == "tesseract-5"
    assert [(s.locator, s.metadata.get("source")) for s in document.spans] == [
        ("page:1", "native"),
        ("page:2", "ocr"),
    ]
    assert "Native text page" in document.text and "Hợp đồng thuê mặt bằng" in document.text
    assert len(ocr.images) == 1  # only the scanned page is rendered and OCR'd
    import io

    rendered = Image.open(io.BytesIO(ocr.images[0]))
    assert rendered.format == "PNG" and rendered.mode == "L"
    assert abs(rendered.width - 595.28 * 300 / 72) <= 2  # reportlab's A4 at 300 dpi


def test_image_goes_straight_to_ocr(tmp_path: Path) -> None:
    from PIL import Image

    path = tmp_path / "scan.jpg"
    Image.new("RGB", (300, 120), "white").save(path)
    ocr = FakeOCR("Biên bản họp")
    document = Extractor(ocr, ocr_enabled=True).extract(path)
    assert [span.locator for span in document.spans] == ["page:1"]
    assert document.text == "Biên bản họp" and "ocr_unavailable" not in document.metadata


def test_blank_ocr_result_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_scanned_pdf(None))
    document = Extractor(FakeOCR(""), ocr_enabled=True).extract(path)
    assert document.metadata["ocr_problems"] == [{"page": 1, "reason": "ocr_blank"}]


def fake_tesseract(tmp_path: Path, langs: str, body: str = "print('Hợp đồng')") -> str:
    """An executable that answers like ``tesseract``: --list-langs, then OCR from stdin."""
    import sys

    script = tmp_path / "tesseract"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if sys.argv[1:] == ['--list-langs']:\n"
        f"    print('List of available languages in \"/x\" ({len(langs.split())}):')\n"
        f"    print({langs!r}.replace(' ', '\\n'))\n"
        "    sys.exit(0)\n"
        "assert sys.argv[1:3] == ['stdin', 'stdout'] and sys.stdin.buffer.read(8)\n"
        f"{body}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def test_tesseract_subprocess_and_missing_pieces(tmp_path: Path) -> None:
    from gdrive_rag.ocr import TesseractOCR

    image = tmp_path / "scan.png"
    from PIL import Image

    Image.new("RGB", (200, 100), "white").save(image)
    working = TesseractOCR("vie+eng", binary=fake_tesseract(tmp_path, "eng osd vie"))
    document = Extractor(working, ocr_enabled=True).extract(image)
    assert document.text == "Hợp đồng"
    for engine in (
        TesseractOCR("vie+eng", binary=None),  # binary missing
        TesseractOCR("vie+eng", binary=str(tmp_path / "nope")),  # path does not exist
        TesseractOCR("vie+eng", binary=fake_tesseract(tmp_path, "eng osd")),  # no vie data
    ):
        document = Extractor(engine, ocr_enabled=True).extract(image)
        assert document.metadata["ocr_unavailable"] is True and document.text == ""


def test_tesseract_timeout_is_a_page_problem(tmp_path: Path) -> None:
    from gdrive_rag.ocr import TesseractOCR

    path = tmp_path / "scan.pdf"
    path.write_bytes(make_scanned_pdf(None))
    slow = TesseractOCR("vie", binary=fake_tesseract(tmp_path, "vie", "time.sleep(5)"), timeout=0.5)
    document = Extractor(slow, ocr_enabled=True).extract(path)
    assert document.metadata["ocr_problems"] == [
        {"page": 1, "reason": "ocr_failed", "error": "timeout"}
    ]
    assert "ocr_unavailable" not in document.metadata


def test_real_tesseract_keeps_vietnamese_diacritics(tmp_path: Path) -> None:
    """Runs only when `tesseract` with Vietnamese and English data is on PATH (CI installs it)."""
    from PIL import Image, ImageDraw, ImageFont

    from gdrive_rag.ocr import FONT_CANDIDATES, SELF_TEST_TEXT, TesseractOCR, self_test

    engine = TesseractOCR("vie+eng")
    if not engine.available():
        pytest.skip("tesseract with vie+eng data is not installed")
    font = next((f for f in FONT_CANDIDATES if Path(f).is_file()), None)
    if font is None:
        pytest.skip("no font with Vietnamese letters")
    assert "HỢP ĐỒNG" in self_test(engine, font)
    picture = Image.new("RGB", (1800, 320), "white")
    draw = ImageDraw.Draw(picture)
    for row, line in enumerate(SELF_TEST_TEXT):
        draw.text((40, 50 + row * 120), line, font=ImageFont.truetype(font, 44), fill="black")
    path = tmp_path / "scan.pdf"
    picture.save(path, resolution=300)  # an image-only PDF; renders back at the same size
    document = Extractor(engine, ocr_enabled=True).extract(path)
    assert document.metadata["pages_needing_ocr"] == [1]
    assert "HỢP ĐỒNG" in document.text and "triệu đồng" in document.text


def test_pages_are_rendered_and_recognized_one_at_a_time(tmp_path: Path, monkeypatch) -> None:
    import io

    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from gdrive_rag import ocr as ocr_module

    picture = ImageReader(Image.new("RGB", (400, 200), "gray"))
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for _ in range(3):
        pdf.drawImage(picture, 72, 400, width=300, height=150)
        pdf.showPage()
    pdf.setPageSize((5000, 3000))  # a poster-sized page: capped before rendering
    pdf.drawImage(picture, 100, 100, width=4000, height=2000)
    pdf.showPage()
    pdf.save()
    path = tmp_path / "scans.pdf"
    path.write_bytes(buffer.getvalue())
    events: list[str] = []
    real_to_png = ocr_module.to_png
    monkeypatch.setattr(
        ocr_module, "to_png", lambda image: (events.append("render"), real_to_png(image))[1]
    )

    class Recording(FakeOCR):
        def recognize(self, png: bytes) -> str:
            events.append("ocr")
            return super().recognize(png)

    engine = Recording("chữ")
    document = Extractor(engine, ocr_enabled=True).extract(path)
    assert document.metadata["pages_needing_ocr"] == [1, 2, 3, 4]
    assert events == ["render", "ocr"] * 4  # never more than one page bitmap at a time
    poster = Image.open(io.BytesIO(engine.images[-1]))
    assert max(poster.size) <= ocr_module.MAX_SIDE
