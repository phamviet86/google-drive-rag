from pathlib import Path

from helpers import EmbeddingStub, FakeDrive, FakeEmbeddings, make_service

from gdrive_rag.config import DEFAULT_QUERY_INSTRUCTION
from gdrive_rag.embeddings import query_text, truncate_normalize

ROOT = "folderAAAAAAAAAAAA"


def indexed(tmp_path: Path, embeddings: FakeEmbeddings | None = None):
    drive = FakeDrive()
    drive.add_root(ROOT, "Tài liệu")
    drive.put(
        ROOT, "doc1111111111111", "hop-dong.txt", "Hợp đồng thuê văn phòng ở Đà Nẵng".encode()
    )
    drive.put(ROOT, "doc2222222222222", "bao-gia.txt", "Báo giá máy chủ và lưu trữ".encode())
    drive.put(
        ROOT,
        "gdoc333333333333",
        "Biên bản họp",
        "# Biên bản\n\nQuyết định tăng ngân sách marketing".encode(),
        mime="application/vnd.google-apps.document",
    )
    service = make_service(tmp_path, drive, embeddings)
    service.add_sources([ROOT])
    service.sync()
    return service


def test_full_text_search_folds_vietnamese_accents(tmp_path: Path) -> None:
    service = indexed(tmp_path)
    for query in ("Đà Nẵng", "da nang", "hop dong"):
        result = service.search(query)
        assert result["mode"] == "full_text"
        assert result["results"][0]["file_name"] == "hop-dong.txt", query
    hit = service.search("ngân sách")["results"][0]
    assert hit["file_name"] == "Biên bản họp"
    assert hit["drive_link"].startswith("https://drive.google.com/")
    assert hit["locator"].startswith("line:")


def test_hybrid_search_with_real_client_against_stub(tmp_path: Path) -> None:
    from gdrive_rag.embeddings import OpenAICompatibleEmbeddings

    stub = EmbeddingStub()
    try:
        client = OpenAICompatibleEmbeddings(
            stub.url, "test-key", "qwen/qwen3-embedding-8b", 1024, DEFAULT_QUERY_INSTRUCTION
        )
        service = indexed(tmp_path, client)  # type: ignore[arg-type]
        assert service.status()["embeddings"]["vectors"] == 3
        document_inputs = [text for body in stub.requests for text in body["input"]]
        assert not any(text.startswith("Instruct:") for text in document_inputs)
        assert all("dimensions" not in body for body in stub.requests)
        assert set(stub.auth) == {"Bearer test-key"}
        stub.requests.clear()
        result = service.search("máy chủ")
        assert result["mode"] == "hybrid"
        assert result["results"][0]["file_name"] == "bao-gia.txt"
        assert stub.requests[0]["input"] == [query_text(DEFAULT_QUERY_INSTRUCTION, "máy chủ")]
        assert stub.requests[0]["input"][0].startswith("Instruct: ")
        assert stub.requests[0]["model"] == "qwen/qwen3-embedding-8b"
        with service.db.read() as connection:
            blob = connection.execute("SELECT vector FROM vectors LIMIT 1").fetchone()[0]
        assert len(blob) == 1024 * 4
    finally:
        stub.close()


def test_model_change_reembeds_instead_of_mixing(tmp_path: Path) -> None:
    first = FakeEmbeddings("model-a", 64)
    service = indexed(tmp_path, first)
    vectors = service.status()["embeddings"]["vectors"]
    assert vectors == 3
    second = FakeEmbeddings("model-b", 32)
    service.embedding_factory = lambda _: second
    result = service.search("máy chủ")
    assert result["mode"] == "full_text" and result["warnings"]
    report = service.embed_pending()
    assert report["embedded"] == 3 and second.document_calls == 1
    assert service.search("máy chủ")["mode"] == "hybrid"


def test_embedding_failure_does_not_fail_sync(tmp_path: Path) -> None:
    embeddings = FakeEmbeddings()
    embeddings.fail = True
    service = indexed(tmp_path, embeddings)
    status = service.status()
    assert status["documents"]["indexed"] == 3
    assert status["embeddings"]["pending"] == 3 and status["embeddings"]["last_error"]
    embeddings.fail = False
    assert service.sync()["embeddings"]["embedded"] == 3


def test_no_key_runs_full_text_then_embeds_later(tmp_path: Path) -> None:
    service = indexed(tmp_path, None)
    assert service.embed_pending() == {"enabled": False, "embedded": 0, "pending": 3}
    later = FakeEmbeddings()
    service.embedding_factory = lambda _: later
    assert service.sync()["embeddings"]["embedded"] == 3


def test_truncate_and_normalize() -> None:
    vector = truncate_normalize([3.0, 4.0, 100.0], 2)
    assert vector == [0.6, 0.8]


def test_read_by_chunk_and_page(tmp_path: Path) -> None:
    service = indexed(tmp_path)
    result = service.read("gdoc333333333333")
    assert result["chunks"][0]["text"].startswith("# Biên bản")
    assert result["next_chunk"] is None
