"""Fakes for Drive and embeddings, plus small generated document fixtures."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from gdrive_rag.config import Settings
from gdrive_rag.drive import DriveItem, DriveItemError, DriveRoot, DriveSourceError, Listing
from gdrive_rag.extract import Extractor
from gdrive_rag.service import Service

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeDrive:
    """In-memory Drive: roots, files per root, and file bytes. Errors can be injected."""

    def __init__(self) -> None:
        self.roots: dict[str, DriveRoot] = {}
        self.files: dict[str, list[DriveItem]] = {}
        self.content: dict[str, bytes] = {}
        self.root_errors: dict[str, Exception] = {}
        self.incomplete: set[str] = set()
        self.downloads: list[str] = []

    def add_root(self, root_id: str, name: str, kind: str = "folder") -> None:
        self.roots[root_id] = DriveRoot(root_id, name, kind)
        self.files.setdefault(root_id, [])

    def put(
        self,
        root_id: str,
        file_id: str,
        name: str,
        data: bytes,
        mime: str = "text/plain",
        version: str = "1",
    ) -> DriveItem:
        item = DriveItem(
            file_id,
            name,
            mime,
            name,
            f"2026-01-0{version}T00:00:00Z",
            hashlib.md5(data).hexdigest(),
            len(data),
            f"https://drive.google.com/file/d/{file_id}/view",
        )
        self.files[root_id] = [i for i in self.files[root_id] if i.id != file_id] + [item]
        self.content[file_id] = data
        return item

    def delete(self, root_id: str, file_id: str) -> None:
        self.files[root_id] = [i for i in self.files[root_id] if i.id != file_id]

    def resolve(self, root_id: str) -> DriveRoot:
        if root_id in self.root_errors:
            raise self.root_errors[root_id]
        if root_id not in self.roots:
            raise DriveItemError("not found or not shared with this Google account (HTTP 404)")
        return self.roots[root_id]

    def list_files(self, root: DriveRoot) -> Listing:
        self.resolve(root.id)
        items = list(self.files.get(root.id, []))
        if root.id in self.incomplete:
            return Listing(items[:1], complete=False, errors=["folder x: HTTP 404"])
        return Listing(items)

    def download(self, item: DriveItem) -> bytes:
        self.downloads.append(item.id)
        return self.content[item.id]


class FakeEmbeddings:
    """Deterministic bag-of-words vectors; records every query text it receives."""

    def __init__(self, model: str = "fake-model", dimensions: int = 64):
        self.model = model
        self.dimensions = dimensions
        self.queries: list[str] = []
        self.document_calls = 0
        self.fail = False

    @property
    def identity(self) -> str:
        return json.dumps(["fake", self.model, self.dimensions])

    def _vector(self, text: str) -> list[float]:
        values = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.lower()):
            values[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding endpoint down")
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        body = text.split("\nQuery:", 1)[-1]
        return self._vector(body)


def fake_converter(source: Path, target: str, outdir: Path) -> Path:
    """Stands in for LibreOffice: the 'legacy' file holds plain text; build a real docx/pptx."""
    text = source.read_bytes().decode("utf-8")
    output = outdir / f"{source.stem}.{target}"
    output.write_bytes(make_docx([text]) if target == "docx" else make_pptx(text))
    return output


def make_service(
    tmp: Path,
    drive: FakeDrive | None = None,
    embeddings: FakeEmbeddings | None = None,
    converter: Callable[[Path, str, Path], Path] = fake_converter,
    **settings: Any,
) -> Service:
    base = replace(Settings(data_dir=tmp, ocr_enabled=False, google_auth="service_account"))
    configured = replace(base, **settings)
    backend = drive or FakeDrive()

    def no_backend(_: Settings) -> FakeDrive:
        raise DriveSourceError("Google access is not configured; run: gdrive-rag setup")

    return Service(
        lambda: configured,
        backend_factory=(lambda _: backend) if drive is not None else no_backend,
        embedding_factory=lambda _: embeddings,
        extractor_factory=lambda s: Extractor(
            None, ocr_enabled=False, converter=converter, work_dir=s.work_dir
        ),
    )


def make_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading("Tiêu đề chính", level=1)
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx(rows: list[list[Any]], sheet: str = "Ngân sách") -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_pptx(title: str = "Kế hoạch quý") -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    assert slide.shapes.title is not None
    slide.shapes.title.text = title
    group = slide.shapes.add_group_shape()
    box = group.shapes.add_textbox(Inches(1), Inches(2), Inches(3), Inches(1))
    box.text_frame.text = "Văn bản trong nhóm"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    table.cell(0, 0).text = "Hạng mục"
    table.cell(0, 1).text = "Chi phí"
    table.cell(1, 0).text = "Máy chủ"
    table.cell(1, 1).text = "500"
    notes = slide.notes_slide.notes_text_frame
    assert notes is not None
    notes.text = "Nhắc khách hàng về hạn chót"
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def make_pdf(lines: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for page in lines:
        pdf.drawString(72, 720, page)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class EmbeddingStub:
    """Local OpenAI-compatible ``/embeddings`` endpoint returning 4096-dim bag-of-words vectors.

    Records every request body and Authorization header so tests can check the query prefix,
    the absence of a document prefix and that no ``dimensions`` field is sent.
    """

    def __init__(self, dimensions: int = 4096):
        import http.server
        import threading

        self.requests: list[dict[str, Any]] = []
        self.auth: list[str] = []
        self.fake = FakeEmbeddings("stub", 256)
        self.dimensions = dimensions
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                stub.auth.append(self.headers.get("Authorization", ""))
                texts = body["input"]
                # Matryoshka-like: the meaning sits in the first dims, the tail is noise padding.
                vectors = [
                    stub.fake._vector(t.split("\nQuery:", 1)[-1]) + [0.5] * (stub.dimensions - 256)
                    for t in texts
                ]
                payload = json.dumps(
                    {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
