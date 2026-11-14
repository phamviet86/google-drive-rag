"""Folder ancestry by Drive ID: folder/files search filters, list_folders, renames, migration."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from helpers import FakeDrive, FakeEmbeddings, make_service

from gdrive_rag.service import Service

ROOT = "rootCongTy0000000"
KE_TOAN = "folderKeToan00000"
NAM_2025 = "folder2025000000"
QUY_1 = "folderQuy1000000"
NHAN_SU = "folderNhanSu0000"
Q1 = "fileQ10000000000"
THUE = "fileThue00000000"
KT = "fileKeToan000000"
NS = "fileNhanSu000000"
GOC = "fileGoc000000000"


def company_tree() -> FakeDrive:
    """Công ty / Kế toán / 2025 / Quý 1 (three levels below Kế toán) and a sibling Nhân sự."""
    drive = FakeDrive()
    drive.add_root(ROOT, "Công ty")
    drive.add_folder(KE_TOAN, "Kế toán", ROOT)
    drive.add_folder(NAM_2025, "2025", KE_TOAN)
    drive.add_folder(QUY_1, "Quý 1", NAM_2025)
    drive.add_folder(NHAN_SU, "Nhân sự", ROOT)
    drive.put(ROOT, Q1, "q1.txt", "Báo cáo thuế quý một".encode(), parent=QUY_1)
    drive.put(ROOT, THUE, "thue-2025.txt", "Báo cáo thuế cả năm".encode(), parent=NAM_2025)
    drive.put(ROOT, KT, "ke-toan.txt", "Báo cáo thuế kế toán chung".encode(), parent=KE_TOAN)
    drive.put(ROOT, NS, "nhan-su.txt", "Báo cáo thuế nhân sự".encode(), parent=NHAN_SU)
    drive.put(ROOT, GOC, "goc.txt", "Báo cáo thuế ở thư mục gốc".encode())
    return drive


def indexed(tmp_path: Path, embeddings: FakeEmbeddings | None = None) -> tuple[FakeDrive, Service]:
    drive = company_tree()
    service = make_service(tmp_path, drive, embeddings)
    service.add_sources([f"https://drive.google.com/drive/folders/{ROOT}"])
    service.sync()
    return drive, service


def found(service: Service, query: str = "báo cáo thuế", **scope: object) -> set[str]:
    return {hit["file_name"] for hit in service.search(query, limit=50, **scope)["results"]}  # type: ignore[arg-type]


def test_folder_filter_includes_descendants_and_excludes_siblings(tmp_path: Path) -> None:
    _, service = indexed(tmp_path)
    assert found(service) == {"q1.txt", "thue-2025.txt", "ke-toan.txt", "nhan-su.txt", "goc.txt"}
    assert found(service, folder=KE_TOAN) == {"q1.txt", "thue-2025.txt", "ke-toan.txt"}
    assert found(service, folder=f"https://drive.google.com/drive/folders/{NAM_2025}") == {
        "q1.txt",
        "thue-2025.txt",
    }
    assert found(service, folder=QUY_1) == {"q1.txt"}
    assert found(service, folder=NHAN_SU) == {"nhan-su.txt"}
    assert len(found(service, folder=ROOT)) == 5
    hit = service.search("quý một", folder=KE_TOAN)["results"][0]
    assert hit["document_id"] == Q1 and hit["parent_folder_id"] == QUY_1
    assert hit["path"] == "Kế toán/2025/Quý 1/q1.txt"


def test_files_filter_and_combined_filters(tmp_path: Path) -> None:
    _, service = indexed(tmp_path)
    q1_link = f"https://drive.google.com/file/d/{Q1}/view?usp=sharing"
    assert found(service, files=[q1_link, GOC]) == {"q1.txt", "goc.txt"}
    assert found(service, files=[q1_link, GOC], folder=KE_TOAN) == {"q1.txt"}
    assert found(service, folder=NAM_2025, source="Công ty") == {"q1.txt", "thue-2025.txt"}
    assert found(service, folder=NHAN_SU, files=[Q1]) == set()  # valid but disjoint scopes


def test_unknown_folder_or_file_is_an_error(tmp_path: Path) -> None:
    drive, service = indexed(tmp_path)
    with pytest.raises(ValueError, match="folderMissing0000"):
        service.search("thuế", folder="folderMissing0000")
    with pytest.raises(ValueError, match="folder"):
        service.search("thuế", folder=Q1)  # a file ID is not a folder
    with pytest.raises(ValueError, match="fileMissing00000"):
        service.search("thuế", files=[Q1, "fileMissing00000"])
    with pytest.raises(ValueError, match="not a Google Drive link"):
        service.search("thuế", files=["https://example.com/x"])
    drive.put(ROOT, "fileBroken000000", "hong.xlsx", b"not a zip", mime="application/vnd.ms-excel")
    service.sync()
    with pytest.raises(ValueError, match="hong.xlsx is not indexed"):
        service.search("thuế", files=["fileBroken000000"])


def test_narrow_scope_still_fills_the_limit(tmp_path: Path) -> None:
    drive = company_tree()
    long_text = "\n\n".join(f"Đoạn {i}: báo cáo thuế chi tiết số {i}. " * 8 for i in range(80))
    drive.put(ROOT, "fileLong00000000", "dai.txt", long_text.encode(), parent=NHAN_SU)
    service = make_service(tmp_path, drive, FakeEmbeddings())
    service.add_sources([ROOT])
    service.sync()
    for scope in ({"files": ["fileLong00000000"]}, {"folder": NHAN_SU}):
        results = service.search("báo cáo thuế chi tiết", limit=8, **scope)["results"]  # type: ignore[arg-type]
        assert len(results) == 8, scope
        scores = [hit["score"] for hit in results]
        assert scores == sorted(scores, reverse=True)


def test_vector_candidates_honour_the_folder_filter(tmp_path: Path) -> None:
    embeddings = FakeEmbeddings()
    _, service = indexed(tmp_path, embeddings)
    unfiltered = service.search("xyzzy plugh")  # no lexical match: hits come from vectors only
    assert unfiltered["mode"] == "hybrid" and len(unfiltered["results"]) == 5
    result = service.search("xyzzy plugh", folder=NAM_2025)
    assert result["mode"] == "hybrid"
    assert {hit["file_name"] for hit in result["results"]} == {"q1.txt", "thue-2025.txt"}
    result = service.search("xyzzy plugh", files=[GOC])
    assert [hit["file_name"] for hit in result["results"]] == ["goc.txt"]


def test_full_text_only_honours_the_folder_filter(tmp_path: Path) -> None:
    _, service = indexed(tmp_path, None)
    result = service.search("báo cáo thuế", folder=NAM_2025)
    assert result["mode"] == "full_text"
    assert {hit["file_name"] for hit in result["results"]} == {"q1.txt", "thue-2025.txt"}


def test_rename_and_move_update_ancestry_without_reindexing(tmp_path: Path) -> None:
    embeddings = FakeEmbeddings()
    drive, service = indexed(tmp_path, embeddings)
    drive.downloads.clear()
    calls = embeddings.document_calls
    # rename a folder: paths follow, nothing is downloaded or embedded again
    drive.add_folder(NAM_2025, "Năm 2025", KE_TOAN)
    report = service.sync()
    assert report["indexed"] == 0 and report["unchanged"] == 5
    hit = service.search("quý một")["results"][0]
    assert hit["path"] == "Kế toán/Năm 2025/Quý 1/q1.txt"
    children = service.list_folders(parent=KE_TOAN)["folders"]
    assert [(f["name"], f["path"]) for f in children] == [("Năm 2025", "Công ty/Kế toán/Năm 2025")]
    # move a whole folder (its files keep their parent ID): the old ancestor stops matching
    drive.add_folder(NAM_2025, "Năm 2025", NHAN_SU)
    service.sync()
    assert found(service, folder=KE_TOAN) == {"ke-toan.txt"}
    assert found(service, folder=NHAN_SU) == {"nhan-su.txt", "q1.txt", "thue-2025.txt"}
    # move one file to another folder
    drive.put(ROOT, GOC, "goc.txt", "Báo cáo thuế ở thư mục gốc".encode(), parent=QUY_1)
    service.sync()
    assert found(service, folder=QUY_1) == {"q1.txt", "goc.txt"}
    assert service.search("thư mục gốc")["results"][0]["parent_folder_id"] == QUY_1
    assert drive.downloads == [] and embeddings.document_calls == calls


def test_overlapping_sources_keep_one_document_and_one_tree(tmp_path: Path) -> None:
    drive, service = indexed(tmp_path)
    drive.add_root(KE_TOAN, "Kế toán")  # the subfolder is also added as its own source
    service.add_sources([KE_TOAN])
    drive.downloads.clear()
    service.sync()
    assert drive.downloads == []
    with service.db.read() as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 5
        parent = connection.execute(
            "SELECT parent_id FROM folders WHERE folder_id=?", (KE_TOAN,)
        ).fetchone()[0]
    assert parent == ROOT  # the outer source's listing gives the real parent
    assert found(service, folder=ROOT, source="Kế toán") == {
        "q1.txt",
        "thue-2025.txt",
        "ke-toan.txt",
    }
    top = service.list_folders(source="Kế toán")["folders"]
    assert [(f["folder_id"], f["documents"]) for f in top] == [(KE_TOAN, 3)]
    # the outer source goes away: its root folder is forgotten, the inner tree stays
    service.remove_source("Công ty")
    with pytest.raises(ValueError, match=ROOT):
        service.search("thuế", folder=ROOT)
    assert found(service, folder=KE_TOAN) == {"q1.txt", "thue-2025.txt", "ke-toan.txt"}
    service.sync()
    top = service.list_folders()["folders"]
    assert [(f["folder_id"], f["path"]) for f in top] == [(KE_TOAN, "Kế toán")]


def test_list_folders(tmp_path: Path) -> None:
    _, service = indexed(tmp_path)
    top = service.list_folders()
    assert top["parent"] is None and [f["folder_id"] for f in top["folders"]] == [ROOT]
    assert top["folders"][0]["documents"] == 5 and top["documents"] == []
    level = service.list_folders(parent=f"https://drive.google.com/drive/folders/{ROOT}")
    assert [(f["name"], f["documents"], f["subfolders"]) for f in level["folders"]] == [
        ("Kế toán", 3, 1),
        ("Nhân sự", 1, 0),
    ]
    assert level["folders"][0]["drive_link"] == f"https://drive.google.com/drive/folders/{KE_TOAN}"
    assert level["documents"] == []
    with_docs = service.list_folders(parent=ROOT, include_documents=True)
    assert [d["file_name"] for d in with_docs["documents"]] == ["goc.txt"]
    with pytest.raises(ValueError, match="folderMissing0000"):
        service.list_folders(parent="folderMissing0000")
    assert with_docs["total"] == 3 and with_docs["next_offset"] is None
    second = service.list_folders(parent=ROOT, include_documents=True, offset=2)
    assert second["folders"] == [] and [d["file_name"] for d in second["documents"]] == ["goc.txt"]


def test_read_accepts_a_drive_link(tmp_path: Path) -> None:
    _, service = indexed(tmp_path)
    for value in (
        Q1,
        f"https://drive.google.com/file/d/{Q1}/view",
        f"https://docs.google.com/document/d/{Q1}/edit",
    ):
        assert service.read(value)["chunks"][0]["text"] == "Báo cáo thuế quý một"
    with pytest.raises(ValueError, match="unknown document id"):
        service.read("nope")


def test_upgrade_from_a_d9e8061_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A data dir made before folder ancestry: migrate in place, keep chunks and vectors, and
    fill ancestry on the next sync without downloading or embedding anything."""
    import gdrive_rag.db as db
    from gdrive_rag.chunking import make_chunks
    from gdrive_rag.models import ExtractedDocument, SourceSpan

    drive = company_tree()
    embeddings = FakeEmbeddings()
    old = db.Database(tmp_path / "index.sqlite3")
    with monkeypatch.context() as patch:
        patch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])
        old.migrate()
    with old.transaction() as connection:  # what d9e8061 wrote after one sync
        connection.execute(
            "INSERT INTO sources(link,root_id,kind,drive_id,name,last_status,file_count) "
            "VALUES(?,?,'folder','',?,'ok',5)",
            (ROOT, ROOT, "Công ty"),
        )
        listing = drive.list_files(drive.roots[ROOT])
        for item in listing.items:
            text = drive.content[item.id].decode()
            chunk = make_chunks(
                ExtractedDocument(text, [SourceSpan("line", "line:1", 0, len(text))], "text/plain"),
                1500,
                200,
            )[0]
            cursor = connection.execute(
                "INSERT INTO documents(file_id,name,path,mime_type,web_url,modified_time,"
                "fingerprint,status,chunk_count) VALUES(?,?,?,?,?,?,?,'indexed',1)",
                (
                    item.id,
                    item.name,
                    item.path,
                    item.mime_type,
                    item.web_url,
                    item.modified_time,
                    item.fingerprint,
                ),
            )
            connection.execute("INSERT INTO document_sources VALUES(?,1)", (cursor.lastrowid,))
            connection.execute(
                "INSERT INTO chunks(document_id,ordinal,text,locator,chunk_hash) VALUES(?,0,?,?,?)",
                (cursor.lastrowid, chunk.text, chunk.locator, chunk.chunk_hash),
            )
            vector = embeddings._vector(chunk.text)
            connection.execute(
                "INSERT INTO vectors VALUES(?,?)",
                (chunk.chunk_hash, struct.pack(f"<{len(vector)}f", *vector)),
            )
        connection.execute(
            "INSERT INTO meta VALUES('embedding_identity', ?)", (embeddings.identity,)
        )
    service = make_service(tmp_path, drive, embeddings)
    with service.db.read() as connection:
        versions = [r[0] for r in connection.execute("SELECT version FROM schema_migrations")]
    assert versions == [1, 2, 3]
    assert service.search("quý một")["mode"] == "hybrid"  # works before the first new sync
    with pytest.raises(ValueError, match="not indexed folder"):
        service.search("thuế", folder=KE_TOAN)
    report = service.sync()
    assert report["unchanged"] == 5 and report["indexed"] == 0
    assert drive.downloads == [] and embeddings.document_calls == 0
    assert found(service, folder=KE_TOAN) == {"q1.txt", "thue-2025.txt", "ke-toan.txt"}
    assert service.search("quý một")["results"][0]["parent_folder_id"] == QUY_1
