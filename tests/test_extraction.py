import json
from pathlib import Path

import pytest
from helpers import fake_converter, make_docx, make_pdf, make_pptx, make_xlsx

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


def test_real_ocr_when_runtime_is_provisioned(tmp_path: Path) -> None:
    """Runs only with GDRAG_TEST_OCR_DIR=<dir from `python -m gdrive_rag.ocr_runtime DIR`>."""
    import os

    from PIL import Image, ImageDraw, ImageFont

    from gdrive_rag.ocr_runtime import OCRRuntimeManager

    root = os.environ.get("GDRAG_TEST_OCR_DIR")
    if not root:
        pytest.skip("set GDRAG_TEST_OCR_DIR to run real OCR")
    image = Image.new("RGB", (1200, 400), "white")
    ImageDraw.Draw(image).text(
        (40, 120), "INVOICE NUMBER 2026", fill="black", font=ImageFont.load_default(64)
    )
    pdf = tmp_path / "scan.pdf"
    image.save(pdf, "PDF", resolution=150)
    runtime = OCRRuntimeManager(Path(root) / "runtime", Path(root) / "models")
    document = Extractor(runtime, ocr_enabled=True).extract(pdf)
    assert "INVOICE" in document.text and document.metadata["pages_needing_ocr"] == [1]
    assert "ocr_unavailable" not in document.metadata


def test_missing_model_files_mean_ocr_unavailable(tmp_path: Path) -> None:
    """Native libraries present but no pinned model files: unavailable, not a failed OCR run."""
    from PIL import Image

    class LibrariesOnly:
        def configure(self) -> bool:
            return True

        def resolved_model_dir(self) -> None:
            return None

    image = tmp_path / "scan.png"
    Image.new("RGB", (200, 100), "white").save(image)
    document = Extractor(LibrariesOnly(), ocr_enabled=True).extract(image)  # type: ignore[arg-type]
    assert document.metadata["ocr_unavailable"] is True
    assert "ocr_failed" not in json.dumps(document.metadata)


def test_runtime_downloads_have_a_timeout(tmp_path: Path, monkeypatch) -> None:
    import io
    import urllib.request

    from gdrive_rag import ocr_runtime

    seen: list[object] = []

    def fake_urlopen(url: str, timeout: object = None) -> io.BytesIO:
        seen.append(timeout)
        return io.BytesIO(b"model bytes")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    target = tmp_path / "file.part"
    ocr_runtime._download("https://example.invalid/x", target)
    assert target.read_bytes() == b"model bytes"
    assert seen == [ocr_runtime.DOWNLOAD_TIMEOUT_SECONDS]
    manager = ocr_runtime.OCRRuntimeManager(tmp_path / "rt", tmp_path / "models")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        manager.fetch_models()  # goes through _download, so through the timeout
    assert len(seen) == 2 and seen[-1] == ocr_runtime.DOWNLOAD_TIMEOUT_SECONDS
