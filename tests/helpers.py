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
from gdrive_rag.drive import (
    Download,
    DriveFolder,
    DriveItem,
    DriveItemError,
    DriveRoot,
    DriveSourceError,
    Listing,
    display_segment,
)
from gdrive_rag.extract import Extractor
from gdrive_rag.service import Service

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeDrive:
    """In-memory Drive: one folder tree (ID -> name, parent), files per root, file bytes.

    A file is put under a root and, optionally, a subfolder (``parent``). A root lists every
    file whose parent chain passes through it, so a subfolder added as a second source sees the
    same files. Paths follow the folder tree unless ``path`` is given. Errors can be injected.
    """

    def __init__(self) -> None:
        self.roots: dict[str, DriveRoot] = {}
        self.folders: dict[str, tuple[str, str]] = {}  # id -> (name, parent id)
        self.files: dict[str, list[DriveItem]] = {}
        self.content: dict[str, bytes] = {}
        self.root_errors: dict[str, Exception] = {}
        self.incomplete: set[str] = set()
        self.downloads: list[str] = []
        self.download_errors: dict[str, Exception] = {}
        self.explicit_paths: set[str] = set()

    def add_root(self, root_id: str, name: str, kind: str = "folder") -> None:
        self.roots[root_id] = DriveRoot(root_id, name, kind)
        self.files.setdefault(root_id, [])
        if kind != "file":
            self.folders[root_id] = (name, self.folders.get(root_id, ("", ""))[1])

    def add_folder(self, folder_id: str, name: str, parent: str) -> None:
        """Add, rename (same parent) or move (new parent) a folder."""
        self.folders[folder_id] = (name, parent)

    def put(
        self,
        root_id: str,
        file_id: str,
        name: str,
        data: bytes,
        mime: str = "text/plain",
        version: str = "1",
        path: str | None = None,
        parent: str | None = None,
    ) -> DriveItem:
        item = DriveItem(
            file_id,
            name,
            mime,
            path or name,
            f"2026-01-0{version}T00:00:00Z",
            hashlib.md5(data).hexdigest(),
            len(data),
            f"https://drive.google.com/file/d/{file_id}/view",
            parent or root_id,
        )
        if path:
            self.explicit_paths.add(file_id)
        else:
            self.explicit_paths.discard(file_id)
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

    def _chain(self, folder_id: str) -> list[str]:
        chain: list[str] = []
        while folder_id in self.folders and folder_id not in chain:
            chain.append(folder_id)
            folder_id = self.folders[folder_id][1]
        return chain

    def list_files(self, root: DriveRoot) -> Listing:
        self.resolve(root.id)
        if root.kind == "file":
            return Listing(list(self.files.get(root.id, [])))
        inside = {f for f in self.folders if root.id in self._chain(f)}
        folders = [DriveFolder(root.id, root.name, "")] + [
            DriveFolder(f, self.folders[f][0], self.folders[f][1])
            for f in sorted(inside - {root.id})
        ]
        items: dict[str, DriveItem] = {}
        for listed in self.files.values():
            for item in listed:
                if item.parent_id not in inside or item.id in items:
                    continue
                if item.id not in self.explicit_paths:
                    chain = self._chain(item.parent_id)
                    names = [self.folders[f][0] for f in chain[: chain.index(root.id)]]
                    path = "/".join(display_segment(n) for n in [*reversed(names), item.name])
                    item = replace(item, path=path)
                items[item.id] = item
        found = list(items.values())
        if root.id in self.incomplete:
            return Listing(
                found[:1], complete=False, errors=["folder x: HTTP 404"], folders=folders
            )
        return Listing(found, folders=folders)

    def download(self, item: DriveItem) -> Download:
        self.downloads.append(item.id)
        if item.id in self.download_errors:
            raise self.download_errors[item.id]
        return Download(self.content[item.id], item.suffix)


class FakeEmbeddings:
    """Deterministic bag-of-words vectors; records every query text it receives."""

    def __init__(self, model: str = "fake-model", dimensions: int = 64):
        self.model = model
        self.dimensions = dimensions
        self.queries: list[str] = []
        self.document_calls = 0
        self.embedded: list[str] = []
        self.fail = False
        self.reject_marker = ""  # texts containing it are refused like an HTTP 400

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
        from gdrive_rag.embeddings import EmbeddingError

        if self.fail:
            raise EmbeddingError("embedding request failed (HTTP 503)")
        if self.reject_marker and any(self.reject_marker in text for text in texts):
            raise EmbeddingError("embedding request failed (HTTP 400)", rejected=True)
        self.document_calls += 1
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        body = text.split("\nQuery:", 1)[-1]
        return self._vector(body)


class FakeOCR:
    """Stands in for Tesseract: returns ``text`` for every page and records the PNGs it got."""

    def __init__(self, text: str = "Hợp đồng quét", name: str = "tesseract-5"):
        self.text = text
        self.name = name
        self.ready = True
        self.images: list[bytes] = []

    def available(self) -> bool:
        return self.ready

    def recognize(self, png: bytes) -> str:
        self.images.append(png)
        return self.text


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
    ocr: FakeOCR | None = None,
    **settings: Any,
) -> Service:
    base = replace(
        Settings(data_dir=tmp, ocr_enabled=ocr is not None, google_auth="service_account")
    )
    configured = replace(base, **settings)
    backend = drive or FakeDrive()

    def no_backend(_: Settings) -> FakeDrive:
        raise DriveSourceError("Google access is not configured; run: gdrive-rag setup")

    return Service(
        lambda: configured,
        backend_factory=(lambda _: backend) if drive is not None else no_backend,
        embedding_factory=lambda _: embeddings,
        extractor_factory=lambda s: Extractor(
            ocr, ocr_enabled=ocr is not None, converter=converter, work_dir=s.work_dir
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


def make_scanned_pdf(text_page: str | None = "Native text page") -> bytes:
    """A PDF whose last page is only an image (no text layer); optionally a text page first."""
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    picture = Image.new("RGB", (800, 300), "white")
    ImageDraw.Draw(picture).rectangle((40, 40, 760, 260), outline="black", width=6)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    if text_page:
        for row in range(30):  # a full text layer, so this page is not routed to OCR
            pdf.drawString(72, 750 - row * 22, f"{text_page} line {row}: rent is due on day 5.")
        pdf.showPage()
    pdf.drawImage(ImageReader(picture), 72, 400, width=450, height=170)
    pdf.showPage()
    pdf.save()
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
